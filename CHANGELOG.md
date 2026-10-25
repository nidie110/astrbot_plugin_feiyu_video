# Changelog

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
