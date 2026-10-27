"""抽帧选点：按字幕信息密度挑「值得看画面」的时刻。

设计（见 docs/watch-v3-design.md 第 5.5 节）：

    S_i = 0.5·z(字幕字数密度) + 0.15·z(cue 频率) + 0.2·z(关键词/数字/专名) + 0.15·z(切镜)

滑窗 20 秒，取局部峰值，再加两条例保护：

- **保底锚点 50%**：等间隔铺满全片。不这么做的话，一段连续演讲会吸走全部
  名额，片子后半段一帧都没有。
- **相邻帧最小间距 2w**：避免 30 帧全落在同一个镜头的 3 秒内。

退化阶梯（宁可粗，不要错）：
1. 有字幕 + 有切镜 → 四项全用
2. 有字幕、没切镜 → 前三项（切镜项置 0）
3. 没字幕、有切镜 → 只用切镜项
4. 都没有 → 完全等间隔（= 现状行为）

公开接口：
    plan(cues, dur, scenes=None, n=10) -> list[Spot]
    even_spots(dur, n) -> list[Spot]
    density_series(cues, dur, scenes, w) -> list[float]   调试用
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 默认窗口（秒）。20 秒约等于一句话半到两句话，够一个画面事件，又不至于糊成一片。
W = 20.0

# 「信息量大」的标记：数字、钱、比例、英文专名、引号/书名号里的词
_MARK = re.compile(
    r"\d+(?:\.\d+)?%?|\d+[万亿千百]|"
    r"[A-Za-z][A-Za-z0-9_.\-]{2,}|"
    r"《[^》]{1,20}》|「[^」]{1,20}」|\"[^\"]{1,20}\""
)

# 三个权重（第四项切镜权重单独给，因为它可能缺席）
_W_DENSITY = 0.5
_W_FREQ = 0.15
_W_MARK = 0.2
_W_SCENE = 0.15


@dataclass(frozen=True)
class Spot:
    """一个取帧点。"""

    t: float  # 取帧时刻（秒）
    bucket: int  # 所属窗口序号（调试用）
    score: float  # 归一化后的分数
    reason: str  # "density" / "anchor" / "scene" / "even"

    @property
    def mmss(self) -> str:
        s = int(max(0, self.t))
        return f"{s // 60}:{s % 60:02d}"


def _z(vals: list[float]) -> list[float]:
    """z-score 归一化；全相同则全 0（避免除零、也避免把均匀分布当峰值）。"""
    n = len(vals)
    if n == 0:
        return []
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    sd = var**0.5
    if sd < 1e-9:
        return [0.0] * n
    return [(v - mean) / sd for v in vals]


def even_spots(dur: float, n: int) -> list[Spot]:
    """完全等间隔。n 个点落在 (0, dur) 内，首尾各留半个间隔。"""
    n = max(1, int(n))
    dur = max(1.0, float(dur))
    step = dur / n
    return [Spot(t=step * (i + 0.5), bucket=i, score=0.0, reason="even") for i in range(n)]


def density_series(cues, dur: float, scenes=None, w: float = W) -> list[dict]:
    """算出每个窗口的原始指标与合成分。

    返回 `[{"i", "t0", "t1", "n_cue", "chars", "marks", "scenes", "score"}]`，
    方便调试与自己写测试。
    """
    dur = max(1.0, float(dur))
    nb = max(1, int(round(dur / max(1.0, w))))
    w = dur / nb  # 均分，避免最后一段特别短
    rows = [
        {"i": i, "t0": i * w, "t1": (i + 1) * w, "n_cue": 0, "chars": 0, "marks": 0, "scenes": 0}
        for i in range(nb)
    ]

    def _bucket(t: float) -> int:
        return min(nb - 1, max(0, int(t // w)))

    for c in cues or []:
        i = _bucket(c.mid)
        rows[i]["n_cue"] += 1
        rows[i]["chars"] += c.chars
        rows[i]["marks"] += len(_MARK.findall(c.text or ""))

    for s in scenes or []:
        try:
            rows[_bucket(float(s))]["scenes"] += 1
        except (TypeError, ValueError):
            continue

    has_cue = any(r["n_cue"] for r in rows)
    has_scene = any(r["scenes"] for r in rows)

    zd = _z([float(r["chars"]) for r in rows]) if has_cue else [0.0] * nb
    zf = _z([float(r["n_cue"]) for r in rows]) if has_cue else [0.0] * nb
    zm = _z([float(r["marks"]) for r in rows]) if has_cue else [0.0] * nb
    zs = _z([float(r["scenes"]) for r in rows]) if has_scene else [0.0] * nb

    for i, r in enumerate(rows):
        s = 0.0
        if has_cue:
            s += _W_DENSITY * zd[i] + _W_FREQ * zf[i] + _W_MARK * zm[i]
        if has_scene:
            s += _W_SCENE * zs[i]
        r["score"] = s
    return rows


def _best_moment(cues, t0: float, t1: float, fallback: float) -> float:
    """窗口内取「字数最多那条 cue 的中点」——帧和台词才对得上。"""
    inside = [c for c in (cues or []) if t0 <= c.mid < t1]
    if not inside:
        return fallback
    best = max(inside, key=lambda c: (c.chars, -c.start))
    return best.mid


def plan(
    cues,
    dur: float,
    scenes=None,
    n: int = 10,
    w: float = W,
    min_gap_ratio: float = 2.0,
) -> list[Spot]:
    """选 n 个取帧点。

    n: 要几张
    min_gap_ratio: 最小间距 = min_gap_ratio × 窗口宽度（防挤在一起）
    """
    n = max(1, int(n))
    dur = max(1.0, float(dur))
    rows = density_series(cues, dur, scenes, w)
    nb = len(rows)
    win = rows[0]["t1"] - rows[0]["t0"] if rows else w

    # 一个窗口都分不到（片太短）：直接等间隔
    if nb <= 1 or not any(r["score"] for r in rows):
        return even_spots(dur, n)

    # 保底锚点占一半（至少 1 个），密度占另一半（至少 1 个）
    n_anchor = max(1, min(n - 1, int(round(n * 0.5)))) if n > 1 else 0
    n_dense = max(0, n - n_anchor)

    # 密度峰：按分数降序取，跳过离已选点太近的
    # 间距上限是 2 个窗口（防挤在一个镜头里），下限由 n 决定（要 30 张就不能按 40 秒间距排）
    min_gap = min(win * max(1.0, float(min_gap_ratio)), dur / max(1, n) * 0.6)
    picked: list[Spot] = []
    order = sorted(range(nb), key=lambda i: -rows[i]["score"])
    for i in order:
        if len(picked) >= n_dense:
            break
        if rows[i]["score"] <= 0:
            break
        t = _best_moment(cues, rows[i]["t0"], rows[i]["t1"], (rows[i]["t0"] + rows[i]["t1"]) / 2)
        if any(abs(t - p.t) < min_gap for p in picked):
            continue
        picked.append(Spot(t=t, bucket=i, score=rows[i]["score"], reason="density"))

    # 保底锚点：等间隔，跳过离已选点太近的
    for s in even_spots(dur, n_anchor + 1)[1:] if n_anchor else []:
        if any(abs(s.t - p.t) < min_gap * 0.6 for p in picked):
            continue
        picked.append(Spot(t=s.t, bucket=int(s.t // win), score=0.0, reason="anchor"))
        if len(picked) >= n:
            break

    # 还不够 n 张：补等间隔
    if len(picked) < n:
        for s in even_spots(dur, n * 3):
            if len(picked) >= n:
                break
            if any(abs(s.t - p.t) < min_gap * 0.35 for p in picked):
                continue
            picked.append(Spot(t=s.t, bucket=s.bucket, score=0.0, reason="even"))

    picked.sort(key=lambda p: p.t)
    return picked[:n]


def describe(spots: list[Spot]) -> str:
    """给日志用的一行摘要。"""
    if not spots:
        return "无"
    kinds: dict[str, int] = {}
    for s in spots:
        kinds[s.reason] = kinds.get(s.reason, 0) + 1
    detail = " ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
    first_last = f"{spots[0].mmss}…{spots[-1].mmss}"
    return f"{len(spots)} 张（{detail}）{first_last}"
