# -*- coding: utf-8 -*-
"""engine/watchctx.py 的单测：phase 判定、任务去重、只发一次。

注意：TaskRegistry / WatchTask 目前**未接线**（main.py 没有引用），
这里只测数据结构本身，不代表插件已经在用两段式。
"""

import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from engine import watchctx as W  # noqa: E402
from engine.subs import Cue, SubsDoc  # noqa: E402


class TestPhase(unittest.TestCase):
    def test_nothing(self):
        self.assertEqual(W.WatchContext().phase, W.NOTHING)

    def test_subs_only(self):
        c = W.WatchContext(subs=SubsDoc(cues=[Cue(0, 1, "话")], dur=10))
        self.assertEqual(c.phase, W.SUBS_ONLY)

    def test_frames_only(self):
        c = W.WatchContext(frames=["a.jpg"])
        self.assertEqual(c.phase, W.FRAMES_ONLY)

    def test_settled(self):
        c = W.WatchContext(subs=SubsDoc(cues=[Cue(0, 1, "话")], dur=10), frames=["a.jpg"])
        self.assertEqual(c.phase, W.SETTLED)

    def test_empty_subs_is_not_subs(self):
        c = W.WatchContext(subs=SubsDoc(cues=[], dur=10), frames=["a.jpg"])
        self.assertEqual(c.phase, W.FRAMES_ONLY)

    def test_brief_has_fields(self):
        c = W.WatchContext(subs=SubsDoc(cues=[Cue(0, 1, "话")], dur=10), frames=["a.jpg"])
        b = c.brief()
        for k in ("phase=", "subs=", "frames=", "cover="):
            self.assertIn(k, b)

    def test_errors_dedup(self):
        c = W.WatchContext()
        c.add_error("a")
        c.add_error("a")
        c.add_error("b")
        self.assertEqual(c.errors, ["a", "b"])


class TestTaskRegistry(unittest.TestCase):
    def test_open_reuses_alive(self):
        r = W.TaskRegistry()
        a = r.open("BV1", "s1")
        b = r.open("BV1", "s1")
        self.assertIs(a, b)

    def test_reopen_after_dead(self):
        r = W.TaskRegistry()
        a = r.open("BV1", "s1")
        a.touch(W.T_DONE)
        a.updated -= 3600  # 假装过了很久
        b = r.open("BV1", "s1")
        self.assertIsNot(a, b)

    def test_cancel_session(self):
        r = W.TaskRegistry()
        t1 = r.open("BV1", "s1")
        t2 = r.open("BV2", "s1")
        n = r.cancel_session("s1", except_fp="BV2")
        self.assertEqual(n, 1)
        self.assertEqual(t1.phase, W.T_CANCELLED)
        self.assertTrue(t2.alive)

    def test_find_session(self):
        r = W.TaskRegistry()
        r.open("BV1", "s1")
        self.assertIsNotNone(r.find_session("s1"))
        self.assertIsNone(r.find_session("s2"))

    def test_claim_send_once(self):
        r = W.TaskRegistry()
        t = r.open("BV1", "s1")
        self.assertTrue(r.claim_send(t))
        self.assertFalse(r.claim_send(t))

    def test_gc_removes_old(self):
        r = W.TaskRegistry()
        t = r.open("BV1", "s1")
        t.touch(W.T_DONE)
        t.updated -= r.MAX_AGE + 10
        r.open("BV2", "s2")  # 触发 _gc
        self.assertIsNone(r.get("BV1"))


class TestDescribe(unittest.TestCase):
    def test_texts(self):
        self.assertIn("画面", W.describe_phase(W.SUBS_ONLY))
        self.assertIn("字幕", W.describe_phase(W.FRAMES_ONLY))
        self.assertEqual(W.describe_phase(W.SETTLED), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
