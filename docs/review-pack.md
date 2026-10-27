# 评审包：让 bot「看视频时听得见话」的实现（AstrBot 插件 feiyu_video 1.4.0）

> 这是一个 AstrBot 插件的一次功能升级。用一句话说清要解决的问题：
> 以前 bot 看 B 站/抖音链接时只能抽几帧画面，**听不见人说话**；现在要同时拿到
> ①整段台词（B站 AI 字幕，不摘要）②按台词密度挑出来的画面帧，两样一起喂给多模态模型，
> 并且**拿不到什么就如实说什么**（不许编）。

## 一、先看这份事实清单（都是本机实测，不是估算）

| 项 | 实测值 |
|---|---|
| 480p 下整片 25.7 分钟 | 73 MB / **45 秒**（1589 KB/s） |
| 480p 下整片 16.9 分钟口播 | 22 MB / 20 秒（1175 KB/s） |
| 前 120 秒片段 | 5.17 MiB / **4 秒** |
| 音频流 64 kbps 1 小时 | 28.8 MB（约 2.5 分钟） |
| 抽 30 帧 | 6.6 秒 |
| 全片切镜扫描 | 7.3 秒（211× realtime） |
| 字幕正文（BV1jhn16GEcv） | 44881 字节 / 367 条 / 3407 字 / 覆盖率 0.99 / 1 秒内取回 |
| 字幕覆盖率抽样（热门 12 条） | 9/12 有中文 AI 字幕（75%） |

## 二、设计文档全文

```markdown
# feiyu_video 看视频 v3 方案设计（字幕 + 全片下载 + 确定性抽帧）

> v3.1，2026-10-27。已吸收外部评审意见（见第 9 节逐条处理）。
> 数据全部本机实测。

---

## 1. 背景：这个插件是给谁的

「大肥鱼」是一个跑在 QQ 上的 AI 人格（AstrBot 4.29.1 + NapCat），底层模型 `deepseek-flash`（多模态，能收图）。

对模型只暴露一个入口：`feiyu(action, target, text, question)`。

用户在 QQ 里发「看看这个 BV1xxx 讲了啥」，她调用 `feiyu(action="看视频", target="<链接>")`，拿结果用自己的语气回答。

**核心矛盾**：用户在 QQ 里等不了太久（超时 90 秒），但她拿到的东西必须够她「真的聊得起来」——现在只看到 3 张截图，被问「他说的那个数字是多少」就答不出，甚至编。

## 2. 现状（1.3.0）的两条路径与短路

现在有两个入口，**各自取一次帧，互不知道对方做了什么**：

| 路径 | 位置 | 行为 |
|---|---|---|
| hook | `main.py:650-690` `on_llm_request` | 检测到视频引用 → 取帧 → 塞 `extra_user_content_parts` → `event.set_extra("feiyu_saw_frames", True)` |
| 工具 | `main.py:797-800` `do_watch` | 开头 `if event.get_extra("feiyu_saw_frames"): return "画面已经在这一轮的请求里了"` |

**这是 v3 必须先解决的死结**：如果只在 tool 路径里加字幕，hook 路径会把整个 tool 短路掉，字幕永远进不来；反过来只在 hook 里加，tool 路径又被短路，两条路的产物对不上。

现状还有：只看前 300 秒、只能描述画面、答不出原话与数字。

## 3. 本机实测基线

### 3.1 下载速度（480p，`worstvideo[height<=480]/worstvideo/worst/bv*/b`）

| 视频 | 时长 | 文件大小 | 下载耗时 | 速度 |
|---|---|---|---|---|
| 自驾拉斯维加斯（高码率） | 25.7 分 | 73 MB | **45 秒** | 1589 KB/s |
| DeepSeek 实测（口播） | 16.9 分 | 22 MB | **20 秒** | 1175 KB/s |
| 大肥鱼建造故宫 | 4.8 分 | 16 MB | **15 秒** | 1149 KB/s |

- `--concurrent-fragments 4` **反而慢一倍**（32 秒 vs 17 秒），不要用
- `--download-sections` 一次只能一个区间，**不支持逗号分隔多区间**
- 曾测出 163 KB/s（25 分钟片 7 分 24 秒），重测证明是网络波动，**同一视频两次测能差 3 倍**

**结论**：下载时间跟**文件大小**走，不跟时长线性（口播码率只有高码率的一半）。**所有预算必须按 p95 算，不能按平均**。

### 3.2 字幕可得性

**列表**（页面内 fetch，需登录态）：
```
GET //api.bilibili.com/x/player/v2?aid=<aid>&cid=<cid>&_t=<随机>   (credentials: include)
→ data.subtitle.subtitles[] = [{lan, lan_doc, subtitle_url, ai_status}, ...]
```
`_t` 破缓存是必需的——实测漏掉它会拿到上一个视频的字幕（覆盖时长超过视频本身）。

**正文**（后台 HTTP，**不需要 cookie**，`auth_key` 已在 URL 里）：
```
GET <subtitle_url>
Referer: https://www.bilibili.com/
→ {"body": [{"from": 0.04, "to": 1.38, "content": "..."}, ...]}
```
从视频页 fetch 会被 CORS 挡（`aisubtitle.hdslb.com` 是另一个域）。实测 44881 字节 / 267 条 / 1 秒内。

**覆盖率**（热门区 12 条抽样）：

| 结果 | 条数 |
|---|---|
| 有 ai-zh 字幕 | 9 / 12（75%） |
| 正文可取 | 7 |
| 覆盖 98–99.9%（完整） | 4 |
| 只覆盖前 25%（断了） | 1 |
| 算出超过视频时长（多 P 合集） | 2 |

无字幕的：纯音乐 Remix、表情包 63 秒短片、刚发布还没生成的。

### 3.3 其他

| 动作 | 耗时 |
|---|---|
| 抽 30 帧（480p） | 6.6 秒 |
| 全片切镜扫描（211× realtime） | 7.3 秒 / 25 分钟片 |
| 取字幕 | ~1 秒 |
| **页面导航** | **8–30 秒**（B 站视频页 30 秒内常到不了 `load`，要等 `commit` 后再等 SPA 就绪） |

## 4. 用户的硬约束

1. 全下载，不只前 300 秒
2. 根据字幕信息密度截帧；无字幕时等间隔
3. 字幕不摘要，全文给
4. 帧没抽出来也要能说，但必须表明画面看不到
5. 没字幕要说
6. 文本只输出具体信息（原话 / 数字 / 专名）
7. 时间戳重要（他要自己去看时能找到），但可以之后申请得到，也可以别暴露
8. 优先按 AI 字幕方向做

## 5. 方案

### 5.1 统一 watch_context（先解决两条路打架）

```python
@dataclass
class WatchContext:
    ref: str                 # 原始引用（链接 / 本地路径）
    task_id: str             # 本次观看的唯一 id（也用于 Phase B 去重）
    subs: SubsDoc | None     # 字幕（None = 没取到，带 reason）
    subs_cover: float        # 覆盖率（区间并集 / 时长）
    frames: list[str]        # 已抽的帧
    plan: list[Spot]         # 抽帧计划（时间点 + 理由）
    phase: str               # "settled" | "subs_only" | "frames_only"
    how: str                 # "hook" | "tool"
```

- 取帧、取字幕都只写这一个对象
- hook 与 tool **消费同一份**：`do_watch` 看到 `phase == "settled"` 才返回「画面和话都在这轮了」，而不是见到一个 bool 就短路
- `phase` 明确告知模型缺什么：
  - `settled` = 字幕 + 帧都有
  - `subs_only` = 只有话，画面没拿到
  - `frames_only` = 只有画面，没拿到字幕

### 5.2 主流程（第一版）

```
链接
 ├─(1) aid/cid：导航到页面（等 commit）→ fetch x/web-interface/view?bvid=
 ├─(2) 字幕列表：fetch x/player/v2?_t=<随机> → 挑 ai-zh
 ├─(3) 字幕正文：后台 HTTP + Referer                    [~1s]
 ├─(4) 覆盖率校验（见 5.4）
 ├─(5) 确定性选点（见 5.5）
 ├─(6) 全片下载 480p                                    [15–45s，p95 按 2× 计]
 ├─(7) 抽帧                                            [~7s]
 └─(8) 组装：字幕 + 帧 → 同一轮注入
```

**端到端预算表**（25 分钟片，p95）：

| 段 | p50 | p95 |
|---|---|---|
| 页面导航 + 拿 aid/cid | 10s | 30s |
| 字幕 | 1s | 3s |
| 下载 | 45s | 90s |
| 抽帧 | 7s | 12s |
| **合计** | **63s** | **135s** |

→ **p95 会超 90 秒超时**。因此门控不能看时长，要看**预估下载量**：`bytes_est / 1 MB/s > 60s` 就进两段式。

### 5.3 字幕怎么进上下文

AstrBot 提供两条等价通道：

```python
from astrbot.core.agent.message import TextPart
part = TextPart(text=...).mark_as_temp()   # mark_as_temp 内部就是 self._no_save = True
req.extra_user_content_parts.append(part)
```

**已核对源码**（`astrbot/core/agent/message.py`）：
- `ContentPart.mark_as_temp()` 存在（第 68 行），返回自身
- 它设的 `_no_save`（第 70 行）就是现有代码手动写的那个字段
- 落库时跳过：`message.py:338` `if item.get("_no_save"): continue`

**必须做的集成测试**（四条）：
1. 文本确实进入 `extra_user_content_parts`
2. 不进入会话历史（下一轮 `get_history()` 里没有）
3. 同一请求内不重复注入（hook 可能被调多次）
4. `system_prompt` 每轮不被改写（现状 `main.py:677` 会追加动态说明）

**不能拼进 `req.prompt`**——会落库，之后每轮重发。

### 5.4 覆盖率校验（比「最后一条 / 时长」强）

`last.to / dur` 发现不了中间大段缺失。改为算：

| 指标 | 算法 | 判据 |
|---|---|---|
| **并集覆盖率** | 所有 cue 区间并集 / `dur` | ≥ 0.85 视为完整 |
| **最大空洞** | 相邻 cue 的最大间隔 | > 60s 判缺段 |
| **起始偏移** | 第一条 `.from` | > 10s 判开头缺 |
| **末尾超出** | `last.to - dur` | > 5% 判多 P 或异常 |
| **多 P 处理** | `pages[]` 长度 > 1 时按 `cid + page` 分开 | 只处理用户指定的那一 P，或按页面顺序拼接并记录偏移 |

**残缺时的语义**：不是「后半段没说」，是「后半段没拿到」。措辞必须区分。

### 5.5 确定性选点（第一版不用 AI）

```
S_i = 0.5·z(字幕字数密度) + 0.15·z(cue 频率) + 0.2·z(关键词/数字/专名) + 0.15·z(切镜)
```
滑窗 20s，取局部峰值。

约束：
- **保底锚点 50%**：等间隔铺满全片，避免全挤在一段演讲里
- **相邻帧最小间距 2w**
- 取帧时刻取该桶内 cue 字数最多那条的**中点**（帧和台词对得上）

退化：
- 无字幕 → 只留切镜项
- 切镜也没有 → 完全退等间隔（现状行为）

**AI 选点不进第一版**：会多一次模型调用（延迟 + 可能超时）、可能生成无效时间。留作 v3.2，且只允许「在给定候选窗口里返回编号」，禁止自由生成时间。

### 5.6 字幕的呈现形态

**不是 SRT，不是逐行带时间戳的表**。是**自然段连排的中文引文**：

```
这片子你看过了。下面是他们说过的话，一句句都在，用词以这个为准；画面你自己看。

（段一，400–900 字，段内不换行，对白用「」，无时间无序号无标题）

（段二，同上）
```

- 只删 ASR 重复行与纯语气词串，实词一句不改
- 逐行排版 = 几百个可复制单元 → 模型照着搬（念稿）；整段引文没有可搬的边界

**两层语义（解决「全文」与「分段」的冲突）**：

| 层 | 内容 | 生命周期 |
|---|---|---|
| **落盘** | 全文字幕写 `subs/<vid>.txt` | 永久，可回查 |
| **当轮注入** | 按 token 预算给相关片段 | 只这一轮 |

- 预算按 **token** 算，不按字符（中文 1 字 ≈ 0.6 token，别用字符数拍）
- ≤ 6000 token：全文
- 超出：给首段 + 被问到的那几段 + 尾段，**并明确告诉她「这是节选，要别的段说一声」**
- 用户说「继续 / 上一段 / 第几段」→ 分页取原文

**关键**：不能让她误以为自己已经看完全文。

### 5.7 时间戳（由插件生成，不靠出站正则）

| 层 | 她上下文里有什么 | 触发 |
|---|---|---|
| **L0 默认** | **什么都没有**。只有内容锚 + 相对位置 | 一直 |
| **L1 问「几分几秒」** | 单点 `3:24` + 可点链接 `?t=204` | 显式询问 |

- 值由插件从 `plan[i].t` 生成，**不是模型自己算**
- 该轮在 `event` 上打结构化标记 `time_authorized = True`，出站时只做「有没有授权」的判断，**不用正则去猜**（`3:24` 可能是视频里的内容、用户原话、普通时长，正则必误伤）
- L2 值只给一次，说完不再重复
- 参数名不带「时间 / 秒 / 戳」（叫「定位」「掐点」）

### 5.8 「没拿到字幕」怎么说

**不能说「这次没声」**——没有字幕不等于没有音频。

> 这次没拿到字幕，我只能按画面说。

帧失败但字幕有：

> 这段我只有话，画面没跟上——他原话是「三万二」，人在干嘛我没看见。

规则：一句交代 → 立刻给替代价值 → 不解释技术原因。同一条消息提第二遍「没看到」算失败。

### 5.9 长片两段式（带状态机）

```python
@dataclass
class WatchTask:
    task_id: str
    fingerprint: str      # BV + cid，用于去重
    session: str          # unified_msg_origin
    phase: str            # "subs_sent" | "downloading" | "done" | "failed" | "cancelled"
    sent_result: bool     # 保证只发一次
```

- Phase A：字幕到手即结束当轮（1 秒），她立刻能聊内容
- Phase B：后台下载 → 帧到手 → **只发「新增的画面事实」**，不重讲一遍
- 去重：同一 `fingerprint` 在 `downloading` 或 `done` 时不重复建档
- 取消：用户在 Phase B 之前又发了新视频 → 旧任务标 `cancelled`
- 失败：标 `failed`，不重试，不打扰
- **`sent_result` 保证只发一次**（进程重启、并发两群都要安全）

## 6. 模块划分

```
engine/subs.py        纯函数：解析（json3/srt/vtt/ass）、清洗、覆盖率校验、分段
engine/picks.py       纯函数：密度公式 + 保底锚点 + 最小间距 + 合并去重
engine/watchctx.py    WatchContext / WatchTask 数据结构 + 状态机
main.py               IO 适配：bsk 导航、fetch 字幕、yt-dlp 下载、ffmpeg 抽帧
```

纯函数层不碰网络、不碰文件，可离线单测（用录下来的字幕 JSON 当 fixture）。

## 7. 降级阶梯

| 级 | 条件 | 行为 |
|---|---|---|
| L1 | 字幕完整 + 帧成功 | 正常：字幕 + 帧 |
| L2 | 字幕残缺（覆盖率 < 0.85） | 字幕照给，标注「只覆盖前 X 分钟」，帧走等间隔 |
| L3 | 无字幕 | 明说「这次没拿到字幕，只能按画面说」，帧走切镜 + 等间隔 |
| L4 | 帧失败 | 只要有字幕就继续，明说画面没看到 |
| L5 | 下载超时 | 只给字幕 + 「画面没来得及看」 |
| L6 | 全都失败 | 如实说做不到，不许编 |

## 8. 配置项（拟）

| 键 | 默认 | 说明 |
|---|---|---|
| `watch_mode` | auto | auto / full / preview |
| `see_shots` | 10 | 一次给几帧（原 6） |
| `subs_enable` | true | 是否取字幕 |
| `subs_inject_tokens` | 6000 | 当轮注入的字幕 token 上限 |
| `cover_min_ratio` | 0.85 | 并集覆盖率下限 |
| `cover_max_gap_sec` | 60 | 最大空洞阈值 |
| `download_deadline_sec` | 150 | 下载墙钟上限（超了走 L5） |
| `download_stall_sec` | 60 | 停滞检测 |
| `two_phase_bytes` | 60 | 预估下载秒数超这个值走两段式 |
| `time_marks` | off | off / on_demand |

## 9. 评审意见的处理（逐条）

| # | 意见 | 处理 | 依据 |
|---|---|---|---|
| P0-1 | 主模型路径会绕过字幕（hook 设标记 → tool 短路） | **采纳**。5.1 统一 `WatchContext`，`do_watch` 按 `phase` 判断而非 bool | 核实 `main.py:650-690` 与 `main.py:797-800`，确认存在 |
| P0-2 | `TextPart.mark_as_temp()` 需先验证可用 | **部分不成立，但照做测试**。已核对源码：`mark_as_temp()` 存在于 `core/agent/message.py:68`，内部就是 `self._no_save = True`（第 70 行），与现有代码手动设的是同一件事；落库跳过在 `message.py:338`。5.3 仍列四条集成测试 | 读 AstrBot 源码确认 |
| P1-1 | 「字幕全文」与「超 2 万字只给 3 段」冲突 | **采纳**。5.6 拆两层：落盘永久全文 / 当轮按 token 预算注入节选，且明确告知「这是节选」 | — |
| P1-2 | 覆盖率算法太弱，发现不了中间缺段/多 P | **采纳**。5.4 改为并集覆盖率 + 最大空洞 + 起始偏移 + 末尾超出 + 多 P 分页 | — |
| P1-3 | 长片 Phase A/B 需要状态机 | **采纳**。5.9 `WatchTask`（task_id / fingerprint / phase / sent_result / 取消 / 去重） | — |
| P1-4 | 25–60 秒估算过于乐观 | **采纳**。5.2 加 p50/p95 预算表，结论是 **p95 会超时**，门控改看预估下载量而非时长 | — |
| P1-5 | AI 选帧不应作为主路径 | **采纳**。5.5 第一版只用确定性方案，AI 留 v3.2 且只允许在候选窗口里返回编号 | — |
| P2-1 | 「这次没声」措辞错误 | **采纳**。5.8 改成「没拿到字幕」 | — |
| P2-2 | 时间戳应由插件生成，别靠出站正则 | **采纳**。5.7 改为插件从 `plan[i].t` 生成 + `event` 结构化标记，去掉正则猜测 | — |

## 10. 第一版范围（收缩后）

**做**：
1. 字幕抓取与校验（含破缓存、覆盖率四指标）
2. 确定性密度抽帧（公式 + 保底锚点 + 最小间距）
3. 统一 `WatchContext`，解决 hook/tool 短路
4. 临时注入（`mark_as_temp`）+ 四条集成测试
5. 长片后台补画面（`WatchTask` 状态机）

**不做（留给后续）**：AI 选帧、复杂时间层、ASR。

## 11. 验收清单

| # | 场景 | 期望 |
|---|---|---|
| 1 | 普通 10 分钟片，字幕完整 | 拿到字幕 + 帧，能答出原话与数字 |
| 2 | 25 分钟高码率片 | p95 内完成，或正确降级到两段式 |
| 3 | 无字幕（纯音乐） | 明说没拿到字幕，按画面说 |
| 4 | 字幕中间缺段 | 标注「只覆盖前 X」，不谎称「后面没说」 |
| 5 | 多 P 合集 | 只处理指定 P，或按顺序拼接并记偏移 |
| 6 | 临时注入不落库 | 下一轮 `get_history()` 里没有字幕正文 |
| 7 | hook + tool 同一轮都触发 | 只有一个 `WatchContext`，不重复取帧、不重复下载 |
| 8 | Phase A/B 去重 | 同一视频不重复建档，`sent_result` 只发一次 |
| 9 | 两个群并发看同一个视频 | 各自独立的 `WatchTask`，互不干扰 |
| 10 | 下载中途用户又发新视频 | 旧任务标 `cancelled`，不打扰 |
```

## 三、engine/subs.py（字幕：解析 / 清洗 / 补标点 / 覆盖率 / 分段 / 渲染）

```python
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
        if self.over > 5:
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
        if self.over > 5:
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
```

## 四、engine/picks.py（按台词密度选帧）

```python
"""抽帧选点：按字幕信息密度挑「值得看画面」的时刻。

设计（见 docs/watch-v3-design.md 第 5.5 节）：

    S_i = 0.5·z(字幕字数密度) + 0.15·z(cue 频率) + 0.2·z(关键词/数字/专名) + 0.15·z(切镜)

滑窗 20 秒，取局部峰值，再加两条例保护：

- **保底锚点 50%**：等间隔铺满全片。不这么做的话，一段连续演讲会吸走全部
  名额，片子后半段一帧都没有。
- **相邻帧最小间距 2w**：避免 30 帧全落在同一个镜头的 3 秒内。

退化阶梯（宁可粗，不要错）：
1. 有字幕 + 有切镜 → 四项全用
2. 有字幕、没切镜 → 前三项（切镜项置 0）
3. 没字幕、有切镜 → 只用切镜项
4. 都没有 → 完全等间隔（= 现状行为）

公开接口：
    plan(cues, dur, scenes=None, n=10) -> list[Spot]
    even_spots(dur, n) -> list[Spot]
    density_series(cues, dur, scenes, w) -> list[float]   调试用
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 默认窗口（秒）。20 秒约等于一句话半到两句话，够一个画面事件，又不至于糊成一片。
W = 20.0

# 「信息量大」的标记：数字、钱、比例、英文专名、引号/书名号里的词
_MARK = re.compile(
    r"\d+(?:\.\d+)?%?|\d+[万亿千百]|"
    r"[A-Za-z][A-Za-z0-9_.\-]{2,}|"
    r"《[^》]{1,20}》|「[^」]{1,20}」|\"[^\"]{1,20}\""
)

# 三个权重（第四项切镜权重单独给，因为它可能缺席）
_W_DENSITY = 0.5
_W_FREQ = 0.15
_W_MARK = 0.2
_W_SCENE = 0.15


@dataclass(frozen=True)
class Spot:
    """一个取帧点。"""

    t: float  # 取帧时刻（秒）
    bucket: int  # 所属窗口序号（调试用）
    score: float  # 归一化后的分数
    reason: str  # "density" / "anchor" / "scene" / "even"

    @property
    def mmss(self) -> str:
        s = int(max(0, self.t))
        return f"{s // 60}:{s % 60:02d}"


def _z(vals: list[float]) -> list[float]:
    """z-score 归一化；全相同则全 0（避免除零、也避免把均匀分布当峰值）。"""
    n = len(vals)
    if n == 0:
        return []
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    sd = var**0.5
    if sd < 1e-9:
        return [0.0] * n
    return [(v - mean) / sd for v in vals]


def even_spots(dur: float, n: int) -> list[Spot]:
    """完全等间隔。n 个点落在 (0, dur) 内，首尾各留半个间隔。"""
    n = max(1, int(n))
    dur = max(1.0, float(dur))
    step = dur / n
    return [Spot(t=step * (i + 0.5), bucket=i, score=0.0, reason="even") for i in range(n)]


def density_series(cues, dur: float, scenes=None, w: float = W) -> list[dict]:
    """算出每个窗口的原始指标与合成分。

    返回 `[{"i", "t0", "t1", "n_cue", "chars", "marks", "scenes", "score"}]`，
    方便调试与自己写测试。
    """
    dur = max(1.0, float(dur))
    nb = max(1, int(round(dur / max(1.0, w))))
    w = dur / nb  # 均分，避免最后一段特别短
    rows = [
        {"i": i, "t0": i * w, "t1": (i + 1) * w, "n_cue": 0, "chars": 0, "marks": 0, "scenes": 0}
        for i in range(nb)
    ]

    def _bucket(t: float) -> int:
        return min(nb - 1, max(0, int(t // w)))

    for c in cues or []:
        i = _bucket(c.mid)
        rows[i]["n_cue"] += 1
        rows[i]["chars"] += c.chars
        rows[i]["marks"] += len(_MARK.findall(c.text or ""))

    for s in scenes or []:
        try:
            rows[_bucket(float(s))]["scenes"] += 1
        except (TypeError, ValueError):
            continue

    has_cue = any(r["n_cue"] for r in rows)
    has_scene = any(r["scenes"] for r in rows)

    zd = _z([float(r["chars"]) for r in rows]) if has_cue else [0.0] * nb
    zf = _z([float(r["n_cue"]) for r in rows]) if has_cue else [0.0] * nb
    zm = _z([float(r["marks"]) for r in rows]) if has_cue else [0.0] * nb
    zs = _z([float(r["scenes"]) for r in rows]) if has_scene else [0.0] * nb

    for i, r in enumerate(rows):
        s = 0.0
        if has_cue:
            s += _W_DENSITY * zd[i] + _W_FREQ * zf[i] + _W_MARK * zm[i]
        if has_scene:
            s += _W_SCENE * zs[i]
        r["score"] = s
    return rows


def _best_moment(cues, t0: float, t1: float, fallback: float) -> float:
    """窗口内取「字数最多那条 cue 的中点」——帧和台词才对得上。"""
    inside = [c for c in (cues or []) if t0 <= c.mid < t1]
    if not inside:
        return fallback
    best = max(inside, key=lambda c: (c.chars, -c.start))
    return best.mid


def plan(
    cues,
    dur: float,
    scenes=None,
    n: int = 10,
    w: float = W,
    min_gap_ratio: float = 2.0,
) -> list[Spot]:
    """选 n 个取帧点。

    n: 要几张
    min_gap_ratio: 最小间距 = min_gap_ratio × 窗口宽度（防挤在一起）
    """
    n = max(1, int(n))
    dur = max(1.0, float(dur))
    rows = density_series(cues, dur, scenes, w)
    nb = len(rows)
    win = rows[0]["t1"] - rows[0]["t0"] if rows else w

    # 一个窗口都分不到（片太短）：直接等间隔
    if nb <= 1 or not any(r["score"] for r in rows):
        return even_spots(dur, n)

    # 保底锚点占一半（至少 1 个），密度占另一半（至少 1 个）
    n_anchor = max(1, min(n - 1, int(round(n * 0.5)))) if n > 1 else 0
    n_dense = max(0, n - n_anchor)

    # 密度峰：按分数降序取，跳过离已选点太近的
    # 间距上限是 2 个窗口（防挤在一个镜头里），下限由 n 决定（要 30 张就不能按 40 秒间距排）
    min_gap = min(win * max(1.0, float(min_gap_ratio)), dur / max(1, n) * 0.6)
    picked: list[Spot] = []
    order = sorted(range(nb), key=lambda i: -rows[i]["score"])
    for i in order:
        if len(picked) >= n_dense:
            break
        if rows[i]["score"] <= 0:
            break
        t = _best_moment(cues, rows[i]["t0"], rows[i]["t1"], (rows[i]["t0"] + rows[i]["t1"]) / 2)
        if any(abs(t - p.t) < min_gap for p in picked):
            continue
        picked.append(Spot(t=t, bucket=i, score=rows[i]["score"], reason="density"))

    # 保底锚点：等间隔，跳过离已选点太近的
    for s in even_spots(dur, n_anchor + 1)[1:] if n_anchor else []:
        if any(abs(s.t - p.t) < min_gap * 0.6 for p in picked):
            continue
        picked.append(Spot(t=s.t, bucket=int(s.t // win), score=0.0, reason="anchor"))
        if len(picked) >= n:
            break

    # 还不够 n 张：补等间隔
    if len(picked) < n:
        for s in even_spots(dur, n * 3):
            if len(picked) >= n:
                break
            if any(abs(s.t - p.t) < min_gap * 0.35 for p in picked):
                continue
            picked.append(Spot(t=s.t, bucket=s.bucket, score=0.0, reason="even"))

    picked.sort(key=lambda p: p.t)
    return picked[:n]


def describe(spots: list[Spot]) -> str:
    """给日志用的一行摘要。"""
    if not spots:
        return "无"
    kinds: dict[str, int] = {}
    for s in spots:
        kinds[s.reason] = kinds.get(s.reason, 0) + 1
    detail = " ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
    first_last = f"{spots[0].mmss}…{spots[-1].mmss}"
    return f"{len(spots)} 张（{detail}）{first_last}"
```

## 五、engine/watchctx.py（这一轮到底拿到了什么）

```python
"""观看上下文：统一次 watch（hook 与 tool 两条路）的产物。

解决的问题（design 5.1）：原来 hook 取完帧就 `event.set_extra("feiyu_saw_frames", True)`，
`do_watch` 一看到这个 bool 就 return「画面已经在这一轮的请求里了」——字幕要是加在
任何一边，另一边都被短路掉。

这里改成**一个对象**：谁取的都往里写，消费方按 `phase` 判断缺什么，而不是猜。

    phase = "settled"      字幕 + 帧 都有
          = "subs_only"    只有话，画面没拿到
          = "frames_only"  只有画面，没拿到字幕
          = "nothing"      都没有

`WatchTask` 是长片两段式（design 5.9）的状态机：Phase A 先把字幕给出去，
Phase B 后台下完再补画面，靠 `fingerprint` 去重、`sent_result` 保证只发一次。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .subs import Cover, SubsDoc

# --- 阶段常量 -------------------------------------------------------------

SETTLED = "settled"
SUBS_ONLY = "subs_only"
FRAMES_ONLY = "frames_only"
NOTHING = "nothing"

# WatchTask 的生命周期
T_SUBS_SENT = "subs_sent"
T_DOWNLOADING = "downloading"
T_DONE = "done"
T_FAILED = "failed"
T_CANCELLED = "cancelled"


@dataclass
class WatchContext:
    """一次观看的全部产物。hook 与 tool 共享同一份。"""

    ref: str = ""  # 原始引用（链接 / 本地路径）
    task_id: str = ""
    subs: SubsDoc | None = None
    frames: list[str] = field(default_factory=list)
    plan: list = field(default_factory=list)  # list[picks.Spot]
    dur: float = 0.0
    how: str = "tool"  # "hook" | "tool"
    errors: list[str] = field(default_factory=list)

    @property
    def phase(self) -> str:
        has_subs = bool(self.subs and self.subs.cues)
        has_frames = bool(self.frames)
        if has_subs and has_frames:
            return SETTLED
        if has_subs:
            return SUBS_ONLY
        if has_frames:
            return FRAMES_ONLY
        return NOTHING

    @property
    def cover(self) -> Cover:
        return self.subs.cover if self.subs else Cover()

    def add_error(self, msg: str) -> None:
        if msg and msg not in self.errors:
            self.errors.append(msg)

    def brief(self) -> str:
        """一行摘要，进日志。"""
        cov = self.cover
        return (
            f"phase={self.phase} how={self.how} "
            f"subs={len(self.subs.cues) if self.subs else 0}条/"
            f"{self.subs.chars if self.subs else 0}字 "
            f"cover={cov.ratio:.2f} gap={cov.max_gap:.0f}s "
            f"frames={len(self.frames)} "
            f"err={'|'.join(self.errors) if self.errors else '-'}"
        )


@dataclass
class WatchTask:
    """长片两段式的后台任务。"""

    fingerprint: str  # BV + cid，去重键
    session: str  # unified_msg_origin
    ref: str = ""
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    phase: str = T_SUBS_SENT
    sent_result: bool = False
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    note: str = ""

    def touch(self, phase: str | None = None, note: str = "") -> None:
        if phase:
            self.phase = phase
        if note:
            self.note = note
        self.updated = time.time()

    @property
    def alive(self) -> bool:
        return self.phase in (T_SUBS_SENT, T_DOWNLOADING)

    @property
    def age(self) -> float:
        return time.time() - self.created


class TaskRegistry:
    """进程内的任务表。够用：单机、单进程、量小。

    只保证三件事：同一个 fingerprint 不重复建档；同一会话只留一个在跑的任务；
    老的自动清掉（防止进程跑久了表膨胀）。
    """

    MAX_AGE = 3600.0  # 1 小时后自动清
    MAX_ITEMS = 200

    def __init__(self) -> None:
        self._by_fp: dict[str, WatchTask] = {}

    # ---- 读 ----------------------------------------------------------------

    def get(self, fingerprint: str) -> WatchTask | None:
        t = self._by_fp.get(fingerprint)
        if t and not t.alive and t.age > 600:
            # 已收尾超过 10 分钟，允许重建
            return None
        return t

    def find_session(self, session: str) -> WatchTask | None:
        for t in self._by_fp.values():
            if t.session == session and t.alive:
                return t
        return None

    def items(self) -> list[WatchTask]:
        return list(self._by_fp.values())

    # ---- 写 ----------------------------------------------------------------

    def open(self, fingerprint: str, session: str, ref: str = "") -> WatchTask:
        """开一个任务。同 fingerprint 已存在且还活着 → 复用（不重复下载）。"""
        old = self._by_fp.get(fingerprint)
        if old and old.alive:
            return old
        task = WatchTask(fingerprint=fingerprint, session=session, ref=ref)
        self._by_fp[fingerprint] = task
        self._gc()
        return task

    def cancel_session(self, session: str, except_fp: str = "") -> int:
        """该会话换了新视频 → 取消旧任务。返回取消数。"""
        n = 0
        for t in self._by_fp.values():
            if t.session == session and t.alive and t.fingerprint != except_fp:
                t.touch(T_CANCELLED, "会话换片")
                n += 1
        return n

    def claim_send(self, task: WatchTask) -> bool:
        """抢占「发结果」的权利。返回 True 表示这次该发，False 表示别人已经发过。"""
        if task.sent_result:
            return False
        task.sent_result = True
        task.touch()
        return True

    # ---- 清理 --------------------------------------------------------------

    def _gc(self) -> None:
        now = time.time()
        dead = [k for k, t in self._by_fp.items() if now - t.updated > self.MAX_AGE]
        for k in dead:
            self._by_fp.pop(k, None)
        if len(self._by_fp) > self.MAX_ITEMS:
            for k, _ in sorted(self._by_fp.items(), key=lambda kv: kv[1].updated)[
                : len(self._by_fp) - self.MAX_ITEMS
            ]:
                self._by_fp.pop(k, None)


def describe_phase(phase: str) -> str:
    """给模型看的缺失说明（人话，不解释机制）。"""
    return {
        SUBS_ONLY: "话我全听到了，画面这次没跟上。",
        FRAMES_ONLY: "画面看到了，但这次没拿到字幕。",
        NOTHING: "这次什么都没拿到。",
    }.get(phase, "")
```

## 六、main.py 里真正干活的那几个方法（IO 适配层，纯函数在上面的 engine 里）

```python
def _subs_text(self, ctx) -> str:
        """把字幕排版成自然段引文；超预算就均匀抽段并声明是节选。"""
        if subs_mod is None or ctx is None or not ctx.subs or not ctx.subs.cues:
            return ""
        try:
            tokens = max(600, int(self.cfg.get("subs_inject_tokens") or 6000))
            max_chars = int(tokens / 0.62)  # 中文约 0.62 token/字
            segs = subs_mod.segment(ctx.subs.cues)
            total = sum(x.chars for x in segs)
            declared = "part" if total > max_chars else "full"
            return subs_mod.render(ctx.subs, segs, declared=declared, max_chars=max_chars)
        except Exception as e:
            logger.warning(f"[feiyu_video] 字幕排版失败: {e}")
            return ""

    def _inject_subs(self, req, text: str) -> None:
        """字幕走 TextPart 并标 temp：只给这一轮，不落库、不每轮重发。"""
        try:
            from astrbot.core.agent.message import TextPart
        except Exception:
            TextPart = None
        if TextPart is None:
            req.system_prompt = (req.system_prompt or "") + "\n\n" + text
            return
        if not isinstance(req.extra_user_content_parts, list):
            req.extra_user_content_parts = []
        part = TextPart(text=text)
        try:
            part.mark_as_temp()
        except Exception:
            try:
                part._no_save = True
            except Exception:
                pass
        req.extra_user_content_parts.append(part)

    def _watch_hint(self, ctx, *, handed: int, can_image: bool) -> str:
        has_subs = bool(ctx is not None and ctx.subs and ctx.subs.cues)
        has_frames = bool(ctx is not None and ctx.frames) and can_image
        if has_subs and has_frames:
            body = ("他发来的是视频链接。下面那段是他们说过的话（原话，一句句都在），"
                    "上面几张是从里面挑出来的画面，两样你都能用。")
        elif has_subs:
            body = ("他发来的是视频链接。下面那段是他们说过的话（原话，一句句都在）——"
                    "但这次画面没跟上，你没看见人在干什么。")
        elif has_frames:
            body = ("他发来的是视频链接。上面几张是从里面挑出来的画面，你能看到；"
                    "这次没拿到他们说的话，别替他们编台词。")
        else:
            body = "他发来的是视频链接，但这次两样都没取到。直说没看成，别猜。"
        return (
            "\n\n（" + body
            + "用你自己的口气回他，别用「视频里展示了…」这种播报腔，也别复述字段名。"
            "他们说过的词就照着说，画面你没看到的地方别补。"
            "别念时间点，也别说你是怎么拿到这些的。）"
        )

    def _watch_result(self, ctx) -> str:
        """tool 路径：把已经取到的东西交回给她（hook 那轮已经注入过的不重复）。"""
        if ctx is None:
            return "这次没取到东西。直说没看成。"
        parts: list[str] = []
        text = self._subs_text(ctx)
        if text:
            parts.append(text)
        if ctx.frames:
            parts.append(f"（这一轮你手上有 {len(ctx.frames)} 张画面，就在上面。）")
        if not parts:
            if wctx_mod is not None:
                return wctx_mod.describe_phase(ctx.phase)
            return "这次没取到东西。直说没看成。"
        parts.append("用你自己的口气回他，别复述字段名，别念时间点。")
        return "\n\n".join(parts)

    # ---------- 字幕（v3）----------

    def _bili_probe(self, url: str) -> tuple[dict, list]:
        """开一次 B 站页面，读 aid/cid/时长 + 字幕列表。返回 (meta, subtitles)。

        只开一次、只读不点：后面下载走 yt-dlp，所以这里不截帧。
        """
        if self._platform(url) != "bilibili":
            return {}, []
        bsk_browser = str(self.cfg.get("bsk_browser") or "").strip()
        args = ["session", "start", "--json", "--no-focus"]
        if bsk_browser:
            args += ["--browser", bsk_browser]
        r = self._bsk(*args, timeout=90)
        sid = str(self._json_slice(r.stdout).get("session_id") or "")
        if not sid:
            raise RuntimeError(f"bsk-session-failed: {(r.stderr or r.stdout)[:160]}")
        info: dict = {}
        try:
            self._bsk("navigate", url, "--session", sid, timeout=90)
            time.sleep(max(2, int(self.cfg.get("browser_settle", 5) or 5)))
            raw = self._bsk("evaluate", "--session", sid, "--json", _BILI_PROBE_JS, timeout=90).stdout
            info = self._json_slice(raw)
            try:
                info = json.loads(info.get("value") or "{}") if "value" in info else info
            except Exception:
                info = {}
        finally:
            try:
                self._bsk("session", "stop", sid, timeout=30)
            except Exception as e:
                logger.warning(f"[feiyu_video] probe 会话没停掉 sid={sid}: {e}")
        if not info.get("ok"):
            logger.info(f"[feiyu_video] 页面上拿不到 aid/cid: {info.get('subs_err') or info}")
            return {}, []
        want = _bvid_of(url)
        got = str(info.get("bvid") or "").strip()
        if want and got and want != got:
            # bsk 复用了旧标签页：页面还是上一条视频。宁可当作没有字幕，也不能张冠李戴。
            logger.warning(f"[feiyu_video] 页面是别的视频（要 {want}，页面 {got}），这次不算")
            return {}, []
        meta = {k: info.get(k) for k in ("aid", "cid", "bvid", "dur", "title", "desc", "page", "n_pages")}
        subs = info.get("subs") or []
        logger.info(
            f"[feiyu_video] 页面信息 aid={meta.get('aid')} cid={meta.get('cid')} "
            f"dur={meta.get('dur')}s subs={len(subs)}"
        )
        return meta, subs

    def _subs_doc(self, meta: dict, subs: list):
        """挑一条字幕并拉正文。不需要 cookie（auth_key 已在 URL 里），只要 Referer。"""
        if subs_mod is None or not subs:
            return None
        pick = subs_mod.pick_doc(subs)
        if not pick:
            return None
        url = str(pick.get("subtitle_url") or pick.get("url") or "")
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith("http"):
            return None
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Referer": "https://www.bilibili.com/",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
                    ),
                },
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"[feiyu_video] 字幕正文拉取失败: {e}")
            return None
        data = body
        if body.lstrip().startswith("{"):
            try:
                data = json.loads(body)
            except Exception:
                data = body
        cues = subs_mod.parse_any(data, fmt=str(pick.get("lan") or ""))
        cues = subs_mod.punctuate(subs_mod.clean_cues(cues))
        doc = subs_mod.SubsDoc(
            cues=cues,
            dur=float(meta.get("dur") or 0),
            lang=str(pick.get("lan") or ""),
            source="ai" if "ai" in str(pick.get("lan") or "") else "manual",
            raw_chars=len(body),
        )
        cov = doc.cover
        logger.info(
            f"[feiyu_video] 字幕 {len(doc.cues)} 条 / {doc.chars} 字 · "
            f"覆盖率 {cov.ratio:.2f} · 空洞 {cov.max_gap:.0f}s"
        )
        return doc

    def _frames_by_plan(self, url: str, meta: dict, doc) -> tuple[list[str], float]:
        """下整片 → 按字幕密度选点 → 定点抽帧。失败返回 ([], 0)。"""
        path = ""
        try:
            path = self._ytdlp_fetch(url, full=True)
            dur = self._duration(path)
            if dur <= 0:
                dur = float(meta.get("dur") or 0)
            n = max(1, int(self.cfg.get("see_shots") or 10))
            cues = doc.cues if doc is not None else []
            spots = picks_mod.plan(cues, dur, n=n)
            frames = self._extract_at(path, [sp.t for sp in spots])
            logger.info(f"[feiyu_video] 按密度抽帧 {picks_mod.describe(spots)} · 片子 {dur:.0f}s")
            return frames, dur
        except Exception as e:
            logger.warning(f"[feiyu_video] 整片抽帧失败 {url[:80]}: {e}")
            return [], 0.0
        finally:
            if path:
                self._cleanup_download(path)

    async def _watch_gather(self, ref: str, *, how: str = "tool"):
        """一次观看的全部产物：字幕 + 帧。hook 与 tool 共用同一份。

        失败一律降级（不抛）：拿不到字幕就只要画面，拿不到画面就只要字幕。
        """
        if wctx_mod is None:
            return None
        ctx = wctx_mod.WatchContext(ref=ref, how=how)
        url = ref if str(ref).startswith(("http://", "https://")) else ""
        meta: dict = {}

        if url and self._platform(url) == "bilibili" and self.cfg.get("subs_enable", True):
            subs_list: list = []
            try:
                meta, subs_list = await asyncio.wait_for(
                    asyncio.to_thread(self._bili_probe, url), timeout=60
                )
            except Exception as e:
                logger.warning(f"[feiyu_video] 取页面信息失败 {url[:80]}: {e}")
                ctx.add_error("probe")
            if meta:
                ctx.dur = float(meta.get("dur") or 0)
            if subs_list:
                try:
                    doc = await asyncio.wait_for(
                        asyncio.to_thread(self._subs_doc, meta, subs_list), timeout=25
                    )
                    ctx.subs = doc
                    if doc is None:
                        ctx.add_error("subs")
                    else:
                        for w in subs_mod.self_check(doc):
                            logger.info(f"[feiyu_video] 字幕可疑：{w}")
                except Exception as e:
                    logger.warning(f"[feiyu_video] 字幕处理失败: {e}")
                    ctx.add_error("subs")

        try:
            if url and meta.get("aid"):
                frames, dur = await asyncio.wait_for(
                    asyncio.to_thread(self._frames_by_plan, url, meta, ctx.subs), timeout=330
                )
                ctx.frames = frames
                if dur:
                    ctx.dur = dur
            else:
                frames, note = await self._frames_for_llm(ref)
                ctx.frames = frames
                ctx.how = ctx.how  # note 只进日志，不给模型（页面简介不算她看到的）
                if note:
                    logger.info(f"[feiyu_video] 页面自带说明（不进上下文）: {note[:80]}")
            if not ctx.frames:
                ctx.add_error("frames")
        except asyncio.TimeoutError:
            logger.warning(f"[feiyu_video] 取画面超时 {url[:80]}")
            ctx.add_error("frames-timeout")
        except Exception as e:
            logger.warning(f"[feiyu_video] 取画面失败 {url[:80]}: {e}")
            ctx.add_error("frames")

        logger.info(f"[feiyu_video] 观看结果 {ctx.brief()}")
        return ctx

    # ---------- 说话的口气 ----------

    def _persona_dir(self) -> Path:
        """persona_mind 的数据目录：配置优先，没配就按同级插件推断。"""
        raw = str(self.cfg.get("persona_data_dir") or "").strip()
        if raw:
            return Path(raw)
        return Path(__file__).resolve().parent.parent.parent / "plugin_data" / "astrbot_plugin_persona_mind"

    def _persona_brief(self) -> str:
        """借人格的味道：几句语料样本 + 当前心情。读不到就返回空串。"""
        import time as _time
        cache = getattr(self, "_persona_cache", None)
        now = _time.time()
        if cache and now - cache[0] < 600:
            return cache[1]
        brief = ""
        try:
            base = self._persona_dir()
            lines = []
            vf = base / "kb" / "voice.md"
            if vf.exists():
                for ln in vf.read_text(encoding="utf-8", errors="ignore").splitlines():
                    t = ln.strip()
                    if not t or t.startswith(("#", ">", "-")):
                        continue
                    lines.append(t)
                    if len(lines) >= 8:
                        break
            mood = ""
            pf = base / "pad.json"
            if pf.exists():
                try:
                    pad = _json.loads(pf.read_text(encoding="utf-8-sig"))
                    if isinstance(pad, dict):
                        v = pad.get("valence", pad.get("v"))
                        if isinstance(v, (int, float)):
                            mood = "现在心情不错" if v >= 20 else ("现在心情一般" if v > -20 else "现在心情不太好")
                except Exception:
                    pass
            if lines:
                brief = "她平时说话的样子（借味道，别照抄）：\n" + "\n".join(f"- {x}" for x in lines)
                if mood:
                    brief += "\n" + mood + "。"
        except Exception as e:
            logger.debug(f"[feiyu_video] 读人格语料失败: {e}")
        self._persona_cache = (now, brief)
        return brief

    def _style_broken(self, text: str) -> bool:
        return any(p in (text or "") for p in _BANNED_PHRASES)

    def _strip_descriptive(self, text: str) -> str:
        """兜底：把描述腔的句子删掉，剩下的话才是她想说的。"""
        parts = re.split(r"(?<=[。！？!?~\n])", (text or "").strip())
        keep = [p for p in parts if p.strip() and not self._style_broken(p)]
        return "".join(keep).strip()

    async def _speak(self, facts_text: str, history: str = "", question: str = "") -> str:
        """把事实换成人格口吻的一两句。失败返回空串，调用方自己兜底。"""
        if not self.cfg.get("rewrite", True):
            return ""
        brief = self._persona_brief()
        ask = ("\n对方的问题：" + question + "\n（先用不超过 20 字答这个，再补一句你的看法）") if question else ""
        base_prompt = (
            _STYLE_RULES + "\n\n" + brief + "\n\n"
            + "你们刚才的聊天：\n" + (history or "（没什么上下文）") + "\n\n"
            + "从画面里提取到的事实：\n" + facts_text + ask
        )
        for attempt in range(2):
            prompt = base_prompt if attempt == 0 else (
                base_prompt + "\n\n上次那版太像播报了。重写：第一句直接是你的反应，别再出现视频/画面/展示这类词。"
            )
            try:
                rw = await self.context.llm_generate(
                    chat_provider_id=self.cfg.get("rewrite_provider", "deepseek/deepseek-flash"),
                    prompt=prompt,
                )
                spoken = (rw.completion_text or "").strip()
            except Exception as e:
                logger.warning(f"[feiyu_video] 转述失败: {e}")
                return ""
            if spoken and not self._style_broken(spoken):
                return spoken.strip('"“”')
            if spoken:
                cleaned = self._strip_descriptive(spoken)
                if cleaned:
                    return cleaned.strip('"“”')
        return ""

    def _crop_video_area(self, shot: str, info: dict, out: Path) -> str:
        """按 video 元素 rect 裁出画面；算不准就整张用，宁可糙也别裁错。"""
        try:
            iw = float(info.get("iw") or 0)
            ih = float(info.get("ih") or 0)
            x, y = float(info.get("x") or 0), float(info.get("y") or 0)
            w, h = float(info.get("w") or 0), float(info.get("h") or 0)
            if iw <= 0 or ih <= 0 or w < 80 or h < 80:
                return shot
            # 只裁「元素 ∩ 视口」：抖音这种播放器比屏幕还高的，也能裁到中间那块
            x0, y0 = max(0.0, min(x, iw)), max(0.0, min(y, ih))
            x1, y1 = max(0.0, min(x + w, iw)), max(0.0, min(y + h, ih))
            if (x1 - x0) < 160 or (y1 - y0) < 120:
                logger.info("[feiyu_video] 播放器不在视口里，用整屏截图")
                return shot
            with Image.open(shot) as im:
                scale = im.width / iw
                box = (int(x0 * scale), int(y0 * scale),
                       min(im.width, int(x1 * scale)), min(im.height, int(y1 * scale)))
                if box[2] - box[0] < 120 or box[3] - box[1] < 90:
                    return shot
                crop = im.crop(box).convert("RGB")
                crop.save(out, quality=90)
            try:
                Path(shot).unlink(missing_ok=True)   # 裁完的原图是纯中间产物
            except OSError:
                pass
            return str(out)
        except Exception as e:
            logger.warning(f"[feiyu_video] 裁画面失败: {e}")
            return shot

    async def _frames_from_url(self, url: str) -> tuple[list[str], str]:
        """网页链接 → (帧列表, 附注文本)。先 yt-dlp，再退浏览器。"""
        url = self._norm_url(url)
        if not self._allowed(url):
            raise PermissionError("url-not-allowed")

        now = time.time()
        hit = self._recent_links.get(url)
        if hit and now - hit[0] < 60:
            return hit[1], hit[2]

        # 这些站不给 cookies 就不发流，白试一次还多等几秒——直接走浏览器
        plat = self._platform(url)
        needs_cookie = plat in ("douyin", "kuaishou", "xiaohongshu")
        skip_ytdlp = needs_cookie and not str(self.cfg.get("cookies_browser") or "").strip()

        async with self._video_sem:
            try:
                if skip_ytdlp:
                    raise RuntimeError("needs-cookies")
                path = await asyncio.to_thread(self._ytdlp_fetch, url)
                frames = await asyncio.to_thread(self._frames, path)
                if frames:
                    logger.info(f"[feiyu_video] yt-dlp 拿到 {len(frames)} 帧: {url}")
                    self._recent_links[url] = (now, frames, "")
                    return frames, ""
                logger.warning(f"[feiyu_video] yt-dlp 下到了但抽不出帧: {url}")
            except Exception as e:
                if "needs-cookies" not in str(e):
                    logger.warning(f"[feiyu_video] yt-dlp 没拿到（{plat}）: {e}")

            if not self.cfg.get("browser_fallback", True):
                raise RuntimeError("no-fallback")
            if self._platform(url) == "other":
                # 直链媒体只走 yt-dlp：别拿登录态的浏览器去开任意域名
                raise RuntimeError("not-platform")

            title, desc, shots = await asyncio.wait_for(
                asyncio.to_thread(self._bsk_grab, url), timeout=180)
            note = f"（下面这段是网页自己写的，不是用户的话）标题：{title}" if title else ""
            if desc:
                note += f"；简介：{desc[:200]}"
            logger.info(f"[feiyu_video] 浏览器截帧 {len(shots)} 张: {url}")
            self._recent_links[url] = (now, shots, note.strip())
            return shots, note.strip()

    # ---------- 让她自己看 ----------

    def _main_supports_image(self, event: AstrMessageEvent) -> bool:
        """主模型这一路到底收不收图；判断不了就当作支持（AstrBot 默认全支持）。"""
        try:
            provider = self.context.get_using_provider(getattr(event, "unified_msg_origin", None))
            if provider is None:
                return False
            mods = provider.provider_config.get("modalities")
            if not mods:
                # 没声明就按不支持处理：宁可退回「插件看完再转述」，也不要把图塞给纯文本模型
                return False
            return "image" in mods
        except Exception as e:
            logger.warning(f"[feiyu_video] 判断主模型能力失败: {e}")
            return False

    def _link_in_message(self, msg: str) -> str | None:
        for raw in re.findall(r"https?://[^\s<>【】（）「」]+", msg or ""):
            # 中文常常紧贴着链接写（"…&vd_source=xxx给你看看这个"），在第一个汉字处切断
            raw = re.split(r"[\u4e00-\u9fa5]", raw, maxsplit=1)[0]
            url = self._norm_url(raw.rstrip("，。！？；：、,.!?"))
            if self._allowed(url):
                return url
        return None

    async def _frames_for_llm(self, ref: str) -> tuple[list[str], str]:
        """按链接或本地文件取帧；同一份看过的在 see_cache_seconds 内直接复用。"""
        ref = (ref or "").strip()
        if not ref:
            return [], ""
        if not ref.startswith(("http://", "https://")) and re.match(r"^[\w.-]+\.[a-z]{2,6}/", ref):
            ref = "https://" + ref
        now = time.time()
        ttl = max(30, int(self.cfg.get("see_cache_seconds") or 600))
        if ref.startswith(("http://", "https://")):
            parts = urllib.parse.urlsplit(ref)
            key = f"{parts.scheme}://{parts.netloc}{parts.path}"
        else:
            key = ref
        hit = self._see_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1], hit[2]
        if ref.startswith(("http://", "https://")):
            frames, note = await self._frames_from_url(ref)
        else:
            path = self._resolve_local(ref)
            if not path or not os.path.exists(path):
                return [], ""
            frames = await asyncio.to_thread(self._frames, path)
            note = ""
        self._see_cache[key] = (now, list(frames), note)
        if len(self._see_cache) > 60:
            for k in sorted(self._see_cache, key=lambda x: self._see_cache[x][0])[:20]:
                self._see_cache.pop(k, None)
        return frames, note

    @filter.on_llm_request()

    @filter.on_llm_request()
    async def see_frames_with_llm(self, event: AstrMessageEvent, req) -> None:
        """他发来视频/链接：把台词和画面一起塞进这一次请求，让她自己看、自己说。"""
        if not self.cfg.get("see_with_llm", True):
            return
        msg = (event.message_str or "").strip()
        if msg.startswith("/"):
            return
        # 只发视频（QQ 里 message_str 是空串）走组件；只发链接走文本
        ref = self._video_ref(event) or self._link_in_message(msg)
        if not ref:
            return
        try:
            ctx = await asyncio.wait_for(
                self._watch_gather(ref, how="hook"),
                timeout=float(self.cfg.get("see_timeout") or 150),
            )
        except asyncio.TimeoutError:
            logger.warning(f"[feiyu_video] 自己看：超时 {ref[:80]}")
            return
        except Exception as e:
            logger.warning(f"[feiyu_video] 自己看：失败 {ref[:80]}: {e}")
            return
        if ctx is None:
            return

        can_image = self._main_supports_image(event)
        handed = 0
        if can_image and ctx.frames:
            picked = list(ctx.frames)[: max(1, int(self.cfg.get("see_shots") or 3))]
            # 只给这一轮看：_no_save 让框架别把这几张图写进会话历史、别每轮重发
            try:
                from astrbot.core.agent.message import ImageURLPart
            except Exception:  # 老版本没有这个类，退回老写法
                ImageURLPart = None
            if ImageURLPart is not None:
                if not isinstance(req.extra_user_content_parts, list):
                    req.extra_user_content_parts = []
                for p in picked:
                    part = ImageURLPart(image_url=ImageURLPart.ImageURL(url=p))
                    try:
                        part._no_save = True
                    except Exception:
                        pass
                    req.extra_user_content_parts.append(part)
            else:
                req.image_urls = list(req.image_urls or []) + picked
            self._last_frames[event.unified_msg_origin] = picked
            handed += 1

        text = self._subs_text(ctx)
        if text:
            self._inject_subs(req, text)
            handed += 1

        if not handed:
            logger.info(f"[feiyu_video] 这次什么都没取到 · {ctx.brief()} · {ref[:80]}")
            return
        req.system_prompt = (req.system_prompt or "") + self._watch_hint(
            ctx, handed=handed, can_image=can_image
        )
        try:
            event.set_extra("feiyu_watch_ctx", ctx)
        except Exception:
            pass
        logger.info(f"[feiyu_video] 交给主模型 {handed} 份 · {ctx.brief()} · {ref[:80]}")
        try:
            kit.record_material(f"看了他发来的视频：{ref[:80]}", kind="event", source="video",
                                umo=str(getattr(event, "unified_msg_origin", "") or ""),
                                mood={"P": 2.0, "Ar": 1.2})
        except Exception:
            pass

    @staticmethod
    def _user_hint(e: Exception) -> str:
        """给群里看的一句话；技术细节只进日志。"""
        s = str(e)
        if isinstance(e, PermissionError):
            return "这个链接我不看，只认视频站的链接"
        if "no-ytdlp" in s:
            return "这条得先装 yt-dlp，我下不了"
        if "timeout" in s:
            return "这条下得太慢，先不看了"
        if "cookie" in s.lower() or "登录" in s:
            return "这个站要登录态，我拿不到画面"
        if "bsk-session" in s:
            return "浏览器没起来，等会儿再试"
        if "no-video-element" in s:
            return "页面打开了，但没找到视频画面"
        if "not-platform" in s:
            return "这个链接我不开浏览器看，只认视频站和直链"
        if "no-fallback" in s:
            return "这条下不下来，浏览器兜底又关着"
        return "这条链接我打不开"

    def _duration(self, path: str) -> float:
        try:
            p = subprocess.run([self.ffmpeg, "-i", path], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60,
                               creationflags=SUBPROCESS_FLAGS)
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", p.stderr or "")
            if m:
                h, mi, s = m.groups()
                return int(h) * 3600 + int(mi) * 60 + float(s)
        except Exception as e:
            logger.warning(f"[feiyu_video] 读取时长失败: {e}")
        return 0.0

    def _frames(self, path: str) -> list[str]:
        try:
            return self._extract_frames(path)
        finally:
            self._cleanup_download(path)

    def _extract_frames(self, path: str) -> list[str]:
        dur = self._duration(path)
        if dur <= 0:
            dur = 10.0
        dur = min(dur, float(self.cfg.get("max_seconds", 600)))
        n = max(1, int(self.cfg.get("frames", 6)))
        width = int(self.cfg.get("frame_width", 640))
        outdir = self.data_dir / "frames" / uuid.uuid4().hex[:8]
        outdir.mkdir(parents=True, exist_ok=True)

        frames = []
        for i in range(n):
            t = max(0.3, dur * (i + 0.5) / n)
            out = outdir / f"f{i}.jpg"
            cmd = [self.ffmpeg, "-y", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                   "-vf", f"scale={width}:-2", str(out)]
            try:
                subprocess.run(cmd, capture_output=True, timeout=60,
                               creationflags=SUBPROCESS_FLAGS)
                if out.exists() and out.stat().st_size > 0:
                    frames.append(str(out))
            except Exception as e:
                logger.warning(f"[feiyu_video] 抽帧失败 t={t}: {e}")
        return frames

    def _extract_at(self, path: str, times: list[float], width: int = 0) -> list[str]:
        """按指定时刻抽帧（秒）。给的是 picks.plan 算出来的点，不是等间隔。"""
        width = width or int(self.cfg.get("frame_width", 640))
        outdir = self.data_dir / "frames" / uuid.uuid4().hex[:8]
        outdir.mkdir(parents=True, exist_ok=True)
        frames: list[str] = []
        for i, t in enumerate(times):
            out = outdir / f"f{i}.jpg"
            cmd = [self.ffmpeg, "-y", "-ss", f"{max(0.1, float(t)):.2f}", "-i", path,
                   "-frames:v", "1", "-vf", f"scale={width}:-2", str(out)]
            try:
                subprocess.run(cmd, capture_output=True, timeout=60,
                               creationflags=SUBPROCESS_FLAGS)
                if out.exists() and out.stat().st_size > 0:
                    frames.append(str(out))
            except Exception as e:
                logger.warning(f"[feiyu_video] 定点抽帧失败 t={t}: {e}")
        return frames

    # ---------- LLM 工具 ----------

    async def do_watch(self, event: AstrMessageEvent, video_ref: str, question: str = "") -> str:
        """看一段视频，把画面里的事实交给你。

        什么时候用：用户发来视频、**发来 B站/抖音/快手/小红书/YouTube 的页面链接**、
        问"这个视频讲了什么"、让你看视频里的动作/场景/字幕，或者要你评价时。
        页面链接直接传进来就行，插件会自己想办法拿到画面。

        返回值是画面里的事实（主题/人物/作品/画面文字等），**紧接着就该用你自己的口气回话**，
        不要复述字段名，也不要把它当成已经回给用户的答复。

        Args:
            video_ref(string): 视频的本地路径，或 http(s) 链接（可以是 B站/抖音这类网页链接）
            question(string): 想从视频里知道什么；不填就按画面给一段事实
        """
        video_ref = (video_ref or "").strip().strip("'\"")
        try:
            _ctx = event.get_extra("feiyu_watch_ctx")
        except Exception:
            _ctx = None
        if _ctx is not None:
            return self._watch_result(_ctx)
        if not video_ref:
            return "他没给视频地址。问他要一下。"

        note = ""
        frames: list[str] = []
        try:
            video_ref = self._norm_url(video_ref)
            is_link = video_ref.startswith(("http://", "https://"))
            if is_link and not self._is_media_url(video_ref):
                if not self._allowed(video_ref):
                    return "这个链接不在能打开的站点里（内网或陌生站点），直说打不开。"
                frames, note = await self._frames_from_url(video_ref)
            else:
                path = video_ref
                if is_link:
                    if not self._allowed(video_ref):
                        return "这个链接不在能打开的站点里，直说打不开。"
                    path = await asyncio.to_thread(self._download, video_ref)
                if not os.path.exists(path):
                    return f"没找到这个视频文件（{os.path.basename(path)}）。直说没拿到。"
                frames = await asyncio.to_thread(self._frames, path)
        except Exception as e:
            logger.exception(f"[feiyu_video] 处理视频失败: {video_ref}")
            return self._user_hint(e)

        if not frames:
            return "画面抽不出来。直说这次没看成。"

        q = (question or "").strip()
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.cfg.get("vision_provider_id", "deepseek/deepseek-vision"),
                prompt=_facts_prompt(note, len(frames)),
                image_urls=frames,
            )
            text = (resp.completion_text or "").strip()
        except Exception as e:
            logger.error(f"[feiyu_video] 视觉调用失败: {e}")
            return f"看画面时出错了（{type(e).__name__}）。这次没看成，别编内容。"

        if not text:
            return "画面拿到了，但什么都没读出来。直说没看成就行。"

        facts = None
        try:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                facts = json.loads(m.group(0))
        except Exception as e:
            logger.warning(f"[feiyu_video] 视觉 JSON 解析失败: {e}")

        if isinstance(facts, dict) and facts:
            brief = _facts_brief(facts, note)
        else:
            brief = "（这是你看画面看到的，不是别人转述）\n" + text[:800]

        if q:
            brief += "\n\n对方问的是：" + q
        brief += "\n\n（这些是你看画面得到的事实：接下来用你自己的口气回他，别复述字段名。）"
        self._last_frames[event.unified_msg_origin] = list(frames or [])[:3]
        logger.info(f"[feiyu_video] 画面事实已交回模型: {len(brief)} 字")
        return brief

    # ---------- 收到视频自动看 ----------

    def _video_ref(self, event) -> str | None:
        """从消息里取第一个视频的路径/URL。"""
        try:
            comps = getattr(event.message_obj, "message", None) or []
        except Exception:
            return None
        for c in comps:
            if c.__class__.__name__ != "Video":
                continue
            info = {k: getattr(c, k, None) for k in ("file", "url", "path", "cover")}
            logger.info(f"[feiyu_video] 收到视频组件: {info}")
            ref = info.get("path") or info.get("file") or info.get("url") or ""
            if not ref:
                continue
            if ref.startswith("file://"):
                from urllib.request import url2pathname
                from urllib.parse import urlparse
                ref = url2pathname(urlparse(ref).path)
            return self._resolve_local(ref)
        return None

    async def _fetch_video_url(self, event, fname: str) -> str | None:
        """用 OneBot 的 get_msg / get_file 把裸文件名换成可下载的 URL。"""
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        call = getattr(api, "call_action", None)
        if call is None:
            return None
        mid = getattr(event.message_obj, "message_id", None)
        if mid is None:
            return None

        # 1) get_msg：拿原始消息段里的 url
        try:
            ret = await call("get_msg", message_id=int(mid))
            segs = ret.get("message") or []
            for seg in segs:
                if seg.get("type") != "video":
                    continue
                data = seg.get("data") or {}
                logger.info(f"[feiyu_video] get_msg 视频段: {data}")
                url = data.get("url") or ""
                if url.startswith("http"):
                    return url
                fid = data.get("file_id") or data.get("file") or fname
                # 2) get_file：用 file_id 换下载地址
                try:
                    fr = await call("get_file", file_id=fid)
                    u = (fr.get("url") or "") if isinstance(fr, dict) else ""
                    if u.startswith("http"):
                        return u
                    b64 = (fr.get("base64") or "") if isinstance(fr, dict) else ""
                    if b64:
                        import base64 as _b64
                        content = _b64.b64decode(b64)
                        out = os.path.join(tempfile.gettempdir(), f"feiyu_video_{uuid.uuid4().hex}.mp4")
                        self._temporary_files.add(out)
                        try:
                            with open(out, "wb") as f:
                                f.write(content)
                        except BaseException:
                            self._cleanup_download(out)
                            raise
                        return out
                except Exception as e:
                    logger.warning(f"[feiyu_video] get_file 失败: {e}")
        except Exception as e:
            logger.warning(f"[feiyu_video] get_msg 失败: {e}")
        return None

    def _resolve_local(self, ref: str) -> str:
        """路径不存在时，按文件名去 NapCat 目录和临时目录里找一找。"""
        if ref.startswith(("http://", "https://")):
            return ref
        if os.path.exists(ref):
            return ref
        base = os.path.basename(ref.replace("\\", "/"))
        roots = [
            r"D:\qqaibot\napcat",
            os.environ.get("TEMP", ""),
            os.environ.get("TMP", ""),
        ]
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                if base in files:
                    found = os.path.join(dirpath, base)
                    logger.info(f"[feiyu_video] 按文件名找到视频: {found}")
                    return found
        return ref


    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_watch_video(self, event: AstrMessageEvent):
        """收到视频就直接处理：不发中间消息，最后一次性 send，避免被别的插件截断。"""
        ref = self._video_ref(event)
        if not ref:
            return
        # 主模型能看图时，不拦事件：把帧塞进请求，让她自己回（see_frames_with_llm）
        if self.cfg.get("see_with_llm", True) and self._main_supports_image(event):
            logger.info("[feiyu_video] 交给主模型自己看，不再代发")
            return
        event.stop_event()

        async def send(text: str):
            try:
                await event.send(MessageChain([Comp.Plain(text)]))
            except Exception as e:
                logger.error(f"[feiyu_video] 发送失败: {e}")

        # 先像人一样应一声；用 send 直发，不会被别的插件截断
        await send("我看看…")

        link_note = ""
        ref = self._norm_url(ref) if ref else ref
        if ref.startswith(("http://", "https://")) and not self._is_media_url(ref):
            try:
                frames, link_note = await self._frames_from_url(ref)
            except Exception as e:
                logger.exception(f"[feiyu_video] 链接视频失败: {ref}")
                await send(self._user_hint(e))
                return
            if not frames:
                await send("链接打开了，但没截到画面")
                return
            path = None
        else:
            frames = []
            path = ref

        try:
            if path is not None:
                if path.startswith(("http://", "https://")):
                    path = await asyncio.to_thread(self._download, path)
                if not os.path.exists(path):
                    # 只有文件名：找 NapCat 要真实地址
                    url = await self._fetch_video_url(event, os.path.basename(path))
                    if url:
                        if url.startswith(("http://", "https://")):
                            path = await asyncio.to_thread(self._download, url)
                        else:
                            path = url
                if not os.path.exists(path):
                    await send(f"视频没拿到：{path}")
                    return
                frames = await asyncio.to_thread(self._frames, path)
        except Exception as e:
            logger.error(f"[feiyu_video] 处理视频失败: {e}")
            await send(f"视频打不开：{type(e).__name__}")
            return

        if not frames:
            await send("抽不出画面，可能这个格式不支持")
            return

        prompt = (
            (f"{link_note}\n" if link_note else "")
            + f"这些是从一段视频里按时间顺序抽取的 {len(frames)} 帧画面。请提取事实，严格输出 JSON，不要多余文字：\n"
            '{"类型":"新闻/动漫/游戏/影视/生活/教学/其他",'
            '"主题":"一句话说这条视频在讲什么",'
            '"人物":["画面里出现的人，能认出来就写名字，认不出写身份描述"],'
            '"时间":"画面里能读到的日期或时间点，没有就空",'
            '"地点":"画面里能读到的地点或场景，没有就空",'
            '"作品":"如果是动漫/影视/游戏，写作品名，不确定就空",'
            '"角色":["作品里的角色名或外形描述"],'
            '"事件":"如果是新闻，写发生了什么；不是新闻就空",'
            '"画面文字":["画面上出现的字幕、标题、牌匾、弹幕逐条列出"],'
            '"氛围":"搞笑/严肃/激烈/温情/日常"}\n'
            "只写画面里能看到的，不确定的字段留空字符串或空数组，绝对不要编。"
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.cfg.get("vision_provider_id", "deepseek/deepseek-vision"),
                prompt=prompt,
                image_urls=frames,
            )
            text = (resp.completion_text or "").strip()
        except Exception as e:
            logger.error(f"[feiyu_video] 视觉调用失败: {e}")
            await send(f"看图出错了：{type(e).__name__}")
            return

        # ---------- 解析成事实 ----------
        facts = None
        try:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                facts = json.loads(m.group(0))
        except Exception as e:
            logger.warning(f"[feiyu_video] 视觉 JSON 解析失败: {e}")

        if not facts:
            facts = {"主题": (text or "").strip()[:200], "人物": [], "画面文字": []}

        # ---------- 认不出来的东西，去搜一下 ----------
        unknowns = []
        if not facts.get("作品"):
            unknowns.append((facts.get("类型") or "") + " 作品")
        for kw in (facts.get("画面文字") or [])[:2]:
            if 2 <= len(str(kw)) <= 20:
                unknowns.append(str(kw))
        # 动漫/影视：先拿最清晰的一帧去 trace.moe 搜番
        if frames and (not facts.get("作品") or str(facts.get("类型") or "") in ("动漫", "影视")):
            hit = await asyncio.to_thread(self._identify_anime, frames[0])
            if hit:
                facts["搜番结果"] = hit
                logger.info(f"[feiyu_video] trace.moe 命中: {hit}")

        # 真实人物/新闻/游戏：用百度识图跑一帧
        if frames and self.cfg.get("image_search", True):
            need = (str(facts.get("类型") or "") in ("新闻", "生活", "其他", "游戏")
                    or not facts.get("人物") or any("认不出" in str(p) for p in (facts.get("人物") or [])))
            if need:
                try:
                    hit2 = await asyncio.to_thread(self._baidu_identify, frames[0])
                    if hit2:
                        facts["识图结果"] = hit2
                        logger.info(f"[feiyu_video] 百度识图: {hit2[:80]}")
                except Exception as e:
                    logger.warning(f"[feiyu_video] 百度识图失败: {e}")

        if unknowns and self.cfg.get("search_key"):
            try:
                hint = await asyncio.to_thread(self._web_hint, " ".join(unknowns[:2]))
                if hint:
                    facts["搜索补充"] = hint
            except Exception as e:
                logger.warning(f"[feiyu_video] 搜索补充失败: {e}")

        # ---------- 结合上下文，用她的话说 ----------
        history = self._recent_history(event)
        fact_text = json.dumps(facts, ensure_ascii=False)[:1500]

        spoken = await self._speak(fact_text, history)

        if not spoken:
            theme = str(facts.get("主题") or "").strip()
            spoken = ("看完了，" + theme) if theme and len(theme) <= 40 else "看完了，但我说不好这是啥，你再问我细节？"

        self._last_frames[event.unified_msg_origin] = list(frames or [])[:3]
        final = (spoken or "看完了，没看出什么名堂。")[:300]

        # 手动写进对话历史：事件被 stop 了，pipeline 不会替我们记
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(event.unified_msg_origin)
            if cid:
                await cm.add_message_pair(
                    cid,
                    {"role": "user", "content": "[发来一段视频]"},
                    {"role": "assistant", "content": final},
                )
                logger.info("[feiyu_video] 已写入对话历史")
            else:
                logger.warning("[feiyu_video] 没有当前会话 id，历史未写入")
        except Exception as e:
            logger.warning(f"[feiyu_video] 写历史失败: {e}")

        yield event.plain_result(final)

        # 给「写日记」留一份素材：她隔天补写时会翻到这条
        try:
            self._note_for_diary(facts, spoken or final, ref, event.unified_msg_origin)
        except Exception as e:
            logger.warning(f"[feiyu_video] 日记素材写入失败: {e}")

    def _note_for_diary(self, facts: dict, spoken: str, ref: str, umo: str) -> None:
        """把她看完视频后的事实和当时的反应，追加进 persona_mind 的素材池。

        不 import 对方插件（耦合会互相拖崩），直接按约定的 jsonl 追加。
        """
        import time as _time
        from pathlib import Path

        base = Path(self.cfg.get("diary_material_dir") or "").expanduser()
        if not str(base) or base == Path("."):
            base = Path("data/plugin_data/astrbot_plugin_persona_mind/diary_materials")
        base.mkdir(parents=True, exist_ok=True)
        day = _time.strftime("%Y-%m-%d")
        rec = {
            "ts": _time.time(),
            "kind": "video",
            "umo": str(umo)[:120],
            "url": str(ref)[:300],
            "title": str(facts.get("作品") or facts.get("主题") or "")[:80],
            "summary": str(facts.get("主题") or "")[:160],
            "type": str(facts.get("类型") or "")[:16],
            "feel": str(spoken)[:160],
        }
        try:
            kit.record_mood({"P": 2.5, "Ar": 1.5}, umo=str(umo), source="video")
        except Exception:
            pass
        with (base / f"{day}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


    def _recent_history(self, event, turns: int | None = None) -> str:
        """从会话库里取最近几轮对话文本，作为转述时的语境。"""
        import sqlite3
        try:
            umo = event.unified_msg_origin
            con = sqlite3.connect(r"D:\qqaibot\astrbot\data\data_v4.db", timeout=5)
            row = con.execute("select content from conversations where user_id=? order by updated_at desc limit 1", (umo,)).fetchone()
            con.close()
            if not row:
                return "（无）"
            msgs = json.loads(row[0])
            out = []
            for m in msgs[-(turns or int(self.cfg.get("history_turns", 6))):]:
                role = "对方" if m.get("role") == "user" else "我"
                parts = m.get("content")
                txt = " ".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text") if isinstance(parts, list) else str(parts)
                txt = re.sub(r"<system_reminder>.*?</system_reminder>", "", txt, flags=re.S).strip()
                if txt:
                    out.append(f"{role}: {txt[:120]}")
            return "\n".join(out) or "（无）"
        except Exception as e:
            logger.warning(f"[feiyu_video] 读历史失败: {e}")
            return "（无）"

    def _bsk(self, *args, timeout: int = 150):
        """调 bsk CLI（后台浏览器，不抢焦点）。"""
        import subprocess
        bsk = str(self.cfg.get("bsk_path") or "bsk")
        env = dict(os.environ)
        env["BSK_AUTO_START"] = "0"
        if self.cfg.get("bsk_path"):
            env["PATH"] = env.get("PATH", "") + os.pathsep + os.path.dirname(str(bsk))
        return subprocess.run([bsk, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env,
                              creationflags=SUBPROCESS_FLAGS)

    def _baidu_identify(self, image_path: str) -> str:
        """百度识图：压图 -> 注入上传控件 -> 读结果。返回若干条结果标题。"""
        import base64, time
        try:
            from PIL import Image
            im = Image.open(image_path).convert("RGB")
            im.thumbnail((512, 512))
            buf = __import__("io").BytesIO()
            im.save(buf, "JPEG", quality=72)
            b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception as e:
            logger.warning(f"[feiyu_video] 图片压缩失败: {e}")
            return ""

        bid = self.cfg.get("bsk_browser", "")
        args = ["session", "start", "--json", "--no-focus"] + (["--browser", bid] if bid else [])
        r = self._bsk(*args)
        if r.returncode != 0:
            try:
                rr = self._bsk("browsers", "--json", timeout=30)
                arr = json.loads(rr.stdout or "[]")
                if arr:
                    bid = arr[0]["instance_id"]
                    self.cfg["bsk_browser"] = bid
                    self._save_cfg()
                    r = self._bsk("session", "start", "--json", "--no-focus", "--browser", bid)
            except Exception:
                pass
        try:
            sid = json.loads(r.stdout[r.stdout.index("{"):r.stdout.rindex("}") + 1])["session_id"]
        except Exception:
            logger.warning(f"[feiyu_video] bsk 会话失败: {(r.stderr or r.stdout)[:160]}")
            return ""

        try:
            self._bsk("navigate", "https://graph.baidu.com/pcpage/index?tpl_from=pc", "--session", sid)
            time.sleep(2)
            self._bsk("evaluate", "--session", sid, "--json", "window.__img=''")
            for i in range(0, len(b64), 20000):
                part = b64[i:i + 20000]
                self._bsk("evaluate", "--session", sid, "--json", f'window.__img += "{part}"; 1')

            js = (
                "(() => { const inp=document.querySelector('input[type=file]'); if(!inp) return 'no';"
                "const bin=atob(window.__img); const arr=new Uint8Array(bin.length);"
                "for(let i=0;i<bin.length;i++) arr[i]=bin.charCodeAt(i);"
                "const f=new File([arr],'p.jpg',{type:'image/jpeg'}); const dt=new DataTransfer();"
                "dt.items.add(f); inp.files=dt.files;"
                "inp.dispatchEvent(new Event('change',{bubbles:true})); return 'ok'; })()"
            )
            self._bsk("evaluate", "--session", sid, "--json", js)
            time.sleep(8)

            out = self._bsk("observe", "--session", sid, "--json")
            try:
                text = json.loads(out.stdout[out.stdout.index("{"):out.stdout.rindex("}") + 1]).get("text", "")
            except Exception:
                text = ""
        finally:
            self._bsk("session", "stop", sid)

        # 从语义树里拣出结果条目
        lines = []
        for line in (text or "").splitlines():
            line = line.strip()
            if line.startswith("StaticText "):
                v = line[len("StaticText "):].strip().strip('"')
                if not (4 <= len(v) <= 60):
                    continue
                if any(x in v for x in ("相似图片", "相关商品", "全部", "淘宝", "天猫", "神器", "一键", "编辑", "￥", "¥")):
                    continue
                lines.append(v)
            if len(lines) >= 8:
                break
        return "；".join(dict.fromkeys(lines))[:300]

    def _identify_anime(self, frame_path: str) -> str:
        """用 trace.moe 以图搜番：返回「作品 第几集 相似度」或空串。"""
        import urllib.request, uuid as _uuid
        try:
            with open(frame_path, "rb") as f:
                data = f.read()
            boundary = "----WebKit" + _uuid.uuid4().hex
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"a.jpg\"\r\n"
                f"Content-Type: image/jpeg\r\n\r\n".encode()
                + data
                + f"\r\n--{boundary}--\r\n".encode()
            )
            req = urllib.request.Request(
                "https://api.trace.moe/search?anilistInfo&cutBorders",
                data=body,
                headers={"Content-Type": "multipart/form-data; boundary=" + boundary,
                         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
                         "Accept": "application/json"},
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            d = json.loads(opener.open(req, timeout=40).read().decode("utf-8", "ignore"))
            out = []
            for item in (d.get("result") or [])[:2]:
                sim = float(item.get("similarity") or 0)
                if sim < 0.82:
                    continue
                an = item.get("anilist") or {}
                t = an.get("title") or {}
                name = t.get("native") or t.get("romaji") or t.get("english") or ""
                ep = item.get("episode")
                if name:
                    out.append(f"{name} 第{ep}集（相似度{sim:.2f}）" if ep else f"{name}（相似度{sim:.2f}）")
            return "；".join(out)
        except Exception as e:
            logger.warning(f"[feiyu_video] trace.moe 失败: {e}")
            return ""

    def _web_hint(self, query: str) -> str:
        """用 DeepSeek 搜索补一点背景（认人/认作品）。"""
        import urllib.request
        payload = {"query": query[:120], "count": 3, "need_content": False}
        req = urllib.request.Request(
            self.cfg.get("search_api", "https://api.deepseek.com/v1/tools/search"),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.cfg.get("search_key", "")},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        data = json.loads(opener.open(req, timeout=20).read())
        lines = []
        for r in (data.get("results") or [])[:3]:
            t = (r.get("title") or "").strip()
            sm = (r.get("summary") or "").strip().replace("\n", " ")
            if t or sm:
                lines.append(f"{t}｜{sm[:80]}")
        return "\n".join(lines)[:400]

    async def _identify_last(self, event: AstrMessageEvent) -> str:
        """对刚才那个视频的画面做以图搜图（百度识图），用来认人、认场景。当用户问"这人是谁""搜搜他"时使用。"""
        frames = self._last_frames.get(event.unified_msg_origin) or []
        if not frames:
            return "还没看过视频，手上没有图可搜。"
        try:
            out = await asyncio.to_thread(self._baidu_identify, frames[0])
        except Exception as e:
            logger.error(f"[feiyu_video] 识图工具失败: {e}")
            return f"搜图失败（{type(e).__name__}）。"
        return (out or "没搜到明显的匹配，可能这人网上没资料").strip()

    @filter.command("识图")
    async def cmd_identify(self, event: AstrMessageEvent):
        """百度识图：/识图 <本地路径或图片URL>"""
        raw = (event.message_str or "").strip()
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法：/识图 D:\\某张图.jpg")
            return
        ref = parts[1].strip()
        if ref.startswith(("http://", "https://")):
            ref = await asyncio.to_thread(self._download, ref)
        if not os.path.exists(ref):
            yield event.plain_result(f"找不到图片：{ref}")
            return
        yield event.plain_result("搜着呢…")
        try:
            out = await asyncio.to_thread(self._baidu_identify, ref)
        except Exception as e:
            yield event.plain_result(f"识图失败：{type(e).__name__}")
            return
        finally:
            self._cleanup_download(ref)
        yield event.plain_result(out or "没搜到明显的匹配")

    @filter.command("看视频")
    async def cmd_watch(self, event: AstrMessageEvent):
        """手动让她看视频：/看视频 <本地路径 或 B站/抖音链接>"""
        raw = (event.message_str or "").strip()
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法：/看视频 D:\\某个视频.mp4")
            return
        yield event.plain_result("看着呢…")
        async for r in self.watch_video(event, parts[1].strip(), ""):
            yield r
```

## 七、配置项（plugin 的 _conf_schema.json，节选）

```json
{
  "_sec_vision": {
    "description": "=== 看视频 ===",
    "type": "text",
    "invisible": true
  },
  "vision_provider_id": {
    "description": "视觉模型 provider id",
    "type": "string",
    "default": "",
    "hint": "填 AstrBot 里配置好的多模态模型 id，例如 deepseek/deepseek-vision。"
  },
  "frames": {
    "description": "抽帧数量",
    "type": "int",
    "default": 6,
    "slider": {
      "min": 1,
      "max": 30,
      "step": 1
    },
    "hint": "越多越准，也越费钱/越慢。"
  },
  "max_seconds": {
    "description": "最长视频时长（秒）",
    "type": "int",
    "default": 600,
    "slider": {
      "min": 10,
      "max": 3600,
      "step": 10
    }
  },
  "frame_width": {
    "description": "抽帧宽度（像素）",
    "type": "int",
    "default": 640,
    "slider": {
      "min": 320,
      "max": 1920,
      "step": 32
    }
  },
  "_sec_rewrite": {
    "description": "=== 说话方式 ===",
    "type": "text",
    "invisible": true
  },
  "rewrite": {
    "description": "把事实重写成人格口吻",
    "type": "bool",
    "default": true
  },
  "rewrite_provider": {
    "description": "重写用的文本模型 id",
    "type": "string",
    "default": "",
    "condition": {
      "rewrite": true
    }
  },
  "history_turns": {
    "description": "参考最近几轮对话",
    "type": "int",
    "default": 6,
    "slider": {
      "min": 0,
      "max": 20,
      "step": 2
    }
  },
  "_sec_search": {
    "description": "=== 识图与联网（可选） ===",
    "type": "text",
    "invisible": true
  },
  "image_search": {
    "description": "认不出时用百度识图",
    "type": "bool",
    "default": true,
    "hint": "需要 bsk（browser-skill）可用。"
  },
  "bsk_path": {
    "description": "bsk.exe 路径",
    "type": "string",
    "default": "",
    "hint": "留空用 PATH 里的 bsk。不填且没装 bsk 时，识图会自动跳过。"
  },
  "bsk_browser": {
    "description": "bsk 浏览器实例 id",
    "type": "string",
    "default": "",
    "hint": "留空自动获取并记住第一个实例。"
  },
  "search_key": {
    "description": "搜索接口 API Key",
    "type": "string",
    "default": "",
    "hint": "留空则不联网补充。"
  },
  "search_api": {
    "description": "搜索接口地址",
    "type": "string",
    "default": "https://api.deepseek.com/v1/tools/search"
  },
  "_sec_link": {
    "description": "=== 网页链接（B站/抖音等） ===",
    "type": "text",
    "invisible": true
  },
  "yt_dlp": {
    "description": "yt-dlp 路径",
    "type": "text",
    "default": "",
    "hint": "留空会自动找 PATH 与常见安装位置；找不到就得填，比如 .agent-reach-venv\\Scripts\\yt-dlp.exe。"
  },
  "ytdlp_seconds": {
    "description": "只下载前几秒",
    "type": "int",
    "default": 30,
    "slider": {
      "min": 5,
      "max": 300,
      "step": 5
    },
    "hint": "整条视频又慢又占盘，看开头几十秒通常就够判断内容。"
  },
  "ytdlp_height": {
    "description": "下载画质上限",
    "type": "int",
    "default": 480,
    "slider": {
      "min": 144,
      "max": 1080,
      "step": 16
    },
    "hint": "视觉模型吃不了太大的图，480 足够。"
  },
  "cookies_browser": {
    "description": "从哪个浏览器取 cookies",
    "type": "string",
    "default": "",
    "options": [
      "",
      "edge",
      "chrome",
      "firefox"
    ],
    "labels": [
      "不带",
      "Edge",
      "Chrome",
      "Firefox"
    ],
    "hint": "抖音这类站点要 cookies 才肯给视频；浏览器正开着时可能读不到（数据库被占），那就靠下面的浏览器兜底。"
  },
  "browser_fallback": {
    "description": "下不下来时用浏览器截帧",
    "type": "bool",
    "default": true,
    "hint": "用 bsk 打开页面，把画面区域截几张图交给视觉模型。抖音基本走这条路。"
  },
  "browser_shots": {
    "description": "浏览器截几帧",
    "type": "int",
    "default": 4,
    "slider": {
      "min": 1,
      "max": 10,
      "step": 1
    },
    "condition": {
      "browser_fallback": true
    }
  },
  "browser_settle": {
    "description": "打开页面后等几秒",
    "type": "int",
    "default": 5,
    "slider": {
      "min": 1,
      "max": 20,
      "step": 1
    },
    "condition": {
      "browser_fallback": true
    },
    "hint": "等页面把播放器渲染出来再截。"
  },
  "persona_data_dir": {
    "type": "string",
    "description": "人格数据目录（可选）",
    "hint": "给 persona_mind 的 plugin_data 目录，用来借她平时的语气与心情；留空按同级插件自动推断",
    "default": ""
  },
  "_sec_see": {
    "type": "text",
    "invisible": true,
    "description": "让她自己看（把画面直接交给主模型）"
  },
  "see_with_llm": {
    "type": "bool",
    "description": "画面直接给你自己看",
    "default": true,
    "hint": "打开后：收到视频/视频链接时不再由插件代你看再转述，而是把截出来的画面直接塞进这一次请求，你用自己的话说。要求主模型是多模态（AstrBot 里该 provider 的 modalities 含 image）。"
  },
  "see_shots": {
    "type": "int",
    "description": "一次给几帧（按台词密度挑，不是等间隔）",
    "default": 10,
    "slider": {
      "min": 1,
      "max": 30,
      "step": 1
    },
    "condition": {
      "see_with_llm": true
    },
    "hint": "抽帧总数在「抽几帧」那项，两处要一起调大；帧多 token 也多。"
  },
  "see_cache_seconds": {
    "type": "int",
    "description": "同一条链接多久内不重复抽帧",
    "default": 600,
    "slider": {
      "min": 60,
      "max": 3600,
      "step": 60
    },
    "condition": {
      "see_with_llm": true
    },
    "hint": "同一条视频反复讨论时复用缓存的帧，省掉一次下载或开浏览器。"
  },
  "see_timeout": {
    "type": "int",
    "description": "一条链接最多等多久（秒）",
    "default": 180,
    "slider": {
      "min": 30,
      "max": 300,
      "step": 10
    },
    "condition": {
      "see_with_llm": true
    },
    "hint": "全片下载 + 抽帧一般 40-90 秒，长片可能更久；太小会让她说「这次没看成」。"
  },
  "subs_enable": {
    "type": "bool",
    "description": "读台词（B站 AI 字幕）",
    "default": true,
    "hint": "有字幕时她会拿到整段原话，按台词密度挑画面；没有就只给画面并如实说。"
  },
  "subs_inject_tokens": {
    "type": "int",
    "description": "台词最多念多少（token）",
    "default": 6000,
    "slider": {
      "min": 1200,
      "max": 20000,
      "step": 200
    },
    "hint": "长片会按这个预算挑段，并在开头说明「这是节选」。"
  }
}
```

## 八、我想被挑刺的地方（按重要性）

1. **`subs.punctuate()` 的断句规则**：AI 字幕整段没有标点，现在按「收尾是 `吗呢吧啊啦了么哦哈呀嘛` 且
   攒够 ≥12 字 → 断句；攒够 ≥12 字 → 逗号；否则直接拼」。纯规则、零成本，但位置不一定对
   （实测会出现「今天的主角9月19号动工，一个月，七个仓库。」这种）。有没有更好的做法？
   用模型重新标点会改写风险 + 成本，值不值？
2. **`picks.plan()` 的权重是拍的**：`0.5·z(字数密度) + 0.15·z(cue频率) + 0.2·z(数字/专名) + 0.15·z(切镜)`，
   20 秒滑窗，一半给密度峰、一半保底锚点，相邻帧有最小间距。**没有标注数据验证过**，
   也没有「这样选出来的帧是不是比等间隔更有用」的对照实验。
3. **时钟安排**：钩子里串行 `探测(60s) → 字幕(25s) → 下载+抽帧(330s)`，但整个钩子由
   `see_timeout=180s` 卡着。25 分钟片实测 45 秒能下完，可长片（>1 小时）会超时 → 整轮失败。
   设计文档里写了「两段式」（先给前 300 秒答一句、后台补全再主动发一条），**这一版没做**。
4. **超预算时是「均匀抽段」而不是「按问题挑段」**：6000 token 预算下，长片会等距取段，
   首尾必留。设计文档 5.6 写的是「按用户问题挑相关段」，实现退化了。
5. **字幕进上下文的通道**：走 `req.extra_user_content_parts` 里的 `TextPart(...).mark_as_temp()`，
   依据是 `astrbot/core/agent/message.py:338` 落库时 `if item.get("_no_save"): continue`、
   `mark_as_temp()` 内部就是 `self._no_save = True`（第 68-70 行）。但 `tool_loop_agent_runner.py:1508/1540`
   **每一步工具调用都会重发 extra_user_content_parts**——同一条消息里她如果连调 3 次工具，
   字幕会被重发 3 次。这个成本账我按「一轮只调一次」估的，没实测。
6. **没做的三块**：ASR 兜底（没字幕就只能给画面）、AI 选帧（第一版砍了）、
   时间戳按需（现在结构上不给时间，只给内容和相对位置词）。

## 九、已知的坑（都是踩出来的，写进注释了）

- `x/player/v2` 有 CDN 缓存：不加 `_t=<时间戳>` 会拿到**上一条视频**的字幕（实测覆盖时长比本片还长）。
- bsk（浏览器自动化 CLI）会复用旧标签页：拿到的 `bvid` 必须和目标的 BV 对一遍，对不上就当没有字幕——
  宁可没字幕，不能张冠李戴。
- 字幕正文要后台 HTTP + `Referer: https://www.bilibili.com/`，在视频页里 `fetch()` 会被 CORS 挡（`Failed to fetch`）。
- `--download-sections` 一次只能给一个区间，不支持逗号分隔多区间；而且产物是 ffmpeg 重切过的独立流、
  时间轴归零，**拿不到原始分片偏移 → 不能续接下载**。
- `--concurrent-fragments 4` 反而慢一倍（32s vs 17s）。
