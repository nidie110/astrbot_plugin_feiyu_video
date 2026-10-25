# astrbot_plugin_feiyu_video

让 bot 真的「看」视频：抽关键帧交给视觉模型，再用她自己的口吻讲出来。

## 特性

- **自动抽帧**：按 `frames` 均匀抽帧，可调宽度压体积，超长视频受 `max_seconds` 限制
- **模型直读**：抽出的帧作为多模态消息交给视觉模型，不需要额外部署
- **认不出会补证**：画面识别不了时用百度识图认物、必要时联网补背景
- **口吻重写**：可选把「事实描述」重写成人格说话方式，避免念说明书
- **两种入口**：群里直接甩视频，或 `/看视频 <路径或链接>`

## 安装

1. `pip install -r requirements.txt`（`imageio-ffmpeg`、`Pillow`）
2. 目录放到 `astrbot/data/plugins/`
3. 配置里填 `vision_provider_id`（AstrBot 里已配好的多模态模型 id）
4. 重启 AstrBot

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
