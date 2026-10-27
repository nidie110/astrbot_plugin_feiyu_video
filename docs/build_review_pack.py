# -*- coding: utf-8 -*-
"""把字幕这块的证据和代码拼成一个可以直接丢给外部的评审包。"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUT = DOCS / "review-pack.md"

WANT_FUNCS = [
    "_bili_probe", "_subs_doc", "_frames_by_plan", "_watch_gather",
    "_subs_text", "_inject_subs", "_watch_hint", "_watch_result",
    "see_frames_with_llm",
]


def methods_of(src: str, names: list[str]) -> str:
    """按 def 名把方法整段切出来。"""
    lines = src.splitlines()
    starts = []
    for i, ln in enumerate(lines):
        m = re.match(r"\s{4}(?:async )?def (\w+)\(", ln)
        if m and m.group(1) in names:
            starts.append((i, m.group(1)))
    out = []
    for n, (i, name) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        # 往回带上装饰器和注释
        top = i
        while top > 0 and (
            lines[top - 1].strip().startswith("@")
            or lines[top - 1].strip().startswith("#")
            or lines[top - 1].strip().startswith('"""')
        ):
            top -= 1
        out.append("\n".join(lines[top:end]).rstrip())
    return "\n\n".join(out)


doc = (DOCS / "watch-v3-design.md").read_text(encoding="utf-8")
subs = (ROOT / "engine" / "subs.py").read_text(encoding="utf-8")
picks = (ROOT / "engine" / "picks.py").read_text(encoding="utf-8")
wctx = (ROOT / "engine" / "watchctx.py").read_text(encoding="utf-8")
main_src = (ROOT / "main.py").read_text(encoding="utf-8")
main_part = methods_of(main_src, WANT_FUNCS)
schema = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")

parts = [
    "# 评审包：让 bot「看视频时听得见话」的实现（AstrBot 插件 feiyu_video 1.4.0）",
    "",
    "> 这是一个 AstrBot 插件的一次功能升级。用一句话说清要解决的问题：",
    "> 以前 bot 看 B 站/抖音链接时只能抽几帧画面，**听不见人说话**；现在要同时拿到",
    "> ①整段台词（B站 AI 字幕，不摘要）②按台词密度挑出来的画面帧，两样一起喂给多模态模型，",
    "> 并且**拿不到什么就如实说什么**（不许编）。",
    "",
    "## 一、先看这份事实清单（都是本机实测，不是估算）",
    "",
    "| 项 | 实测值 |",
    "|---|---|",
    "| 480p 下整片 25.7 分钟 | 73 MB / **45 秒**（1589 KB/s） |",
    "| 480p 下整片 16.9 分钟口播 | 22 MB / 20 秒（1175 KB/s） |",
    "| 前 120 秒片段 | 5.17 MiB / **4 秒** |",
    "| 音频流 64 kbps 1 小时 | 28.8 MB（约 2.5 分钟） |",
    "| 抽 30 帧 | 6.6 秒 |",
    "| 全片切镜扫描 | 7.3 秒（211× realtime） |",
    "| 字幕正文（BV1jhn16GEcv） | 44881 字节 / 367 条 / 3407 字 / 覆盖率 0.99 / 1 秒内取回 |",
    "| 字幕覆盖率抽样（热门 12 条） | 9/12 有中文 AI 字幕（75%） |",
    "",
    "## 二、设计文档全文",
    "",
    "```markdown",
    doc.strip(),
    "```",
    "",
    "## 三、engine/subs.py（字幕：解析 / 清洗 / 补标点 / 覆盖率 / 分段 / 渲染）",
    "",
    "```python",
    subs.strip(),
    "```",
    "",
    "## 四、engine/picks.py（按台词密度选帧）",
    "",
    "```python",
    picks.strip(),
    "```",
    "",
    "## 五、engine/watchctx.py（这一轮到底拿到了什么）",
    "",
    "```python",
    wctx.strip(),
    "```",
    "",
    "## 六、main.py 里真正干活的那几个方法（IO 适配层，纯函数在上面的 engine 里）",
    "",
    "```python",
    main_part.strip(),
    "```",
    "",
    "## 七、配置项（plugin 的 _conf_schema.json，节选）",
    "",
    "```json",
    schema.strip(),
    "```",
    "",
    "## 八、我想被挑刺的地方（按重要性）",
    "",
    "1. **`subs.punctuate()` 的断句规则**：AI 字幕整段没有标点，现在按「收尾是 `吗呢吧啊啦了么哦哈呀嘛` 且",
    "   攒够 ≥12 字 → 断句；攒够 ≥12 字 → 逗号；否则直接拼」。纯规则、零成本，但位置不一定对",
    "   （实测会出现「今天的主角9月19号动工，一个月，七个仓库。」这种）。有没有更好的做法？",
    "   用模型重新标点会改写风险 + 成本，值不值？",
    "2. **`picks.plan()` 的权重是拍的**：`0.5·z(字数密度) + 0.15·z(cue频率) + 0.2·z(数字/专名) + 0.15·z(切镜)`，",
    "   20 秒滑窗，一半给密度峰、一半保底锚点，相邻帧有最小间距。**没有标注数据验证过**，",
    "   也没有「这样选出来的帧是不是比等间隔更有用」的对照实验。",
    "3. **时钟安排**：钩子里串行 `探测(60s) → 字幕(25s) → 下载+抽帧(330s)`，但整个钩子由",
    "   `see_timeout=180s` 卡着。25 分钟片实测 45 秒能下完，可长片（>1 小时）会超时 → 整轮失败。",
    "   设计文档里写了「两段式」（先给前 300 秒答一句、后台补全再主动发一条），**这一版没做**。",
    "4. **超预算时是「均匀抽段」而不是「按问题挑段」**：6000 token 预算下，长片会等距取段，",
    "   首尾必留。设计文档 5.6 写的是「按用户问题挑相关段」，实现退化了。",
    "5. **字幕进上下文的通道**：走 `req.extra_user_content_parts` 里的 `TextPart(...).mark_as_temp()`，",
    "   依据是 `astrbot/core/agent/message.py:338` 落库时 `if item.get(\"_no_save\"): continue`、",
    "   `mark_as_temp()` 内部就是 `self._no_save = True`（第 68-70 行）。但 `tool_loop_agent_runner.py:1508/1540`",
    "   **每一步工具调用都会重发 extra_user_content_parts**——同一条消息里她如果连调 3 次工具，",
    "   字幕会被重发 3 次。这个成本账我按「一轮只调一次」估的，没实测。",
    "6. **没做的三块**：ASR 兜底（没字幕就只能给画面）、AI 选帧（第一版砍了）、",
    "   时间戳按需（现在结构上不给时间，只给内容和相对位置词）。",
    "",
    "## 九、已知的坑（都是踩出来的，写进注释了）",
    "",
    "- `x/player/v2` 有 CDN 缓存：不加 `_t=<时间戳>` 会拿到**上一条视频**的字幕（实测覆盖时长比本片还长）。",
    "- bsk（浏览器自动化 CLI）会复用旧标签页：拿到的 `bvid` 必须和目标的 BV 对一遍，对不上就当没有字幕——",
    "  宁可没字幕，不能张冠李戴。",
    "- 字幕正文要后台 HTTP + `Referer: https://www.bilibili.com/`，在视频页里 `fetch()` 会被 CORS 挡（`Failed to fetch`）。",
    "- `--download-sections` 一次只能给一个区间，不支持逗号分隔多区间；而且产物是 ffmpeg 重切过的独立流、",
    "  时间轴归零，**拿不到原始分片偏移 → 不能续接下载**。",
    "- `--concurrent-fragments 4` 反而慢一倍（32s vs 17s）。",
]

OUT.write_text("\n".join(parts) + "\n", encoding="utf-8")

# ---- 拆成两个文件：A 先贴（问题与事实），B 要代码时再贴 ----
i3 = parts.index("## 三、engine/subs.py（字幕：解析 / 清洗 / 补标点 / 覆盖率 / 分段 / 渲染）")
i8 = parts.index("## 八、我想被挑刺的地方（按重要性）")
A = parts[:i3] + parts[i8:]
B = parts[i3:i8]

OUT_A = DOCS / "review-pack-A（先贴这个）.md"
OUT_B = DOCS / "review-pack-B（代码附录）.md"
OUT_A.write_text("\n".join(A) + "\n", encoding="utf-8")
OUT_B.write_text(
    "# 代码附录（接在 A 后面贴，或单独贴）\n\n" + "\n".join(B) + "\n", encoding="utf-8"
)

for f in (OUT, OUT_A, OUT_B):
    n = len(f.read_text(encoding="utf-8"))
    print(f"{f.name:36s} {f.stat().st_size/1024:6.0f} KB  {n:7d} 字")
