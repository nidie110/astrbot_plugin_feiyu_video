# astrbot_plugin_feiyu_video

让 bot 真的「看」视频：抽关键帧交给视觉模型，再用她自己的口吻讲出来。

## 特性

- **自动抽帧**：按 `frames` 均匀抽帧，可调宽度压体积，超长视频受 `max_seconds` 限制
- **模型直读**：抽出的帧作为多模态消息交给视觉模型，不需要额外部署
- **认不出会补证**：画面识别不了时用百度识图认物、必要时联网补背景
- **口吻重写**：可选把「事实描述」重写成人格说话方式，避免念说明书
- **两种入口**：群里直接甩视频，或 `/看视频 <路径或链接>`

## 网页链接也能看（1.2.0）

以前只认两样东西：QQ 发来的视频文件、直链 mp4。别人甩一条 `https://www.bilibili.com/video/BV...` 或抖音分享链，她会当成文件去下载，下回来一堆 HTML，然后说「这个视频打不开」。

现在遇到页面链接，按两条路走：

1. **yt-dlp 下前 N 秒**（默认 30 秒、480p）——B 站这类能直接下的，2 秒就能拿到画面
2. **下不下来就让浏览器去看**——用 bsk 打开页面，找到 `video` 元素，把画面区域按时间截几张图（默认 4 张），连标题和简介一起交给视觉模型

第二条是关键：抖音这类站点不给 cookies 就不发视频流（`Fresh cookies (not necessarily logged in) are needed`），而浏览器里是登录状态，页面能正常播——截图就等于「真的看了」。截完会用 video 元素的坐标裁掉黑边和推荐栏，只留画面。

| 站点 | 走哪条 |
|---|---|
| B站（含 b23.tv 短链） | yt-dlp |
| 抖音（含 v.douyin.com 短链） | 直接走浏览器截帧（没配 cookies 时不白试 yt-dlp） |
| 快手 / 小红书 / YouTube | 没配 cookies 直接浏览器；配了就先 yt-dlp |
| 直链 .mp4 / .webm | 直接下载抽帧（老路） |

相关配置：`yt_dlp`、`ytdlp_seconds`、`ytdlp_height`、`cookies_browser`、`browser_fallback`、`browser_shots`、`browser_settle`。

## 安装

1. `pip install -r requirements.txt`（`imageio-ffmpeg`、`Pillow`）
2. 想看 B 站这类链接，还要 `pip install yt-dlp`（不装也能用：抖音走浏览器，其他站点会提示「得先装 yt-dlp」）
3. 目录放到 `astrbot/data/plugins/`
4. 配置里填 `vision_provider_id`（AstrBot 里已配好的多模态模型 id）
5. 重启 AstrBot

## 配置

| 键 | 说明 |
| --- | --- |
| `vision_provider_id` | 视觉模型 id，例如 `deepseek/deepseek-vision` |
| `frames` / `max_seconds` / `frame_width` | 抽帧数量、时长上限、帧宽度 |
| `rewrite` / `rewrite_provider` | 是否重写成口吻、用哪个文本模型 |
| `history_turns` | 参考最近几轮对话来判断「这视频」指什么 |
| `image_search` | 认不出时用识图（需 bsk 可用） |
| `bsk_path` / `bsk_browser` | bsk 路径与实例，留空自动发现 |
| `search_key` / `search_api` | 联网搜索，留空则关闭 |

## 指令与工具

| 类型 | 名称 | 说明 |
| --- | --- | --- |
| 指令 | `/看视频 <路径或链接>` | 抽帧并讲解 |
| 指令 | `/识图` | 认最后一张图/最后一帧 |
| 工具 | `watch_video` | 模型自主调用 |
| 工具 | `identify_last_frame` | 认图 |

## 常见问题

**报 ffmpeg 相关错误**
`imageio-ffmpeg` 没装或没下载到二进制，重装一次即可。

**说「看不到视频」**
`vision_provider_id` 填错，或模型不支持图片输入。看日志里 provider 的实际返回。

**很慢/很贵**
调小 `frames` 和 `frame_width`，或调小 `max_seconds`。

## 许可

MIT，见 [LICENSE](LICENSE)。

## 安全边界

这条链路会驱动 bsk（日常那个浏览器，带登录态），所以做了闸门：

- 只认上表里的站点域名（按 host 后缀匹配，`bilibili.com.evil.com`、`evil.com/?u=bilibili.com` 都不算）
- 内网地址一律拒：`127.*`、`10.*`、`192.168.*`、`169.254.*`、`172.16-31.*`、`localhost`
- 直链媒体文件（`.mp4` 等）也放行，但同样过内网过滤

想加站点就改 `_PLATFORM_HOSTS`，别把闸门删了。

## 让她自己看（1.2.2）

默认打开 `see_with_llm`。收到视频或 B 站/抖音这类页面链接时，插件只负责把画面取出来（yt-dlp 下前 30 秒，或开浏览器截帧），然后把这些帧**直接放进这一次的模型请求**，主模型自己看、自己说。

- 前提：主模型多模态。AstrBot 的 provider 配置里 `modalities` 要含 `image`，否则框架会在请求前把图片丢掉（`tool_loop_agent_runner.py` 里那层判断）。
- 主模型不是多模态时自动退回旧行为：插件用 `vision_provider_id` 看完，转成事实，再用 `_speak` 换她的口气说一遍。
- 同一条链接 10 分钟内复用缓存帧；一次最多 3 帧；单条链接最多等 180 秒。

