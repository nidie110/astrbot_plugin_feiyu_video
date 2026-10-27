# -*- coding: utf-8 -*-
"""engine/subs.py 的离线单测：解析 / 清洗 / 覆盖率 / 分段 / 渲染 / 挑字幕。

跑法（纯标准库，不需要 AstrBot）：

    cd D:\\qqaibot\\astrbot\\data\\plugins\\astrbot_plugin_feiyu_video
    python -m unittest tests.test_subs -v
"""
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from engine import subs as S  # noqa: E402


def bili_json(items):
    return {"body": [{"from": a, "to": b, "content": t} for a, b, t in items]}


class TestParse(unittest.TestCase):
    def test_bili_json_basic(self):
        cues = S.parse_bili_json(bili_json([(0.0, 1.4, "大家好"), (1.4, 3.0, "今天聊插件")]))
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[1].text, "今天聊插件")
        self.assertAlmostEqual(cues[0].end, 1.4)

    def test_bili_json_empty_and_bad(self):
        self.assertEqual(S.parse_bili_json({}), [])
        self.assertEqual(S.parse_bili_json({"body": []}), [])
        # 缺字段 / 时间非法 → 跳过，不抛
        got = S.parse_bili_json({"body": [{"content": "有字没时间"}, {"from": 1, "to": 2}]})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "有字没时间")

    def test_bili_json_reversed_time(self):
        cues = S.parse_bili_json(bili_json([(5.0, 2.0, "倒的")]))
        self.assertLessEqual(cues[0].start, cues[0].end)

    def test_srt(self):
        txt = (
            "1\n00:00:01,000 --> 00:00:03,500\n第一句\n\n"
            "2\n00:00:03,500 --> 00:00:06,000\n第二句\n换行也算\n"
        )
        cues = S.parse_srt(txt)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start, 1.0)
        self.assertAlmostEqual(cues[0].end, 3.5)
        self.assertIn("换行也算", cues[1].text)

    def test_vtt(self):
        txt = "WEBVTT\n\n00:01.000 --> 00:03.000\nhello\n\n00:03.000 --> 00:05.500\nworld\n"
        cues = S.parse_vtt(txt)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start, 1.0)
        self.assertAlmostEqual(cues[1].end, 5.5)

    def test_ass(self):
        txt = (
            "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
            "Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,{\\i1}第一句\n"
            "Dialogue: 0,0:00:03.50,0:00:06.00,Default,,0,0,0,,第二句\\N续\n"
        )
        cues = S.parse_ass(txt)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "第一句")
        self.assertIn("续", cues[1].text)

    def test_json3(self):
        raw = {
            "events": [
                {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "hi "}, {"utf8": "there"}]},
                {"tStartMs": 3500, "dDurationMs": 1500, "segs": [{"utf8": "again"}]},
            ]
        }
        cues = S.parse_json3(raw)
        self.assertEqual(cues[0].text, "hi there")
        self.assertAlmostEqual(cues[0].start, 1.0)
        self.assertAlmostEqual(cues[1].end, 5.0)

    def test_parse_any_dispatch(self):
        self.assertEqual(len(S.parse_any({"body": [{"from": 0, "to": 1, "content": "x"}]})), 1)
        self.assertEqual(len(S.parse_any("1\n00:00:01,000 --> 00:00:02,000\ny\n")), 1)
        self.assertEqual(S.parse_any("乱七八糟的一句话"), [])

    def test_clock(self):
        self.assertAlmostEqual(S._parse_clock("00:01:23,456"), 83.456)
        self.assertAlmostEqual(S._parse_clock("01:23.456"), 83.456)
        self.assertAlmostEqual(S._parse_clock("83.4"), 83.4)


class TestPickDoc(unittest.TestCase):
    def test_prefers_chinese_ai(self):
        got = S.pick_doc(
            [
                {"lan": "ai-en", "subtitle_url": "//x/ai_subtitle/en.json"},
                {"lan": "ai-ja", "subtitle_url": "//x/ai_subtitle/ja.json"},
                {"lan": "ai-zh", "subtitle_url": "//x/ai_subtitle/zh.json"},
            ]
        )
        self.assertEqual(got["lan"], "ai-zh")

    def test_manual_beats_ai_within_same_lang(self):
        got = S.pick_doc(
            [
                {"lan": "ai-zh", "subtitle_url": "//x/ai_subtitle/zh.json", "ai_status": 2},
                {"lan": "zh-CN", "subtitle_url": "//x/subtitle/manual.srt"},
            ]
        )
        self.assertIn("manual", got["subtitle_url"])

    def test_rejects_danmaku(self):
        got = S.pick_doc(
            [
                {"lan": "ai-zh", "subtitle_url": "//x/danmaku/a.xml"},
                {"lan": "ai-zh", "subtitle_url": "//x/ai_subtitle/zh.json"},
            ]
        )
        self.assertIn("ai_subtitle", got["subtitle_url"])

    def test_only_danmaku_returns_none(self):
        self.assertIsNone(S.pick_doc([{"lan": "ai-zh", "subtitle_url": "//x/danmaku/a.xml"}]))

    def test_empty(self):
        self.assertIsNone(S.pick_doc([]))
        self.assertIsNone(S.pick_doc(None))


class TestClean(unittest.TestCase):
    def test_drop_filler(self):
        cues = [S.Cue(0, 1, "嗯"), S.Cue(1, 2, "啊——"), S.Cue(2, 3, "这个插件真能用")]
        got = S.clean_cues(cues)
        self.assertEqual([c.text for c in got], ["这个插件真能用"])

    def test_drop_exact_duplicate(self):
        cues = [S.Cue(0, 1, "大家好"), S.Cue(1, 2, "大家好"), S.Cue(2, 3, "今天聊插件")]
        got = S.clean_cues(cues)
        self.assertEqual([c.text for c in got], ["大家好", "今天聊插件"])

    def test_rolling_prefix_keeps_longer(self):
        cues = [S.Cue(0, 1, "今天"), S.Cue(1, 2, "今天聊"), S.Cue(2, 3, "今天聊插件")]
        got = S.clean_cues(cues)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "今天聊插件")

    def test_keep_real_words(self):
        """实词一句不改 —— 不是摘要，不是改写。"""
        cues = [S.Cue(0, 1, "他说复利是 27%，不是 12%")]
        self.assertEqual(S.clean_cues(cues)[0].text, "他说复利是 27%，不是 12%")


class TestCover(unittest.TestCase):
    def test_full(self):
        cues = [S.Cue(t, t + 1.9, "x") for t in range(0, 60)]
        # 0.5 容差内相邻会合并，60 条约覆盖 0..59.9
        c = S.cover_of(cues, 60.0)
        self.assertGreater(c.ratio, 0.85)
        self.assertTrue(c.ok)

    def test_middle_gap(self):
        cues = [S.Cue(0, 10, "x"), S.Cue(100, 110, "y")]
        c = S.cover_of(cues, 120.0)
        self.assertAlmostEqual(c.max_gap, 90.0)
        self.assertFalse(c.ok)
        self.assertIn("空的", c.reason())

    def test_overrun(self):
        cues = [S.Cue(0, 10, "x"), S.Cue(10, 400, "y")]
        c = S.cover_of(cues, 120.0)
        self.assertGreater(c.over, 200)
        self.assertFalse(c.ok)

    def test_late_start(self):
        cues = [S.Cue(30, 40, "x")]
        c = S.cover_of(cues, 100.0)
        self.assertFalse(c.ok)
        self.assertIn("开头", c.reason())

    def test_empty(self):
        c = S.cover_of([], 100.0)
        self.assertEqual(c.n, 0)
        self.assertFalse(c.ok)
        self.assertEqual(c.reason(), "没拿到字幕")


class TestSegmentRender(unittest.TestCase):
    def _doc(self, n=80, dur=None):
        cues = []
        t = 0.0
        for i in range(n):
            txt = f"这是第{i}句话，内容差不多长。"
            cues.append(S.Cue(t, t + 3, txt))
            t += 3
        doc = S.SubsDoc(cues=cues, dur=dur or t, source="ai-zh")
        return doc

    def test_segment_covers_all_text(self):
        doc = self._doc()
        segs = S.segment(doc.cues)
        joined = "".join(s.text for s in segs)
        self.assertEqual(joined, "".join(c.text for c in doc.cues))
        # 每段不离散
        self.assertTrue(all(200 <= s.chars <= 1000 for s in segs))

    def test_render_full_has_header_and_no_timestamps(self):
        doc = self._doc(40)
        out = S.render(doc)
        self.assertIn("第0句", out)
        self.assertIn("一句句都在", out)
        # 不能出现时间码（设计 5.6 / 5.7：默认结构上不给时间）
        self.assertNotRegex(out, r"\d{1,2}:\d{2}")

    def test_render_part_header(self):
        doc = self._doc(40)
        out = S.render(doc, declared="part")
        self.assertIn("没念到的部分不代表没说过", out)

    def test_render_empty(self):
        self.assertEqual(S.render(S.SubsDoc()), "这次没拿到字幕。")

    def test_render_budget(self):
        doc = self._doc(120)
        full = S.render(doc)
        cut = S.render(doc, max_chars=800)
        self.assertLess(len(cut), len(full))
        # 截在段边界，不留半句
        self.assertTrue(cut.rstrip().endswith("。") or cut.rstrip().endswith("）"))

    def test_render_warns_when_partial(self):
        doc = self._doc(10)
        doc.dur = 300.0  # 只覆盖 30 秒
        out = S.render(doc)
        self.assertIn("字幕情况", out)


class TestSelfCheck(unittest.TestCase):
    def test_flags_overrun(self):
        doc = S.SubsDoc(cues=[S.Cue(0, 500, "x")], dur=100)
        self.assertTrue(any("多 P" in w or "覆盖" in w for w in S.self_check(doc)))

    def test_clean_doc_no_warn(self):
        cues = [S.Cue(t, t + 2, "内容") for t in range(0, 100, 3)]
        doc = S.SubsDoc(cues=cues, dur=100)
        self.assertEqual(S.self_check(doc), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
