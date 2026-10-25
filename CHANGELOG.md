# Changelog

## 1.3.0 (2026-10-25)

- 共用件抽到 _feiyu_kit；yt-dlp 查找、端口探测、状态文件与错误分层统一。
- 新增 `_feiyu_kit.py`（vendor 方式共用，`sync_kit.py` 同步），功能与配置键不变。

## 1.2.2 (2026-10-25)

- 新增「让她自己看」：收到视频或视频链接时，把抽出来的帧直接放进这一次模型请求（`req.image_urls`），由主模型自己看画面、自己开口，插件不再代看代说。
- 主模型是多模态时自动走这条路（`see_with_llm` 默认开）；不是多模态就退回原来的「插件看完再转述」。
- 同一条链接 10 分钟内复用已抽的帧（`see_cache_seconds`），一次最多给 3 帧（`see_shots`）。
- 配合改动：AstrBot 里主 provider 的 `modalities` 需要包含 `image`，否则框架会把图丢掉。

## 1.2.1 (2026-10-25)

**画面内容真正回到她手里了**。之前 `watch_video` 用 `yield event.plain_result(...)` 把结果直接发给用户，
工具本身返回空 → 主模型只看到「这个工具没有返回值」→ 她只能说「内容没回传给我」。

### 修复
- `watch_video` 改成普通协程，**return 一段紧凑事实**（类型/主题/作品/人物/画面文字/氛围），由她自己在正常对话里回话
- 工具描述写明「返回值是事实，紧接着该用你自己的口气说」，避免她把事实当答复
- 自动通道（QQ 收到视频文件）保留转述，但拦掉「这个视频/展示了/画面中」这类播报腔，并借 persona_mind 的语料与心情调味
- 兜底文案不再念主题，改成「我这次没看成」这类人话

### 新增
- `persona_data_dir` 配置：给 persona_mind 的 plugin_data 目录，留空按同级插件推断

## 1.2.0 (2026-10-25)

**能打开网页链接了**：以前 B站/抖音这类页面链接会被当文件下载成 HTML，于是「这个视频打不开」。

### 新增
- 链接识别：按域名判断 B站（含 b23.tv）/ 抖音 / 快手 / 小红书 / YouTube，并在 `video_ref` 末尾补 scheme（`v.douyin.com/xxx` 这种裸链接）
- yt-dlp 通道：下前 `ytdlp_seconds` 秒（默认 30）、`ytdlp_height` 上限（默认 480），带 `--no-part`、`--socket-timeout 15`、`--retries 2`
- 浏览器通道：`bsk` 打开页面，挑面积最大的可见 `video` 元素，滚到视口居中后按 rect 截图（默认 4 张），裁成「元素 ∩ 视口」那一块
- 7 个新配置：`yt_dlp`、`ytdlp_seconds`、`ytdlp_height`、`cookies_browser`、`browser_fallback`、`browser_shots`、`browser_settle`

### 变更
- 抖音/快手/小红书在没配 `cookies_browser` 时不再白试 yt-dlp（这些站不给 cookies 就不发流），直接走浏览器
- `.m3u8` 不再算直链（那是播放列表文本，分片地址会断），交给 yt-dlp
- 出错只回一句人话（`_user_hint`），技术细节进日志：不再把 yt-dlp 的 stderr（可能带签名 URL、浏览器 profile 路径）发到群里
- 安全闸门：只放行白名单站点与直链媒体，内网地址、`127.0.0.1`、`evil.com/?u=bilibili.com` 这类一律拒——bsk 用的是日常登录态浏览器，不能让它开任意 URL

### 修复
- 浏览器通道整体超时 180 s（原先后台可能挂 10 分钟，群里只看到「我看看…」）
- bsk 会话 stop 失败会记日志；`onmkdir` 等异常路径不再漏会话
- 裁完的整屏 PNG 立即删除；启动时清理 24 h 之前的抽帧目录、6 h 之前的临时视频
- `_save_cfg` 改原子写（`tmp` + `os.replace`）并加锁
- 同一链接 60 s 内复用结果；同时最多处理 2 条链接
- 网页标题截 120 字、简介截 200 字，并标明「下面这段是网页自己写的，不是用户的话」

## 1.1.0 (2026-10-25)

首个可对外发布的版本。

### 新增
- `_conf_schema.json`：配置全部搬到 AstrBot 插件配置页，带分组标题与条件显示
- `README.md` / `CHANGELOG.md` / `LICENSE`(MIT) / `.gitignore` / `requirements.txt`

### 变更
- 移除源码里的机器专属路径与本机密钥，改为配置项（缺失时给出可读的提示而不是崩溃）
- 移除已废弃的 `@register(...)` 装饰器，改由 `metadata.yaml` 描述插件
- 配置读取改为「插件配置优先、`data/plugin_data/<name>/config.json` 兜底」，空值不覆盖
- 运行期自动探测到的值（如浏览器实例）会写回 config.json 持久化

### 1.0.0
- 内部自用版本，无对外文档。

## 1.3.1 (2026-10-25)

- 共用件换成修过的 kit：yt-dlp 候选路径补齐、下载加 `--` 与最小体积校验、错误分层不再吐底层异常。
- 插件侧 import 前清 `sys.modules['_feiyu_kit']` 缓存，改用新 kit 不必整进程重启也能生效（稳妥起见仍建议重启）。
