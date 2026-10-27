# -*- coding: utf-8 -*-
"""hook → tool 的接线：用假 event/req 钉死三件事。

1. 台词和画面都只进 `extra_user_content_parts`，而且都标了「不落库」
2. **绝不写 system_prompt** —— 那是会话前缀，每轮变一次就把前缀缓存废掉
3. tool 复用 hook 那轮的 `WatchContext`，不会再下载一遍

跑法（必须用 AstrBot 的 python，要 import astrbot）：
  PYTHONIOENCODING=utf-8 "C:/Users/33887/AppData/Roaming/uv/tools/astrbot/Scripts/python.exe" -m unittest tests.test_hook_flow -v
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as M  # noqa: E402

URL = "https://www.bilibili.com/video/BV1jhn16GEcv"


class FakeEvent:
    """只实现 hook / tool 会碰到的那几个方法。"""

    def __init__(self, text=f"看看这个 {URL}"):
        self.message_str = text
        self.unified_msg_origin = "test:p2p:1"
        self._extra = {}
        self.message_obj = type("O", (), {"message": []})()

    def get_extra(self, k):
        return self._extra.get(k)

    def set_extra(self, k, v):
        self._extra[k] = v


class FakeReq:
    def __init__(self):
        self.prompt = "看看这个"
        self.system_prompt = "SYSTEM-PROMPT-这一轮不许变"
        self.image_urls = []
        self.extra_user_content_parts = []


def make_ctx():
    subs = M.subs_mod
    wc = M.wctx_mod
    assert subs is not None and wc is not None, "engine 没加载起来"
    lines = [
        "今天咱们装六个插件。",
        "第一个是记忆插件，它按半衰期遗忘。",
        "第二个是情绪插件，七根轴。",
    ]
    cues = [subs.Cue(start=i * 3.0, end=i * 3.0 + 2.5, text=t) for i, t in enumerate(lines)]
    doc = subs.SubsDoc(cues=cues, dur=30.0, lang="ai-zh", source="bilibili", raw_chars=48)
    ctx = wc.WatchContext(ref=URL, how="hook")
    ctx.subs = doc
    ctx.frames = ["/tmp/_f1.jpg", "/tmp/_f2.jpg"]
    ctx.dur = 30.0
    return ctx


def make_plugin(ctx):
    p = object.__new__(M.FeiyuVideo)
    p.cfg = dict(M.DEFAULT_CONFIG)
    p._last_frames = {}
    p.calls = []
    fake = ctx

    async def _gather(ref, *, how="tool", ctx=None):
        p.calls.append((ref, how))
        # hook 现在会先把 ctx 建好传进来（超时时才留得住已读到的内容）
        box = ctx if ctx is not None else M.wctx_mod.WatchContext(ref=ref, how=how)
        if fake is not None:
            box.subs = fake.subs
            box.frames = list(fake.frames or [])
            box.dur = fake.dur
        return box

    p._watch_gather = _gather
    p._main_supports_image = lambda event: True
    return p


class HookFlow(unittest.TestCase):
    def _run_hook(self, ctx=None):
        ctx = ctx or make_ctx()
        p = make_plugin(ctx)
        ev, req = FakeEvent(), FakeReq()
        asyncio.run(p.see_frames_with_llm(ev, req))
        return p, ev, req, ctx

    def test_临时内容不写系统提示语(self):
        """system_prompt 是会话前缀，动它等于废掉前缀缓存。"""
        _, _, req, _ = self._run_hook()
        self.assertEqual(req.system_prompt, "SYSTEM-PROMPT-这一轮不许变")
        self.assertTrue(req.extra_user_content_parts, "一个字都没注入，说明这条路根本没走通")

    def test_临时内容都标了不落库(self):
        """不标 _no_save 的话，这些内容会被写进会话历史、之后每轮重发。"""
        _, _, req, _ = self._run_hook()
        for part in req.extra_user_content_parts:
            self.assertTrue(
                getattr(part, "_no_save", False),
                f"这一份没标不落库：{type(part).__name__}",
            )

    def test_台词真的进了上下文(self):
        _, _, req, _ = self._run_hook()
        blob = "".join(getattr(p, "text", "") or "" for p in req.extra_user_content_parts)
        self.assertIn("记忆插件", blob, "台词没进上下文")

    def test_hook_与_tool_共用一份(self):
        p, ev, _, ctx = self._run_hook()
        got = ev.get_extra("feiyu_watch_ctx")
        self.assertIsNotNone(got, "ctx 没挂到 event 上")
        self.assertEqual(got.ref, ctx.ref)
        out = asyncio.run(p.do_watch(ev, URL))
        self.assertIn("记忆插件", out, "tool 没把台词交回去")
        self.assertEqual(len(p.calls), 1, "tool 又去跑了一遍 gather（等于下了两次片）")

    def test_空文本不注入(self):
        p = make_plugin(make_ctx())
        req = FakeReq()
        self.assertFalse(p._inject_temp(req, ""))
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertEqual(req.system_prompt, "SYSTEM-PROMPT-这一轮不许变")


if __name__ == "__main__":
    unittest.main()
