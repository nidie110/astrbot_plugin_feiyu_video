"""观看上下文：统一次 watch（hook 与 tool 两条路）的产物。

解决的问题（design 5.1）：原来 hook 取完帧就 `event.set_extra("feiyu_saw_frames", True)`，
`do_watch` 一看到这个 bool 就 return「画面已经在这一轮的请求里了」——字幕要是加在
任何一边，另一边都被短路掉。

这里改成**一个对象**：谁取的都往里写，消费方按 `phase` 判断缺什么，而不是猜。

    phase = "settled"      字幕 + 帧 都有
          = "subs_only"    只有话，画面没拿到
          = "frames_only"  只有画面，没拿到字幕
          = "nothing"      都没有

`WatchTask` 是长片两段式（design 5.9）的状态机：Phase A 先把字幕给出去，
Phase B 后台下完再补画面，靠 `fingerprint` 去重、`sent_result` 保证只发一次。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .subs import Cover, SubsDoc

# --- 阶段常量 -------------------------------------------------------------

SETTLED = "settled"
SUBS_ONLY = "subs_only"
FRAMES_ONLY = "frames_only"
NOTHING = "nothing"

# WatchTask 的生命周期
T_SUBS_SENT = "subs_sent"
T_DOWNLOADING = "downloading"
T_DONE = "done"
T_FAILED = "failed"
T_CANCELLED = "cancelled"


@dataclass
class WatchContext:
    """一次观看的全部产物。hook 与 tool 共享同一份。"""

    ref: str = ""  # 原始引用（链接 / 本地路径）
    task_id: str = ""
    subs: SubsDoc | None = None
    frames: list[str] = field(default_factory=list)
    plan: list = field(default_factory=list)  # list[picks.Spot]
    dur: float = 0.0
    how: str = "tool"  # "hook" | "tool"
    errors: list[str] = field(default_factory=list)

    @property
    def phase(self) -> str:
        has_subs = bool(self.subs and self.subs.cues)
        has_frames = bool(self.frames)
        if has_subs and has_frames:
            return SETTLED
        if has_subs:
            return SUBS_ONLY
        if has_frames:
            return FRAMES_ONLY
        return NOTHING

    @property
    def cover(self) -> Cover:
        return self.subs.cover if self.subs else Cover()

    def add_error(self, msg: str) -> None:
        if msg and msg not in self.errors:
            self.errors.append(msg)

    def brief(self) -> str:
        """一行摘要，进日志。"""
        cov = self.cover
        return (
            f"phase={self.phase} how={self.how} "
            f"subs={len(self.subs.cues) if self.subs else 0}条/"
            f"{self.subs.chars if self.subs else 0}字 "
            f"cover={cov.ratio:.2f} gap={cov.max_gap:.0f}s "
            f"frames={len(self.frames)} "
            f"err={'|'.join(self.errors) if self.errors else '-'}"
        )


@dataclass
class WatchTask:
    """长片两段式的后台任务。"""

    fingerprint: str  # BV + cid，去重键
    session: str  # unified_msg_origin
    ref: str = ""
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    phase: str = T_SUBS_SENT
    sent_result: bool = False
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    note: str = ""

    def touch(self, phase: str | None = None, note: str = "") -> None:
        if phase:
            self.phase = phase
        if note:
            self.note = note
        self.updated = time.time()

    @property
    def alive(self) -> bool:
        return self.phase in (T_SUBS_SENT, T_DOWNLOADING)

    @property
    def age(self) -> float:
        return time.time() - self.created


class TaskRegistry:
    """进程内的任务表。够用：单机、单进程、量小。

    只保证三件事：同一个 fingerprint 不重复建档；同一会话只留一个在跑的任务；
    老的自动清掉（防止进程跑久了表膨胀）。
    """

    MAX_AGE = 3600.0  # 1 小时后自动清
    MAX_ITEMS = 200

    def __init__(self) -> None:
        self._by_fp: dict[str, WatchTask] = {}

    # ---- 读 ----------------------------------------------------------------

    def get(self, fingerprint: str) -> WatchTask | None:
        t = self._by_fp.get(fingerprint)
        if t and not t.alive and t.age > 600:
            # 已收尾超过 10 分钟，允许重建
            return None
        return t

    def find_session(self, session: str) -> WatchTask | None:
        for t in self._by_fp.values():
            if t.session == session and t.alive:
                return t
        return None

    def items(self) -> list[WatchTask]:
        return list(self._by_fp.values())

    # ---- 写 ----------------------------------------------------------------

    def open(self, fingerprint: str, session: str, ref: str = "") -> WatchTask:
        """开一个任务。同 fingerprint 已存在且还活着 → 复用（不重复下载）。"""
        old = self._by_fp.get(fingerprint)
        if old and old.alive:
            return old
        task = WatchTask(fingerprint=fingerprint, session=session, ref=ref)
        self._by_fp[fingerprint] = task
        self._gc()
        return task

    def cancel_session(self, session: str, except_fp: str = "") -> int:
        """该会话换了新视频 → 取消旧任务。返回取消数。"""
        n = 0
        for t in self._by_fp.values():
            if t.session == session and t.alive and t.fingerprint != except_fp:
                t.touch(T_CANCELLED, "会话换片")
                n += 1
        return n

    def claim_send(self, task: WatchTask) -> bool:
        """抢占「发结果」的权利。返回 True 表示这次该发，False 表示别人已经发过。"""
        if task.sent_result:
            return False
        task.sent_result = True
        task.touch()
        return True

    # ---- 清理 --------------------------------------------------------------

    def _gc(self) -> None:
        now = time.time()
        dead = [k for k, t in self._by_fp.items() if now - t.updated > self.MAX_AGE]
        for k in dead:
            self._by_fp.pop(k, None)
        if len(self._by_fp) > self.MAX_ITEMS:
            for k, _ in sorted(self._by_fp.items(), key=lambda kv: kv[1].updated)[
                : len(self._by_fp) - self.MAX_ITEMS
            ]:
                self._by_fp.pop(k, None)


def describe_phase(phase: str) -> str:
    """给模型看的缺失说明（人话，不解释机制）。"""
    return {
        SUBS_ONLY: "话我全听到了，画面这次没跟上。",
        FRAMES_ONLY: "画面看到了，但这次没拿到字幕。",
        NOTHING: "这次什么都没拿到。",
    }.get(phase, "")
