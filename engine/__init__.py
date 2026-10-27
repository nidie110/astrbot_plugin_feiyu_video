"""feiyu_video 的纯函数层（不碰网络、不碰文件、不读配置）。

    subs.py      字幕解析 / 清洗 / 覆盖率 / 分段 / 渲染
    picks.py     按信息密度选抽帧时刻
    watchctx.py  一次观看的产物（WatchContext）与长片任务状态机（WatchTask）

加载方式见 main.py 顶部：以 `feiyu_video_engine` 这个**唯一包名**按路径加载，
避免和别的插件（persona_mind 也有 engine/）抢同一个 `engine` 模块名。
"""

from . import picks, subs, watchctx  # noqa: F401

__all__ = ["subs", "picks", "watchctx"]
