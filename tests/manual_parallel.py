# -*- coding: utf-8 -*-
"""真实视频端到端：走插件的 _watch_gather（并行版），打印耗时与产物。

用法：
  PYTHONIOENCODING=utf-8 "C:/Users/33887/AppData/Roaming/uv/tools/astrbot/Scripts/python.exe" tests/manual_parallel.py [BV号]
"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as M  # noqa: E402

DEFAULT_BV = "BV1jhn16GEcv"


def build():
    p = object.__new__(M.FeiyuVideo)
    p.cfg = dict(M.DEFAULT_CONFIG)
    p.cfg["see_timeout"] = 420
    p.data_dir = ROOT / "_probe_data"
    p.data_dir.mkdir(parents=True, exist_ok=True)
    p.cfg_path = p.data_dir / "config.json"
    p.plugin_cfg = {}
    p._temporary_files = set()
    p._video_sem = asyncio.Semaphore(2)
    p._last_frames = {}
    p._recent_links = {}
    p._see_cache = {}
    p.ffmpeg = M.imageio_ffmpeg.get_ffmpeg_exe()
    return p


async def run(url: str):
    p = build()
    ctx = M.wctx_mod.WatchContext(ref=url, how="tool")
    t0 = time.time()
    await p._watch_gather(url, how="tool", ctx=ctx)
    used = time.time() - t0

    print("")
    print("=" * 60)
    print(f"总耗时      {used:.1f}s")
    print(f"brief       {ctx.brief()}")
    print(f"片长        {ctx.dur:.0f}s")
    if ctx.subs is not None:
        print(f"字幕        {len(ctx.subs.cues)} 条 / {ctx.subs.chars} 字 / 覆盖 {ctx.subs.cover.ratio:.2f}")
    else:
        print("字幕        无")
    print(f"帧          {len(ctx.frames)} 张")
    print(f"选点        {len(ctx.plan)} 个")
    print(f"错误        {ctx.errors}")
    return ctx


if __name__ == "__main__":
    bv = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BV
    url = bv if bv.startswith("http") else f"https://www.bilibili.com/video/{bv}"
    asyncio.run(run(url))
