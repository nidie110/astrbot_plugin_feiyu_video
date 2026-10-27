# -*- coding: utf-8 -*-
"""端到端验证：真实视频 → 字幕 → 排版。只用 engine 纯函数，不碰插件类。"""
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from engine import picks, subs  # noqa: E402

BSK = r"C:\Users\33887\.local\bin\bsk.exe"
TARGET = sys.argv[1] if len(sys.argv) > 1 else "https://www.bilibili.com/video/BV1jhn16GEcv/"

JS = r"""(async () => {
  const st = window.__INITIAL_STATE__ || {};
  const v = st.videoData || {};
  const pages = Array.isArray(v.pages) ? v.pages : [];
  const page = pages[0] || {};
  const aid = v.aid || 0, cid = page.cid || v.cid || 0;
  const out = {ok: !!(aid && cid), aid, cid, dur: Math.round(v.duration || 0), title: String(v.title || '').slice(0, 80), subs: []};
  if (!out.ok) return JSON.stringify(out);
  try {
    const r = await fetch('//api.bilibili.com/x/player/v2?aid=' + aid + '&cid=' + cid + '&_t=' + Date.now(), {credentials: 'include'});
    const j = await r.json();
    const list = (((j || {}).data || {}).subtitle || {}).subtitles || [];
    out.subs = list.map(x => ({lan: x.lan, lan_doc: x.lan_doc, subtitle_url: x.subtitle_url, ai: x.ai_status}));
  } catch (e) { out.err = String(e && e.message || e).slice(0, 100); }
  return JSON.stringify(out);
})()"""


def run(*a, timeout=120):
    return subprocess.run(
        [BSK, *a], capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


r = run("session", "start", "--json")
sid = json.loads(r.stdout or "{}").get("session_id")
if not sid:
    print("session 起不来:", (r.stderr or r.stdout)[:200])
    raise SystemExit(1)
print("sid:", sid)
try:
    run("navigate", TARGET, "--session", sid, "--wait-until", "commit")
    time.sleep(12)
    r = run("evaluate", "--session", sid, "--json", JS)
    out = json.loads(r.stdout or "{}")
    raw = out.get("value") or "{}"
    info = json.loads(raw) if isinstance(raw, str) else raw
finally:
    run("session", "stop", sid, timeout=40)

print(f"标题: {info.get('title')}")
print(f"aid={info.get('aid')} cid={info.get('cid')} dur={info.get('dur')}s subs={len(info.get('subs') or [])}")
for x in info.get("subs") or []:
    print(f"  - {x.get(chr(39)+chr(39))}")

print("raw subs:", (info.get("subs") or [])[:1])
pick = subs.pick_doc(info.get("subs") or [])
if not pick:
    print("没有可用字幕（danmaku / 空列表都算没有）")
    raise SystemExit(0)
print("选中:", pick.get("lan"), pick.get("lan_doc"))

u = str(pick.get("subtitle_url") or pick.get("url") or "")
if u.startswith("//"):
    u = "https:" + u
req = urllib.request.Request(u, headers={
    "Referer": "https://www.bilibili.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
})
with urllib.request.urlopen(req, timeout=20) as resp:
    body = resp.read().decode("utf-8", errors="replace")

data = json.loads(body) if body.lstrip().startswith("{") else body
cues = subs.punctuate(subs.clean_cues(subs.parse_any(data, fmt=str(pick.get("lan") or ""))))
doc = subs.SubsDoc(cues=cues, dur=float(info.get("dur") or 0), lang=str(pick.get("lan") or ""),
                   source="ai", raw_chars=len(body))
cov = doc.cover
print(f"字幕: {len(doc.cues)} 条 / {doc.chars} 字 / 覆盖率 {cov.ratio:.2f} / 空洞 {cov.max_gap:.0f}s / ok={cov.ok}")
print("可疑:", subs.self_check(doc) or "无")
for c in cues[:6]:
    print(f"  cue {c.start:6.2f}-{c.end:6.2f} |{c.text}|")

spots = picks.plan(doc.cues, float(info.get("dur") or 0), n=10)
print("选点:", picks.describe(spots))
for sp in spots:
    print(f"   {sp.mmss} score={sp.score:.2f} {sp.reason}")
segs = subs.segment(doc.cues)
print(f"分段: {len(segs)} 段, 长度 {[s.chars for s in segs][:8]}")

for label, budget in (("全文 8000 字", 8000), ("节选 2000 字", 2000)):
    text = subs.render(doc, segs, declared="full", max_chars=budget)
    print(f"\n===== {label} → {len(text)} 字 =====")
    print(text[:700].replace("\n", "⏎"))
    print("……")
    print(text[-400:].replace("\n", "⏎"))
