# Changelog

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
