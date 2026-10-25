# -*- coding: utf-8 -*-
"""feiyu_video 的纯逻辑单测：闸门、媒体识别、URL 归一、裁剪边界。

跑法（必须用 AstrBot 自己的解释器，它才装了 astrbot 包）：

    cd D:\qqaibot\astrbot\data\plugins\astrbot_plugin_feiyu_video
    python -m unittest discover -s tests -t .

不打网络，不碰 bsk。
"""
import importlib
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR.parent))

from PIL import Image  # noqa: E402

main = importlib.import_module("astrbot_plugin_feiyu_video.main")


class _FakeCtx:
    provider_manager = None


def _make(config=None):
    return getattr(main, "FeiyuVideo")(_FakeCtx(), config=config)


class TestGate(unittest.TestCase):
    """闸门是安全边界：放错的链接会被登录态浏览器打开。"""

    @classmethod
    def setUpClass(cls):
        cls.v = _make()

    def test_allows_known_sites(self):
        for url in (
            "https://www.bilibili.com/video/BV1qW411m7rG",
            "https://b23.tv/abc123",
            "https://www.douyin.com/video/6961737553342991651",
            "https://v.douyin.com/iRxYqBv/",
            "https://www.youtube.com/watch?v=x",
            "https://x.test/a.mp4",
        ):
            self.assertTrue(self.v._allowed(url), url)

    def test_rejects_private_and_lookalike(self):
        for url in (
            "http://127.0.0.1:6185/",
            "http://localhost:8080/",
            "http://192.168.1.5/secret",
            "http://10.0.0.8/",
            "http://172.20.3.4/",
            "http://169.254.169.254/latest",
            "https://evil.com/?u=bilibili.com",
            "https://bilibili.com.evil.com/video/BV1",
            "https://evil.com/bilibili.com/video",
            "file:///C:/Windows/win.ini",
            "",
        ):
            self.assertFalse(self.v._allowed(url), url)

    def test_host_suffix_not_substring(self):
        self.assertFalse(self.v._allowed("https://notbilibili.com/video/BV1"))
        self.assertTrue(self.v._allowed("https://m.bilibili.com/video/BV1"))

    def test_user_hint_never_leaks_raw_error(self):
        e = RuntimeError("ERROR: [Douyin] x: Fresh cookies are needed (sign=abc&expires=1)")
        hint = self.v._user_hint(e)
        self.assertIsInstance(hint, str)
        self.assertNotIn("sign=", hint)
        self.assertNotIn("expires=", hint)
        self.assertLessEqual(len(hint), 80)

    def test_user_hint_for_bland_errors(self):
        for e in (RuntimeError("needs-cookies"), TimeoutError("x"), ValueError("nope")):
            self.assertTrue(self.v._user_hint(e))


class TestMediaUrl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = _make()

    def test_media_ext(self):
        for url in ("https://x.test/a.mp4", "https://x.test/a.webm", "https://x.test/a.mov",
                    "https://x.test/a.MP4?x=1"):
            self.assertTrue(self.v._is_media_url(url), url)

    def test_not_media(self):
        # m3u8 是播放列表文本，yt-dlp 才能处理
        for url in ("https://x.test/a.m3u8", "https://www.bilibili.com/video/BV1",
                    "https://x.test/page"):
            self.assertFalse(self.v._is_media_url(url), url)


class TestNormUrl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = _make()

    def test_adds_scheme(self):
        self.assertTrue(self.v._norm_url("v.douyin.com/xyz/").startswith("https://v.douyin.com/"))

    def test_keeps_existing(self):
        self.assertEqual(self.v._norm_url("  https://a.test/x  "), "https://a.test/x")

    def test_platform(self):
        self.assertEqual(self.v._platform("https://b23.tv/x"), "bilibili")
        self.assertEqual(self.v._platform("https://v.douyin.com/x"), "douyin")
        self.assertEqual(self.v._platform("https://a.test/x"), "other")


class TestJsonSlice(unittest.TestCase):
    """bsk --json 前后会混日志行，解析要能切开。"""

    def test_plain_object(self):
        self.assertEqual(main.FeiyuVideo._json_slice('{"a": 1}'), {"a": 1})

    def test_noisy(self):
        raw = 'noise line\n{"sessionId": "abc", "ok": true}\ntail'
        self.assertEqual(main.FeiyuVideo._json_slice(raw)["sessionId"], "abc")

    def test_garbage(self):
        self.assertEqual(main.FeiyuVideo._json_slice("nothing here"), {})


class TestCrop(unittest.TestCase):
    """元素比视口大时取交集，不能整块跳过，也不能裁到视口外。"""

    @classmethod
    def setUpClass(cls):
        cls.v = _make()

    def test_crop_intersection(self):
        with tempfile.TemporaryDirectory() as td:
            shot = Path(td) / "s.png"
            Image.new("RGB", (2000, 1000), "white").save(shot)
            # 视口 1000x500（CSS），元素从 y=-100 到 y=900 → 只能裁 0..500
            out = self.v._crop_video_area(str(shot), {"x": 10, "y": -100, "w": 900, "h": 1000,
                                                     "iw": 1000, "ih": 500}, Path(td) / "c0.jpg")
            self.assertIsNotNone(out)
            # scale = 2000/1000 = 2：x 10..910 → 1800px，y 0..500 → 1000px
            with Image.open(out) as im:
                self.assertEqual((im.width, im.height), (1800, 1000))

    def test_crop_skips_tiny(self):
        with tempfile.TemporaryDirectory() as td:
            shot = Path(td) / "s.png"
            Image.new("RGB", (800, 600), "white").save(shot)
            out = self.v._crop_video_area(str(shot), {"x": 0, "y": 0, "w": 40, "h": 30,
                                                     "iw": 800, "ih": 600}, Path(td) / "c0.jpg")
            self.assertEqual(out, str(shot))

    def test_crop_off_screen(self):
        with tempfile.TemporaryDirectory() as td:
            shot = Path(td) / "s.png"
            Image.new("RGB", (800, 600), "white").save(shot)
            out = self.v._crop_video_area(str(shot), {"x": 5000, "y": 5000, "w": 800, "h": 600,
                                                     "iw": 800, "ih": 600}, Path(td) / "c0.jpg")
            self.assertEqual(out, str(shot))


if __name__ == "__main__":
    unittest.main()
