# -*- coding: utf-8 -*-
"""第二轮审查后的四条护栏测试：每一条都是「现在不这样写就会回退」的地方。

1. 外层超时不能把已经拿到的字幕一起带走（ctx 在 wait_for 外面建）
2. 长片只看了前段时**必须说出来**（只写错误码不说话术等于没做）
3. 字幕不完整时不能按密度选点（否则帧全被吸到有字幕的那一小段）
4. 末尾超出按时长比例判，不是固定 5 秒

跑法：
  PYTHONIOENCODING=utf-8 "C:/Users/33887/AppData/Roaming/uv/tools/astrbot/Scripts/python.exe" -m unittest tests.test_watch_flow -v
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as M  # noqa: E402

URL = "https://www.bilibili.com/video/BV1jhn16GEcv"


def make_doc(ok=True):
    subs = M.subs_mod
    lines = ["第一句话。", "第二句话。", "第三句话。"]
    cues = [subs.Cue(start=i * 3.0, end=i * 3.0 + 2.5, text=t) for i, t in enumerate(lines)]
    return subs.SubsDoc(cues=cues, dur=30.0, lang="ai-zh", source="bilibili", raw_chars=30)


def make_plugin():
    p = object.__new__(M.FeiyuVideo)
    p.cfg = dict(M.DEFAULT_CONFIG)
    p.cfg["see_timeout"] = 420
    p._last_frames = {}
    p._temporary_files = set()
    p._video_sem = asyncio.Semaphore(2)
    return p


class TimeoutKeepsSubs(unittest.TestCase):
    """B1：外层 wait_for 取消时，已经读到的字幕不能跟着一起没。"""

    def test_超时后字幕还在(self):
        p = make_plugin()
        p._bili_probe = lambda url: ({"aid": 1, "cid": 2, "dur": 7200}, [{"lan": "ai-zh"}])
        p._subs_doc = lambda meta, subs: make_doc()

        p._ytdlp_meta = lambda url: {"dur": 7200.0, "title": "t"}

        def _slow_download(url, max_sec=0):
            time.sleep(1.5)  # 模拟整片还没下完
            return "/tmp/x.mp4", 7200.0

        p._download_full = _slow_download

        ctx = M.wctx_mod.WatchContext(ref=URL, how="hook")
        budget = 0.8  # 比 _slow_frames 短：这一轮一定超时

        async def run():
            try:
                await asyncio.wait_for(p._watch_gather(URL, how="hook", ctx=ctx), timeout=budget)
            except asyncio.TimeoutError:
                pass
            return ctx

        ctx = asyncio.run(run())
        self.assertTrue(ctx.subs is not None and ctx.subs.cues, "超时把字幕一起带走了")
        self.assertFalse(ctx.frames, "帧本来就没抽完，不该有")


class LongVideoMustSay(unittest.TestCase):
    """B2：截断了就明说只看前段，不能让她以为全片都看过。"""

    def _ctx(self):
        ctx = M.wctx_mod.WatchContext(ref=URL, how="hook")
        ctx.subs = make_doc()
        ctx.frames = ["/tmp/a.jpg"]
        ctx.dur = 7200.0
        ctx.add_error("long")
        return ctx

    def test_提示语里有前段说明(self):
        p = make_plugin()
        hint = p._watch_hint(self._ctx(), handed=2, can_image=True)
        self.assertIn("分钟", hint)
        self.assertIn("后面", hint)
        self.assertIn("画面", hint)

    def test_tool_结果里也有(self):
        p = make_plugin()
        out = p._watch_result(self._ctx())
        self.assertIn("分钟", out)

    def test_没截断时不说这些(self):
        p = make_plugin()
        ctx = M.wctx_mod.WatchContext(ref=URL, how="hook")
        ctx.subs = make_doc()
        ctx.frames = ["/tmp/a.jpg"]
        hint = p._watch_hint(ctx, handed=2, can_image=True)
        self.assertNotIn("只看了前", hint)

    def test_截断时字幕降级为节选口径(self):
        """截断了就不能说「一句句都在」，得说「只念了一部分」。"""
        p = make_plugin()
        text = p._subs_text(self._ctx())
        self.assertTrue(text)
        self.assertIn("一部分", text)
        self.assertIn("没念到", text)

class BrokenSubsNoDensity(unittest.TestCase):
    """P1-1：字幕不完整就得退等间隔，别把帧全吸到有字幕的那一段。"""

    def test_不完整时不调密度选点(self):
        p = make_plugin()
        p._ytdlp_fetch = lambda url, full=False, max_sec=0: "/tmp/fake.mp4"
        p._duration = lambda path: 30.0
        p._extract_at = lambda path, times, width=0: ["/tmp/a.jpg"]
        p._cleanup_download = lambda path: None

        doc = make_doc()
        doc.cues = doc.cues[:1]  # 只覆盖开头 3 秒 → cover.ok 必然是 False
        self.assertFalse(doc.cover.ok)

        called = {"plan": 0}
        real_plan = M.picks_mod.plan

        def _spy(*a, **kw):
            called["plan"] += 1
            return real_plan(*a, **kw)

        M.picks_mod.plan = _spy
        try:
            frames, dur, spots = p._frames_by_plan(URL, {"dur": 30}, doc)
        finally:
            M.picks_mod.plan = real_plan

        self.assertEqual(called["plan"], 0, "字幕不完整还走了密度选点")
        self.assertTrue(frames)
        self.assertEqual(len(spots), p._shot_count())


class ParallelChains(unittest.TestCase):
    """两条链必须并排跑：各睡 0.4 秒，总耗时应该接近 0.4 而不是 0.8。"""

    def test_字幕链和画面链同时跑(self):
        p = make_plugin()
        p._bili_probe = lambda url: (
            time.sleep(0.4),
            ({"aid": 1, "cid": 2, "dur": 30}, [{"lan": "ai-zh"}]),
        )[1]
        p._subs_doc = lambda meta, subs: make_doc()
        p._ytdlp_meta = lambda url: (time.sleep(0.4), {"dur": 30.0})[1]
        p._download_full = lambda url, max_sec=0: ("/tmp/x.mp4", 30.0)
        p._pick_frames = lambda path, dur, doc, n=0: (["/tmp/a.jpg"], [])

        ctx = M.wctx_mod.WatchContext(ref=URL, how="hook")
        t0 = time.time()
        asyncio.run(p._watch_gather(URL, how="hook", ctx=ctx))
        used = time.time() - t0

        self.assertLess(used, 0.75, f"两条链没有并行（用了 {used:.2f}s，串行要 0.8s 以上）")
        self.assertTrue(ctx.subs is not None, "并行了但字幕丢了")
        self.assertTrue(ctx.frames, "并行了但画面丢了")


class OverRatioNotSeconds(unittest.TestCase):
    """P1-4：末尾超出用比例，不是固定 5 秒。"""

    def test_短片超六秒算异常(self):
        c = M.subs_mod.Cover(ratio=0.99, max_gap=1.0, start=0.0, over=8.0, over_ratio=8.0 / 120, n=10)
        self.assertFalse(c.ok, "2 分钟的片超 8 秒（6.7%）应该判异常")

    def test_长片超两百秒不算异常(self):
        c = M.subs_mod.Cover(ratio=0.99, max_gap=1.0, start=0.0, over=200.0, over_ratio=200.0 / 7200, n=400)
        self.assertTrue(c.ok, "2 小时的片超 200 秒（2.7%）不该判异常")


if __name__ == "__main__":
    unittest.main()
