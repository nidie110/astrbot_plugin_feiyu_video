"""字幕解析、清洗、覆盖率校验、分段、渲染。

设计原则（见 docs/watch-v3-design.md 第 5.4 / 5.6 节）：

1. **纯函数**：不碰网络、不碰文件、不读配置。输入是原始文本/对象，输出是
   `SubsDoc`。这样可以用录下来的 JSON 当 fixture 离线单测。
2. **只做必要清洗**：删重复行、删纯语气词串。实词一句不改 —— 用户明确要求
   「字幕不摘要，全文给」，任何改写都会让她把改写过的话当成原话引用。
3. **覆盖率用并集算**，不是「最后一条 / 时长」。后者发现不了中间大段缺失，
   而缺段是最容易被误读成「后面没说」的情况。
4. **渲染成自然段，不是逐行字幕**：逐行排版等于几百个可复制单元，模型会照着
   搬（念稿）；整段引文没有可搬的边界。

对外接口：
    parse_bili_json / parse_srt / parse_vtt / parse_ass / parse_json3
    pick_doc(subtitles) -> dict | None            从 x/player/v2 的 subtitles[] 里挑
    clean_cues(cues) -> list[Cue]                 去重、去噪声
    cover_of(cues, dur) -> Cover                  四个覆盖率指标
    segment(cues, budget) -> list[Segment]        按场景切段
    render(doc, segments=None, declared="full") -> str
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 中文口播 4–5 字/秒；英文按词算约 2.5 词/秒
_CHARS_PER_SEC = 4.5

# 纯语气词：整条 cue 只有这些字就丢掉（不丢含实词的句子）
_FILLER = re.compile(
    r"^[\s，。、？！,.?!…~～·　—\-－]*(?:"
    r"啊|呃|嗯+|哦+|喔|唉|哎+|诶|欸|哈+|嘿|呵|呀|哇|额|"
    r"那个|这个|就是|然后|所以|那么|其实|反正|"
    r"[—\-－~～…]{1,6}"
    r")[\s，。、？！,.?!…~～·　—\-－]*$"
)


@dataclass(frozen=True)
class Cue:
    """一条字幕。start/end 单位秒。"""

    start: float
    end: float
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def mid(self) -> float:
        return (self.start + self.end) / 2.0


@dataclass
class Cover:
    """覆盖率四项指标。"""

    ratio: float = 0.0  # 并集覆盖率（有效 0–1，超 1 说明字幕比视频长）
    max_gap: float = 0.0  # 相邻 cue 之间的最大空洞（秒）
    start: float = 0.0  # 第一条 cue 的起始时刻（秒）
    over: float = 0.0  # 末尾超出的秒数（多 P 合集常见）
    over_ratio: float = 0.0  # 末尾超出占全片时长的比例（判断用这个，短长片才公平）
    n: int = 0  # cue 条数

    @property
    def ok(self) -> bool:
        """够不够格当「完整」用。阈值见 design 5.4。"""
        if self.n == 0:
            return False
        if self.ratio < 0.85:
            return False
        if self.max_gap > 60:
            return False
        if self.start > 10:
            return False
        # 末尾超出按时长比例判，不用固定秒数：3 分钟的短片超 6 秒就是离谱，
        # 2 小时的片超 6 秒根本不算事。
        if self.over_ratio > 0.05:
            return False
        return True

    def reason(self) -> str:
        """给她看的残缺原因（口语，不含技术词）。"""
        if self.n == 0:
            return "没拿到字幕"
        bits = []
        if self.start > 10:
            bits.append(f"开头 {int(self.start)} 秒没有说话声")
        if self.ratio < 0.85:
            bits.append(f"大概只覆盖了前半部分（约 {int(self.ratio * 100)}%）")
        if self.max_gap > 60:
            bits.append(f"中间有 {int(self.max_gap)} 秒是空的")
        if self.over_ratio > 0.05:
            bits.append("尾巴超出了这条视频的长度，可能是合集")
        return "、".join(bits) or "基本完整"


@dataclass
class Segment:
    """渲染用的一个自然段。"""

    start: float
    end: float
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class SubsDoc:
    """一部片子的字幕。"""

    cues: list[Cue] = field(default_factory=list)
    dur: float = 0.0
    lang: str = ""
    source: str = ""  # "ai-zh" / "srt" / "vtt" / "ass" / "json3"
    raw_chars: int = 0

    @property
    def chars(self) -> int:
        return sum(c.chars for c in self.cues)

    @property
    def cover(self) -> Cover:
        return cover_of(self.cues, self.dur)

    @property
    def text(self) -> str:
        return "".join(c.text for c in self.cues)


# ---------------------------------------------------------------- 时间解析


def _t(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s.replace(",", "."))


_HHMMSS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_MMSS = re.compile(r"(\d{1,3}):(\d{2})[,.](\d{1,3})")


def _parse_clock(s: str) -> float | None:
    """`00:01:23,456` / `01:23.456` / `83.4` → 秒。"""
    s = (s or "").strip()
    m = _HHMMSS.search(s)
    if m:
        return _t(m.group(1), m.group(2), f"{m.group(3)}.{m.group(4)}")
    m = _MMSS.search(s)  # VTT 常见：MM:SS.mmm
    if m:
        return _t("0", m.group(1), f"{m.group(2)}.{m.group(3)}")
    try:
        return float(re.sub(r"[^\d.]", "", s) or 0)
    except ValueError:
        return None


# ---------------------------------------------------------------- 各格式解析


def parse_bili_json(raw) -> list[Cue]:
    """B 站字幕正文：`{"body": [{"from": 0.04, "to": 1.38, "content": "..."}]}`。

    注意 `body` 里可能混着非句子条目（ASR 偶尔给空串或纯标点），下游 clean 处理。
    """
    out: list[Cue] = []
    items = raw
    if isinstance(raw, dict):
        items = raw.get("body") or raw.get("body_list") or raw.get("data") or []
        if isinstance(items, dict):
            items = items.get("body") or []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        txt = str(it.get("content") or it.get("text") or "").strip()
        if not txt:
            continue
        try:
            a = float(it.get("from", it.get("start", 0)))
            b = float(it.get("to", it.get("end", a)))
        except (TypeError, ValueError):
            continue
        if b < a:
            a, b = b, a
        out.append(Cue(a, b, txt))
    return out


def parse_srt(text: str) -> list[Cue]:
    """SRT：序号 / `00:00:01,000 --> 00:00:03,500` / 正文（可多行）。"""
    out: list[Cue] = []
    blocks = re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").strip())
    for blk in blocks:
        lines = [ln for ln in blk.split("\n") if ln.strip()]
        if not lines:
            continue
        idx = 0
        if re.fullmatch(r"\d+", lines[0].strip()):
            idx = 1
        if idx >= len(lines) or "-->" not in lines[idx]:
            continue
        a_s, b_s = lines[idx].split("-->", 1)
        a = _parse_clock(a_s)
        b = _parse_clock(b_s.strip().split(" ")[0])
        if a is None or b is None:
            continue
        body = " ".join(ln.strip() for ln in lines[idx + 1 :]).strip()
        if body:
            out.append(Cue(a, b, body))
    return out


def parse_vtt(text: str) -> list[Cue]:
    """WebVTT：去头部，其余与 SRT 同构（时间用 `.` 分隔毫秒）。"""
    text = (text or "").replace("\r\n", "\n")
    text = re.sub(r"^WEBVTT.*?\n", "", text, count=1, flags=re.S)
    text = re.sub(r"^(NOTE|STYLE|REGION)\b.*?(?=\n\s*\n)", "", text, flags=re.S | re.M)
    text = re.sub(r"<[^>]+>", "", text)  # 内联样式标签
    return parse_srt(text)


def parse_ass(text: str) -> list[Cue]:
    """ASS/SSA：`Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,正文`。"""
    out: list[Cue] = []
    for ln in (text or "").replace("\r\n", "\n").split("\n"):
        if not ln.startswith("Dialogue:"):
            continue
        parts = ln.split(",", 9)
        if len(parts) < 10:
            continue
        a = _parse_clock(parts[1])
        b = _parse_clock(parts[2])
        body = re.sub(r"\{[^}]*\}", "", parts[9]).replace("\\N", " ").strip()
        if a is None or b is None or not body:
            continue
        out.append(Cue(a, b, body))
    return out


def parse_json3(raw) -> list[Cue]:
    """YouTube json3：`{"events": [{"tStartMs":.., "dDurationMs":.., "segs":[{"utf8":".."}]}]}`。"""
    out: list[Cue] = []
    evs = raw.get("events") if isinstance(raw, dict) else None
    if not isinstance(evs, list):
        return out
    for ev in evs:
        if not isinstance(ev, dict) or "segs" not in ev:
            continue
        txt = "".join(str(s.get("utf8", "")) for s in ev.get("segs") or [] if isinstance(s, dict))
        txt = txt.strip()
        if not txt:
            continue
        a = float(ev.get("tStartMs", 0)) / 1000.0
        b = a + float(ev.get("dDurationMs", 0)) / 1000.0
        out.append(Cue(a, b, txt))
    return out


def parse_any(raw, fmt: str = "") -> list[Cue]:
    """按格式名或内容猜着解析。fmt 优先。"""
    fmt = (fmt or "").lower()
    if isinstance(raw, dict) and ("events" in raw):
        return parse_json3(raw)
    if isinstance(raw, dict) and ("body" in raw or "body_list" in raw):
        return parse_bili_json(raw)
    if isinstance(raw, list):
        return parse_bili_json(raw)
    text = str(raw or "")
    if fmt == "vtt" or text.lstrip().startswith("WEBVTT"):
        return parse_vtt(text)
    if fmt == "ass" or "[Script Info]" in text or "Dialogue:" in text:
        return parse_ass(text)
    if "-->" in text:
        return parse_srt(text)
    return []


# ---------------------------------------------------------------- 挑字幕


# 要的优先级：中文 AI 字幕第一，人工中文字幕次之，其它语言再次
_LANG_ORDER = {"zh": 0, "ai-zh": 0, "zh-cn": 0, "zh-hans": 0, "zh-tw": 1, "zh-hant": 1}


def pick_doc(subtitles) -> dict | None:
    """从 `x/player/v2` 的 `data.subtitle.subtitles[]` 里挑一条。

    返回原 dict（含 `subtitle_url`），没挑到返回 None。
    只用白名单里的格式 —— `danmaku` 一律跳过，否则会把弹幕当台词念。
    """
    if not isinstance(subtitles, list):
        return None
    best, best_key = None, None
    for it in subtitles:
        if not isinstance(it, dict):
            continue
        url = str(it.get("subtitle_url") or it.get("url") or "").strip()
        if not url:
            continue
        low = url.lower()
        if "danmaku" in low or "danmu" in low:
            continue
        if not any(x in low for x in ("json", "srt", "vtt", "ass", "/aisubtitle", "subtitle")):
            continue
        lan = str(it.get("lan") or "").lower()
        doc = _LANG_ORDER.get(lan, 9)
        is_ai = ("ai" in lan) or ("ai_subtitle" in low) or bool(it.get("ai_status"))
        key = (doc, 1 if is_ai else 0)  # 同语种里人工字幕优先
        if best_key is None or key < best_key:
            best, best_key = it, key
    return best


# ---------------------------------------------------------------- 清洗


def _norm(t: str) -> str:
    """比较去重用：去掉标点空白。"""
    return re.sub(r"[\s，。、？！,.?!…~～·　「」“”\"'']", "", t or "")


_GAP_COMMA = 0.25
_GAP_STOP = 0.90
_END_PUNCT = "。！？…，、；：,.;:!?"
_SENT_TAIL = "吗呢吧啊啦了么哦哈呀嘛"


def punctuate(cues: list[Cue]) -> list[Cue]:
    """按口语节奏补标点，让 3400 字能读。**只加标点，一个实词都不动。**

    B 站 AI 字幕的 cue 是按停顿切的、之间 gap 常常是 0，整段连读几乎没标点。
    所以判据不是「停顿多久」，而是「这一刻像不像一句话说完」：
      - 收尾是语气词/助词（吗呢吧啊啦了么哦哈）且已攒够字数  → 。
      - 攒够一句话的长度（>=30 字）                            → 。
      - 攒到短语长度（>=12 字）                                → ，
      - 太短就继续拼，不乱塞标点。
    """
    if not cues:
        return []
    out: list[Cue] = []
    n = len(cues)
    run = 0
    for i, c in enumerate(cues):
        text = c.text
        run += c.chars
        if text and text[-1] not in _END_PUNCT:
            gap = (cues[i + 1].start - c.end) if i + 1 < n else 99.0
            tail = text[-1] in _SENT_TAIL
            if i + 1 >= n or gap >= _GAP_STOP or (run >= 12 and tail) or run >= 30:
                text += "。"
                run = 0
            elif run >= 12:
                text += "，"
        out.append(Cue(c.start, c.end, text))
    return out


def clean_cues(cues: list[Cue]) -> list[Cue]:
    """去重 + 去纯语气词。**不改写任何实词**。

    ASR 常见两种噪声：
    1. 相邻条目内容相同或互为前缀（滚动字幕/重复识别）→ 只留最长那条
    2. 整条只有「啊啊啊」「嗯」→ 丢掉
    """
    out: list[Cue] = []
    prev_norm = ""
    for c in sorted(cues, key=lambda x: x.start):
        txt = re.sub(r"\s+", " ", (c.text or "").strip())
        if not txt:
            continue
        if _FILLER.match(txt):
            continue
        n = _norm(txt)
        if not n:
            continue
        # 与上一条完全重复
        if n == prev_norm:
            continue
        # 上一条是本条的前缀（滚动字幕重复）→ 用本条替换上一条
        if out and prev_norm and n.startswith(prev_norm) and len(n) > len(prev_norm):
            out.pop()
            prev_norm = ""
        if not out:
            out.append(Cue(c.start, c.end, txt))
        else:
            last = out[-1]
            # 时间重叠且内容包含 → 合并，取更长
            if c.start < last.end and (n in _norm(last.text) or _norm(last.text) in n):
                longer = txt if len(n) > len(_norm(last.text)) else last.text
                out[-1] = Cue(last.start, max(last.end, c.end), longer)
            else:
                out.append(Cue(c.start, c.end, txt))
        prev_norm = _norm(out[-1].text)
    return out


# ---------------------------------------------------------------- 覆盖率


def cover_of(cues: list[Cue], dur: float = 0.0) -> Cover:
    """算并集覆盖率 / 最大空洞 / 起始偏移 / 末尾超出。"""
    if not cues:
        return Cover()
    cs = sorted(cues, key=lambda c: c.start)
    merged: list[list[float]] = []
    for c in cs:
        if merged and c.start <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], c.end)
        else:
            merged.append([c.start, c.end])
    total = sum(b - a for a, b in merged)
    end = merged[-1][1]
    span = dur if dur > 0 else max(end, 1.0)
    max_gap = 0.0
    for i in range(1, len(merged)):
        max_gap = max(max_gap, merged[i][0] - merged[i - 1][1])
    return Cover(
        ratio=total / span if span > 0 else 0.0,
        max_gap=max_gap,
        start=merged[0][0],
        over=max(0.0, end - span),
        over_ratio=(max(0.0, end - span) / span) if span > 0 else 0.0,
        n=len(cs),
    )


# ---------------------------------------------------------------- 分段


_SENT_END = "。！？!?…"


def segment(cues: list[Cue], target: int = 650, min_chars: int = 300, max_chars: int = 950) -> list[Segment]:
    """把 cue 合成自然段。

    规则：段内不换行；尽量在句末切（句号/问号/感叹号/省略号），
    达不到目标长度就继续拼，超过上限就在最近的句末断。
    """
    if not cues:
        return []
    segs: list[Segment] = []
    buf: list[str] = []
    a = cues[0].start
    b = cues[0].end
    n = 0
    for c in cues:
        buf.append(c.text)
        n += c.chars
        b = c.end
        at_sent = bool(c.text) and c.text[-1] in _SENT_END
        if n >= max_chars or (n >= target and at_sent):
            segs.append(Segment(a, b, "".join(buf)))
            buf, n = [], 0
            a = c.end
    if buf and n >= min_chars:
        segs.append(Segment(a, b, "".join(buf)))
    elif buf and segs:
        # 尾巴太短，并进上一段
        last = segs[-1]
        segs[-1] = Segment(last.start, b, last.text + "".join(buf))
    elif buf:
        segs.append(Segment(a, b, "".join(buf)))
    return segs


# ---------------------------------------------------------------- 渲染


_HEAD_FULL = "这片子你看过了。下面是他们说过的话，一句句都在，用词以这个为准；画面你自己看。"
_HEAD_PART = "这片子你看了一部分。下面是他们说过的话里、跟这次问的有关的那几段，用词以这个为准；没念到的部分不代表没说过。"
_HEAD_NONE = "这次没拿到字幕。"


def render(
    doc: SubsDoc,
    segments: list[Segment] | None = None,
    *,
    declared: str = "full",
    max_chars: int = 0,
) -> str:
    """渲染成自然段连排的引文。

    declared: "full"（全给）/ "part"（节选，会加一句「没念到的不代表没说过」）
    max_chars: >0 时按它截（截在段边界，不切段内）
    """
    if not doc or not doc.cues:
        return _HEAD_NONE
    segs = segments if segments is not None else segment(doc.cues)
    if max_chars and sum(s.chars for s in segs) > max_chars and len(segs) > 1:
        # 超预算：均匀抽段（首尾必留），而不是从头截断——否则她会以为这片子只讲了开头。
        avg = max(1, sum(s.chars for s in segs) // len(segs))
        k = max(2, min(len(segs), max_chars // avg))
        if k < len(segs):
            step = (len(segs) - 1) / (k - 1)
            idx = sorted({min(len(segs) - 1, int(round(i * step))) for i in range(k)})
            segs = [segs[i] for i in idx]
            declared = "part"
    if declared == "part":
        head = _HEAD_PART
    else:
        head = _HEAD_FULL
    body: list[str] = []
    used = 0
    for s in segs:
        if max_chars and used + s.chars > max_chars and body:
            break
        body.append(s.text)
        used += s.chars
    out = [head, ""]
    out.extend(body)
    # 残缺说明放在末尾（不打断正文）
    cov = doc.cover
    if not cov.ok and cov.n:
        out.append("")
        out.append(f"（字幕情况：{cov.reason()}。）")
    return "\n\n".join(out)


# ---------------------------------------------------------------- 校验辅助


def self_check(doc: SubsDoc) -> list[str]:
    """返回可疑项，供日志用（不抛异常）。"""
    warn: list[str] = []
    if not doc.cues:
        return ["没有 cue"]
    if doc.chars == 0:
        warn.append("cue 全是空文本")
    cov = doc.cover
    if cov.ratio > 1.5:
        warn.append(f"覆盖率达到 {cov.ratio:.2f}，疑似多 P 合集串台")
    if cov.max_gap > 120:
        warn.append(f"最大空洞 {cov.max_gap:.0f} 秒")
    # 逐条时长异常：单条超过 30 秒通常是把整段塞进了一条
    long_cues = [c for c in doc.cues if c.end - c.start > 30]
    if len(long_cues) > len(doc.cues) * 0.2:
        warn.append(f"{len(long_cues)} 条 cue 时长超过 30 秒")
    return warn


def estimate_chars(dur: float) -> int:
    """按语速估字幕字数（用于预算与降级判断）。"""
    return max(0, int(dur * _CHARS_PER_SEC))
