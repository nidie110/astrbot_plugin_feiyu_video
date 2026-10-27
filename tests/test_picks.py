# -*- coding: utf-8 -*-
"""engine/picks.py 的离线单测：信息密度选点、保底锚点、最小间距、退化。

    python -m unittest tests.test_picks -v
"""
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from engine import picks as P  # noqa: E402
from engine.subs import Cue  # noqa: E402


def cues_every(step=3.0, dur=300.0, text="随便说点什么东西凑字数。"):
    out, t = [], 0.0
    while t < dur:
        out.append(Cue(t, t + step * 0.8, text))
        t += step
    return out


def dense_block(dur=600.0, a=200.0, b=280.0):
    """200–280 秒之间塞满数字和专名，其它地方稀疏。"""
    out, t = [], 0.0
    while t < dur:
        inside = a <= t < b
        txt = "涨了 37%，王小明说 2026 年要做到 1.2 亿，代价是砍掉三个项目。" if inside else "嗯……"
        if inside:
            out.append(Cue(t, t + 1.2, txt))
        t += 1.5 if inside else 6.0
    return out


class TestEven(unittest.TestCase):
    def test_count_and_range(self):
        got = P.even_spots(100.0, 5)
        self.assertEqual(len(got), 5)
        for s in got:
            self.assertGreater(s.t, 0)
            self.assertLess(s.t, 100.0)
        self.assertTrue(all(s.reason == "even" for s in got))

    def test_degenerate(self):
        self.assertEqual(len(P.even_spots(0.0, 3)), 3)
        self.assertEqual(len(P.even_spots(10.0, 0)), 1)
        self.assertEqual(len(P.even_spots(-5.0, 2)), 2)

    def test_ordered(self):
        ts = [s.t for s in P.even_spots(120.0, 8)]
        self.assertEqual(ts, sorted(ts))


class TestDensitySeries(unittest.TestCase):
    def test_scores_higher_in_dense(self):
        cues = dense_block()
        rows = P.density_series(cues, 600.0)
        self.assertGreater(len(rows), 5)
        dense_i = max(range(len(rows)), key=lambda i: rows[i]["score"])
        self.assertTrue(200 <= rows[dense_i]["t0"] < 280, f"峰值在 {rows[dense_i]['t0']}")

    def test_no_cues_no_scenes_all_zero(self):
        rows = P.density_series([], 100.0)
        self.assertTrue(all(r["score"] == 0 for r in rows))

    def test_only_scenes(self):
        rows = P.density_series([], 100.0, scenes=[10, 11, 12, 80])
        scored = [r for r in rows if r["score"] > 0]
        self.assertTrue(scored)
        # 10–12 那个窗口应该最突出
        self.assertLess(min(r["t0"] for r in scored), 40)

    def test_marks_counted(self):
        cues = [Cue(0, 2, "涨了 37%"), Cue(3, 5, "OpenAI 那边"), Cue(6, 8, "没有数字")]
        rows = P.density_series(cues, 12.0)
        self.assertGreaterEqual(sum(r["marks"] for r in rows), 2)


class TestPlan(unittest.TestCase):
    def test_returns_n(self):
        for n in (1, 3, 10, 30):
            got = P.plan(dense_block(), 600.0, scenes=None, n=n)
            self.assertEqual(len(got), n, f"n={n}")

    def test_sorted_and_in_range(self):
        got = P.plan(cues_every(), 300.0, n=12)
        ts = [s.t for s in got]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(all(0 <= t <= 301 for t in ts))

    def test_no_crowding(self):
        """最小间距：不能全挤在一个窗口里。"""
        got = P.plan(dense_block(), 600.0, n=10)
        ts = [s.t for s in got]
        win = 600.0 / max(1, int(round(600.0 / P.W)))
        for a, b in zip(ts, ts[1:]):
            self.assertGreaterEqual(b - a, win * 0.3, f"间距 {b - a:.1f} 太小")

    def test_anchor_keeps_tail_covered(self):
        """密集段在开头时，后半段也得有点。"""
        cues = dense_block(dur=600.0, a=0.0, b=80.0)
        got = P.plan(cues, 600.0, n=10)
        self.assertGreater(max(s.t for s in got), 300, "后半段一帧都没有")

    def test_degenerate_all_even(self):
        got = P.plan([], 600.0, scenes=None, n=6)
        self.assertEqual(len(got), 6)
        self.assertTrue(all(s.reason == "even" for s in got))

    def test_short_video(self):
        got = P.plan([Cue(0, 2, "只有一句")], 5.0, n=3)
        self.assertEqual(len(got), 3)
        for s in got:
            self.assertLessEqual(s.t, 5.5)

    def test_reasons_are_known(self):
        got = P.plan(dense_block(), 600.0, scenes=[10, 250, 400], n=8)
        self.assertTrue(all(s.reason in ("density", "anchor", "even", "scene") for s in got))

    def test_mmss(self):
        self.assertEqual(P.Spot(0.0, 0, 0.0, "even").mmss, "0:00")
        self.assertEqual(P.Spot(204.7, 0, 0.0, "even").mmss, "3:24")


class TestDescribe(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(P.describe([]), "无")

    def test_kinds(self):
        spots = [P.Spot(10, 0, 1.0, "density"), P.Spot(200, 1, 0.0, "anchor")]
        d = P.describe(spots)
        self.assertIn("2 张", d)
        self.assertIn("density=1", d)
        self.assertIn("0:10", d)


class TestRealisticShape(unittest.TestCase):
    """一段接近真实口播的分布：开头寒暄、中间内容密集、结尾总结。"""

    def test_all_regions_get_covered(self):
        cues, t = [], 0.0
        for i in range(400):
            if 60 <= t < 90:
                txt = "大家好，今天聊聊插件"      # 寒暄，字数少
            elif 90 <= t < 420:
                txt = f"第{i}点，装机量从 1.2 万涨到 37 万，涨了 30 倍。"  # 密集
            else:
                txt = "就这些，谢谢大家。"          # 结尾
            cues.append(Cue(t, t + 2.4, txt))
            t += 3.0
        got = P.plan(cues, t, n=10)
        self.assertEqual(len(got), 10)
        buckets = {int(s.t // (t / 4)) for s in got}
        self.assertGreaterEqual(len(buckets), 3, "四个区间里至少覆盖三个")


if __name__ == "__main__":
    unittest.main(verbosity=2)
