import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import time
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parent))
_KIT_PATH = Path(__file__).resolve().parent / "_feiyu_kit.py"
kit = _sys.modules.get("_feiyu_kit")
if kit is None:
    # 全进程只有一份 kit：动作注册表必须共享，所以谁先加载谁就是那份，后来者直接复用。
    # 磁盘上的 kit 改动要重启整个 AstrBot 进程才生效（面板重载插件不会换掉已经加载的模块）。
    import importlib.util as _ilu  # noqa: PLC0415

    _spec = _ilu.spec_from_file_location("_feiyu_kit", _KIT_PATH)
    _fresh = _ilu.module_from_spec(_spec)
    _sys.modules["_feiyu_kit"] = _fresh
    _spec.loader.exec_module(_fresh)
    kit = _fresh

# ---- 看视频 v3 的纯函数层（字幕/选点/上下文）----
# 用唯一包名按路径加载：persona_mind 也有一个叫 engine 的包，直接 import engine 会抢坑。
_ENGINE_DIR = Path(__file__).resolve().parent / "engine"
_ENGINE_PKG = "feiyu_video_engine"
subs_mod = picks_mod = wctx_mod = None
try:
    if _ENGINE_PKG not in _sys.modules:
        import importlib.util as _ilu2  # noqa: PLC0415

        _espec = _ilu2.spec_from_file_location(
            _ENGINE_PKG,
            _ENGINE_DIR / "__init__.py",
            submodule_search_locations=[str(_ENGINE_DIR)],
        )
        _emod = _ilu2.module_from_spec(_espec)
        _sys.modules[_ENGINE_PKG] = _emod
        _espec.loader.exec_module(_emod)
    _engine = _sys.modules[_ENGINE_PKG]
    subs_mod, picks_mod, wctx_mod = _engine.subs, _engine.picks, _engine.watchctx
except Exception as _ex:  # 纯函数层坏了不能让整个插件起不来
    print(f"[feiyu_video] engine 加载失败（字幕功能会关闭）: {_ex}")

import imageio_ffmpeg
from PIL import Image
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_feiyu_video"
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# 描述腔黑名单：出现这些就不是「她说话」，是「播报」
_BANNED_PHRASES = (
    "这个视频", "这段视频", "该视频", "视频中", "视频里", "画面中", "画面里",
    "展示了", "介绍了", "讲述了", "呈现了", "可以看到", "本视频",
)

_STYLE_RULES = """你在跟朋友聊天，刚看完他甩来的东西。写一条消息，规则：
- 第一句必须是你的反应或判断（笑死/我服了/这个我熟/有点意思/看不懂但大受震撼…），不是描述
- 绝对不要出现：这个视频、这段视频、视频里、画面中、展示了、介绍了、讲述了、可以看到
- 最多两句，加起来不超过 60 个字；能一句说完就一句
- 说人话，口语，可以有情绪、可以吐槽、可以顺手联想到自己或对方
- 不许 Markdown、书名号堆叠、分点、标题、堆感叹号
- 认不出是谁/哪部作品就直说认不出，别编
- 如果对方问了具体问题：先用不超过 20 字答那个问题，再补一句你的看法"""


# B 站页面里一次读完：aid / cid / 时长 / 标题 / 字幕列表。
# `_t` 破缓存是必需的：不加会命中 CDN 缓存，拿到上一个视频的字幕（实测覆盖时长超过本片）。
_BILI_PROBE_JS = r"""(async () => {
  const st = window.__INITIAL_STATE__ || {};
  const v = st.videoData || {};
  const pages = Array.isArray(v.pages) ? v.pages : [];
  let p = 1;
  try { p = parseInt(new URLSearchParams(location.search).get('p') || '1', 10) || 1; } catch (e) {}
  const page = pages[p - 1] || pages[0] || {};
  const aid = v.aid || 0;
  const cid = page.cid || v.cid || 0;
  const out = {
    ok: !!(aid && cid), aid: aid, cid: cid, bvid: v.bvid || '',
    dur: Math.round(page.duration || v.duration || 0),
    title: String(v.title || document.title || '').slice(0, 120),
    desc: String(v.desc || '').slice(0, 300),
    page: p, n_pages: pages.length, subs: []
  };
  if (!out.ok) return JSON.stringify(out);
  try {
    const t = Date.now();
    const r = await fetch('//api.bilibili.com/x/player/v2?aid=' + aid + '&cid=' + cid + '&_t=' + t,
                          { credentials: 'include' });
    const j = await r.json();
    const list = (((j || {}).data || {}).subtitle || {}).subtitles || [];
    out.subs = list.map(x => ({ lan: x.lan, lan_doc: x.lan_doc, url: x.subtitle_url, ai: x.ai_status }));
    out.code = (j || {}).code;
  } catch (e) {
    out.subs_err = String((e && e.message) || e).slice(0, 120);
  }
  return JSON.stringify(out);
})()"""


_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")


def _bvid_of(url: str) -> str:
    """链接里的 BV 号（b23 短链取不到就返回空）。"""
    m2 = _BV_RE.search(str(url or ""))
    return m2.group(1) if m2 else ""


def _facts_prompt(note: str, n_frames: int) -> str:
    """让视觉模型只交事实，不交感想——感想留给人格那一步。"""
    head = (note + "\n") if note else ""
    return (
        head
        + f"这些是从一段视频里按时间顺序抽取的 {n_frames} 帧画面。请提取事实，严格输出 JSON，不要多余文字：\n"
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


def _facts_brief(facts: dict, note: str) -> str:
    """整理成给主模型看的一段紧凑事实，并说明「这是你看到的」。"""
    def _j(key):
        val = facts.get(key)
        if isinstance(val, list):
            val = "、".join(str(x) for x in val if str(x).strip())
        return str(val or "").strip()

    bits = []
    for key in ("类型", "主题", "作品", "角色", "人物", "事件", "时间", "地点", "画面文字", "氛围"):
        val = _j(key)
        if val:
            bits.append(f"{key}：{val[:180]}")
    for key in ("识图结果", "搜番结果", "搜索补充"):
        val = _j(key)
        if val:
            bits.append(f"{key}：{val[:120]}")
    if note:
        bits.append(f"来源页信息：{note[:150]}")
    head = "（这是你看画面看到的，不是别人转述）"
    return head + ("\n" + "\n".join(bits) if bits else "\n（画面没给出可用信息）")

# 机器相关的路径和密钥都不写进代码：走插件配置（_conf_schema.json）或 plugin_data/config.json。
DEFAULT_CONFIG = {
    # 看图用的多模态模型（AstrBot 里配置的 provider_id）
    "vision_provider_id": "",
    "frames": 6,
    "max_seconds": 600,
    "frame_width": 640,
    # bsk.exe（browser-skill CLI，百度识图用）；留空则用 PATH 里的 bsk
    "bsk_path": "",
    "bsk_browser": "",
    "image_search": True,
    # 把事实重写成她自己的口吻
    "rewrite": True,
    "rewrite_provider": "",
    "history_turns": 6,
    # 联网补充信息（key 留空则不联网）
    "search_key": "",
    "search_api": "https://api.deepseek.com/v1/tools/search",
    # ---- 网页链接（B站/抖音/快手/小红书）----
    # yt-dlp 可执行文件；留空则查 PATH 和几个常见安装位置
    "yt_dlp": "",
    # 只下前这么多秒（下整条又慢又占磁盘）
    "ytdlp_seconds": 30,
    # 下载画质上限，够视觉模型看就行
    "ytdlp_height": 480,
    # 某些站要 cookies（如抖音）：edge / chrome / firefox，留空则不带
    "cookies_browser": "",
    # yt-dlp 拿不到时，用 bsk 浏览器开页面截帧
    "browser_fallback": True,
    # 截几帧、页面打开后等几秒
    "browser_shots": 4,
    "browser_settle": 5,
    # ---- 让她自己看：帧直接进主模型的请求，我不替她转述 ----
    "see_with_llm": True,
    "see_shots": 10,
    "see_shots_max": 12,  # 钩子里注入画面的硬上限（再多 token 撑不住）
    "see_cache_seconds": 600,
    # 一条链接最多等多久（秒）；超时就不带帧，别把整条消息卡死
    "see_timeout": 420,
    # 超过这个时长就只同步看前 long_sync_sec 秒（1 小时的片下载约 2 分钟，再长就撑不住一轮回复）
    "long_sync_sec": 3600,
    # ---- 字幕（v3）----
    # 要不要连字幕一起给她。关掉就退化成「只看画面」的老行为。
    "subs_enable": True,
    # 当轮注入的字幕 token 上限（中文 1 字≈0.6 token，6000 约等于一万字）
    "subs_inject_tokens": 6000,
    # 覆盖率低于这个就当「残缺」，要么降级要么标注
    "cover_min_ratio": 0.85,
    "cover_max_gap_sec": 60,
    # 时间戳：off = 上下文里完全没有；on_demand = 她明确问时才给单点
    "time_marks": "off",
}


class FeiyuVideo(Star):
    """大肥鱼·看视频：抽关键帧喂视觉模型，让她真的能「看」视频。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        kit.register_capability("feiyu_video", "能看视频，自己抽几帧出来看画面，看完用我自己的话说", "all")
        kit.register_action("看视频", self.do_watch, "看视频/网页链接的画面，参数 video_ref、question")
        kit.register_action("认人", self._identify_last, "对刚看的画面以图搜图认人")
        self.data_dir = Path("data/plugin_data") / PLUGIN_NAME
        self._last_frames: dict[str, list] = {}
        self._temporary_files: set[str] = set()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.data_dir / "config.json"
        self.plugin_cfg = dict(config or {})
        self.cfg = dict(DEFAULT_CONFIG)
        if self.cfg_path.exists():
            try:
                self.cfg.update(json.loads(self.cfg_path.read_text(encoding="utf-8")))
            except Exception as e:
                logger.warning(f"[feiyu_video] 配置读取失败: {e}")
        else:
            self.cfg_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.plugin_cfg:
            # 插件配置优先于本地 config.json，空值不覆盖
            self.cfg.update({k: v for k, v in self.plugin_cfg.items() if v not in (None, "")})
        self.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        # 链接看片：同一时刻最多两条，最近 60 秒看过的直接复用
        self._video_sem = asyncio.Semaphore(2)
        self._recent_links: dict[str, tuple[float, list[str], str]] = {}
        # 「让她自己看」的帧缓存：ref -> (时间戳, 帧路径, 页面信息)
        self._see_cache: dict[str, tuple[float, list[str], str]] = {}
        self._cfg_lock = threading.Lock()
        self._sweep_leftovers()

    def _sweep_leftovers(self) -> None:
        """启动时扫一遍：过期的抽帧目录、上次没收掉的临时视频。"""
        now = time.time()
        try:
            root = self.data_dir / "frames"
            if root.is_dir():
                for d in root.iterdir():
                    try:
                        if d.is_dir() and now - d.stat().st_mtime > 24 * 3600:
                            shutil.rmtree(d, ignore_errors=True)
                    except OSError:
                        continue
        except OSError as e:
            logger.warning(f"[feiyu_video] 清理抽帧目录失败: {e}")
        try:
            for p in Path(tempfile.gettempdir()).glob("feiyu_video_*"):
                try:
                    if now - p.stat().st_mtime > 6 * 3600:
                        p.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError as e:
            logger.warning(f"[feiyu_video] 清理临时视频失败: {e}")

    def _save_cfg(self) -> None:
        """把当前配置（除插件配置外的本地值）落盘，下次启动直接用。"""
        try:
            with self._cfg_lock:
                tmp = self.cfg_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
                os.replace(tmp, self.cfg_path)
        except Exception as e:
            logger.warning(f"[feiyu_video] 保存配置失败: {e}")

    # ---------- 工具函数 ----------

    def _download(self, url: str) -> str:
        out = os.path.join(tempfile.gettempdir(), f"feiyu_video_{uuid.uuid4().hex}.mp4")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._temporary_files.add(out)
        try:
            with opener.open(url, timeout=120) as r, open(out, "wb") as f:
                f.write(r.read())
        except BaseException:
            self._cleanup_download(out)
            raise
        return out

    def _cleanup_download(self, path: str) -> None:
        temp = getattr(self, "_temporary_files", None)
        if temp is None or path not in temp:
            return
        try:
            Path(path).unlink(missing_ok=True)
            for extra in Path(path).parent.glob(Path(path).name + ".*"):
                extra.unlink(missing_ok=True)       # .part / .part-Frag*
            self._temporary_files.discard(path)
        except OSError as e:
            logger.warning(f"[feiyu_video] 临时文件清理失败: {e}")

    # ---------- 网页链接：B 站 / 抖音 / 快手 / 小红书 ----------
    #
    # 两条路：yt-dlp 下前 N 秒；下不下来就 bsk 开页面截帧。
    # 注意：页面链接只放行下面这张表里的站点——bsk 用的是日常那个浏览器，
    # 带着登录态，不能让它去开任意 URL（内网地址、面板地址一律拒）。

    _MEDIA_EXT = (".mp4", ".m4v", ".mov", ".webm", ".flv", ".mkv", ".ts")

    _PLATFORM_HOSTS = {
        "bilibili": ("bilibili.com", "b23.tv", "bili2233.cn", "bili22.cn"),
        "douyin": ("douyin.com", "iesdouyin.com"),
        "kuaishou": ("kuaishou.com", "kuaishouapp.com", "chenzhongtech.com"),
        "xiaohongshu": ("xiaohongshu.com", "xhslink.com"),
        "youtube": ("youtube.com", "youtu.be", "ytimg.com"),
    }

    @staticmethod
    def _norm_url(url: str) -> str:
        """补 scheme：分享文案里常见 v.douyin.com/xxx/ 这种裸链接。"""
        u = (url or "").strip()
        if u and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
            if re.match(r"^[\w.-]+\.[a-zA-Z]{2,}(/|$|\?)", u):
                u = "https://" + u
        return u

    @staticmethod
    def _host(url: str) -> str:
        try:
            return (urllib.parse.urlparse(url).hostname or "").lower()
        except Exception:
            return ""

    def _platform(self, url: str) -> str:
        """按 host 后缀匹配（不是整串子串，否则 evil.com/?u=bilibili.com 会混进来）。"""
        host = self._host(self._norm_url(url))
        for name, domains in self._PLATFORM_HOSTS.items():
            if any(host == d or host.endswith("." + d) for d in domains):
                return name
        return "other"

    @staticmethod
    def _private_host(host: str) -> bool:
        if not host:
            return True
        if host in ("localhost", "0.0.0.0", "[::1]", "::1"):
            return True
        if host.startswith(("127.", "10.", "192.168.", "169.254.", "0.")):
            return True
        if host.startswith("172."):
            try:
                if 16 <= int(host.split(".")[1]) <= 31:
                    return True
            except (ValueError, IndexError):
                return True
        return False

    def _allowed(self, url: str) -> bool:
        """闸门：只放行白名单站点，以及直链媒体文件。"""
        u = self._norm_url(url)
        if self._private_host(self._host(u)):
            return False
        return self._platform(u) != "other" or self._is_media_url(u)

    def _is_media_url(self, url: str) -> bool:
        """直链视频文件？（m3u8 不算：那是播放列表文本，得交给 yt-dlp）"""
        try:
            path = urllib.parse.urlparse(url).path.lower()
        except Exception:
            path = (url or "").lower()
        return any(path.endswith(ext) for ext in self._MEDIA_EXT)

    def _ytdlp_exe(self) -> str:
        return kit.find_ytdlp(str(self.cfg.get("yt_dlp") or ""))

    def _ytdlp_fetch(self, url: str, full: bool = False, max_sec: int = 0) -> str:
        """yt-dlp 下载，返回本地 mp4 路径；失败抛异常（细节只进日志）。

        full=False 只下前 ytdlp_seconds 秒（预览，快）；full=True 下整片。
        整片才够按字幕密度选点 —— 只下前 30 秒的话后半段一帧都没有。
        max_sec>0 时即使 full=True 也只下前 max_sec 秒（长片兜底）。
        """
        yt = self._ytdlp_exe()
        if not yt:
            raise RuntimeError("no-ytdlp")

        secs = max(5, int(self.cfg.get("ytdlp_seconds", 30) or 30))
        height = max(144, int(self.cfg.get("ytdlp_height", 480) or 480))
        out = os.path.join(tempfile.gettempdir(), f"feiyu_video_{uuid.uuid4().hex}.mp4")
        self._temporary_files.add(out)

        fmt = f"worstvideo[height<={height}]/worstvideo/worst/bv*/b"
        cmd = [yt, "--no-playlist", "--no-warnings", "--no-mtime", "--no-part",
               "--socket-timeout", "15", "--retries", "2",
               "-f", fmt]
        if not full or max_sec > 0:
            # 注意：--download-sections 需要 ffmpeg；整片下载不需要（别顺手带上）
            seg = int(max_sec) if max_sec > 0 else secs
            cmd += ["--download-sections", f"*0-{seg}", "--ffmpeg-location", self.ffmpeg]
        cmd += ["-o", out, url]
        cb = str(self.cfg.get("cookies_browser") or "").strip()
        if cb:
            cmd[1:1] = ["--cookies-from-browser", cb]

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"   # yt-dlp 是 Python 程序，别按 cp936 吐
        limit = 300 if (full or max_sec > 0) else 120
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=limit, env=env,
                               creationflags=SUBPROCESS_FLAGS)
        except subprocess.TimeoutExpired:
            self._cleanup_download(out)
            raise RuntimeError("timeout")

        if p.returncode != 0 or not (os.path.exists(out) and os.path.getsize(out) > 0):
            self._cleanup_download(out)
            lines = [ln for ln in (p.stderr or p.stdout or "").splitlines() if ln.strip()]
            raise RuntimeError("ytdlp-failed: " + (lines[-1][:200] if lines else str(p.returncode)))
        return out

    def _ytdlp_meta(self, url: str) -> dict:
        """只问元数据（时长/标题），不下载。给「先决定要不要截断」用，秒级返回。"""
        yt = self._ytdlp_exe()
        if not yt:
            return {}
        cmd = [yt, "--no-playlist", "--no-warnings", "--skip-download",
               "--print", "%(duration)s|%(title)s", "--", url]
        cb = str(self.cfg.get("cookies_browser") or "").strip()
        if cb:
            cmd[1:1] = ["--cookies-from-browser", cb]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=45, env=env,
                               creationflags=SUBPROCESS_FLAGS)
        except Exception as e:
            logger.info(f"[feiyu_video] 取时长超时/失败 {url[:60]}: {e}")
            return {}
        if p.returncode != 0:
            return {}
        rows = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
        if not rows:
            return {}
        dur_s, _, title = rows[-1].partition("|")
        try:
            dur = float(dur_s)
        except Exception:
            dur = 0.0
        return {"dur": dur, "title": title}

    # bsk 的输出不保证是纯 JSON（前面可能有信息行），按花括号切片取
    @staticmethod
    def _json_slice(text: str) -> dict:
        raw = (text or "").strip()
        try:
            return json.loads(raw)
        except Exception:
            pass
        i, j = raw.find("{"), raw.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(raw[i:j + 1])
            except Exception:
                return {}
        return {}

    def _bsk_grab(self, url: str) -> tuple[str, str, list[str]]:
        """bsk 开页面，把 video 那块截几帧。返回 (标题, 简介, 帧路径)。"""
        bsk_browser = str(self.cfg.get("bsk_browser") or "").strip()
        args = ["session", "start", "--json", "--no-focus"]
        if bsk_browser:
            args += ["--browser", bsk_browser]
        r = self._bsk(*args, timeout=90)
        sid = str(self._json_slice(r.stdout).get("session_id") or "")
        if not sid:
            raise RuntimeError(f"bsk-session-failed: {(r.stderr or r.stdout)[:160]}")

        shots: list[str] = []
        try:
            outdir = self.data_dir / "frames" / uuid.uuid4().hex[:8]
            outdir.mkdir(parents=True, exist_ok=True)
            self._bsk("navigate", url, "--session", sid, timeout=90)
            time.sleep(max(1, int(self.cfg.get("browser_settle", 5) or 5)))

            # 取播放器几何信息：先把元素滚到视口中间，再读 rect，
            # 顺便把 dpr / scrollY 一起带回来，别在 Python 侧反推坐标系。
            js = ("(()=>{const vs=[...document.querySelectorAll('video')]"
                  ".filter(v=>v.offsetWidth>120&&v.offsetHeight>120);"
                  "const v=vs.sort((a,b)=>b.offsetWidth*b.offsetHeight-a.offsetWidth*a.offsetHeight)[0];"
                  "const m=document.querySelector('meta[name=description]');"
                  "const d=m?m.content:'';"
                  "if(!v) return JSON.stringify({ok:false,title:document.title,desc:d});"
                  "try{v.scrollIntoView({block:'center'})}catch(e){};"
                  "try{v.muted=true;v.pause()}catch(e){};"
                  "const r=v.getBoundingClientRect();"
                  "return JSON.stringify({ok:true,title:document.title,desc:d,"
                  "x:r.x,y:r.y,w:r.width,h:r.height,iw:innerWidth,ih:innerHeight,"
                  "dpr:devicePixelRatio,sy:scrollY,dur:v.duration||0})})()")
            info = self._json_slice(self._bsk("evaluate", "--session", sid, "--json",
                                              js, timeout=60).stdout)
            try:
                info = json.loads(info.get("value") or "{}") if "value" in info else info
            except Exception:
                info = {}
            if not info.get("ok"):
                raise RuntimeError("no-video-element")

            n = max(1, int(self.cfg.get("browser_shots", 4) or 4))
            dur = float(info.get("dur") or 0)
            for i in range(n):
                if dur > 0:
                    t = max(0.3, min(dur - 0.2, dur * (i + 0.5) / n))
                    self._bsk("evaluate", "--session", sid, "--json",
                              "(()=>{const v=[...document.querySelectorAll('video')]"
                              ".filter(v=>v.offsetWidth>120)[0];"
                              f"if(v){{try{{v.currentTime={t:.2f}}}catch(e){{}}}};return 1}})()",
                              timeout=30)
                    time.sleep(0.9)
                shot = outdir / f"b{i}.png"
                self._bsk("screenshot", "--session", sid, "--out", str(shot), timeout=90)
                if shot.exists() and shot.stat().st_size > 0:
                    shots.append(str(self._crop_video_area(shot, info, outdir / f"c{i}.jpg")))
        finally:
            try:
                self._bsk("session", "stop", sid, timeout=30)
            except Exception as e:
                logger.warning(f"[feiyu_video] bsk 会话没停掉 sid={sid}: {e}")
        return str(info.get("title") or "")[:120], str(info.get("desc") or "")[:300], shots

    def _subs_text(self, ctx) -> str:
        """把字幕排版成自然段引文；超预算就均匀抽段并声明是节选。"""
        if subs_mod is None or ctx is None or not ctx.subs or not ctx.subs.cues:
            return ""
        try:
            tokens = max(600, int(self.cfg.get("subs_inject_tokens") or 6000))
            max_chars = int(tokens / 0.62)  # 中文约 0.62 token/字
            segs = subs_mod.segment(ctx.subs.cues)
            total = sum(x.chars for x in segs)
            long = "long" in (getattr(ctx, "errors", None) or [])
            declared = "part" if (total > max_chars or long) else "full"
            return subs_mod.render(ctx.subs, segs, declared=declared, max_chars=max_chars)
        except Exception as e:
            logger.warning(f"[feiyu_video] 字幕排版失败: {e}")
            return ""

    def _inject_temp(self, req, text: str) -> bool:
        """只对这一轮有效的动态内容：字幕、提示语都走这里。

        **绝不写 system_prompt** —— 那是会话前缀，每轮变一次就把前缀缓存全废掉，
        而且这些内容只对当前这一轮有意义。框架没有 TextPart 时宁可不注入并记日志，
        也不退回 system_prompt。
        """
        if not text:
            return False
        try:
            from astrbot.core.agent.message import TextPart
        except Exception:
            logger.warning(
                "[feiyu_video] 这个 AstrBot 版本没有 TextPart，本轮动态内容跳过（不写 system_prompt）"
            )
            return False
        try:
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
            return True
        except Exception as e:  # 注入失败不能把这一轮带崩
            logger.warning(f"[feiyu_video] 临时内容注入失败（这一轮跳过）: {e}")
            return False

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
        long_note = self._long_note(ctx)
        if long_note:
            body = body + long_note
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
        if ctx.frames and "hook" in str(getattr(ctx, "how", "")):
            parts.append(f"（这一轮你手上有 {len(ctx.frames)} 张画面，就在上面。）")
        if not parts:
            if wctx_mod is not None:
                return wctx_mod.describe_phase(ctx.phase)
            return "这次没取到东西。直说没看成。"
        long_note = self._long_note(ctx)
        if long_note:
            parts.append(long_note)
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
            # 页面刚打开时 x/player/v2 偶尔只回一条默认字幕（登录态还没挂上 / 接口慢），
            # 隔两秒再问一次通常就齐了（实测同一视频 5 秒时 1 条、6 秒时 6 条）。
            for _ in range(2):
                if len(info.get("subs") or []) > 1 or not info.get("ok"):
                    break
                time.sleep(2)
                try:
                    raw2 = self._bsk(
                        "evaluate", "--session", sid, "--json", _BILI_PROBE_JS, timeout=90
                    ).stdout
                    nxt = self._json_slice(raw2)
                    nxt = json.loads(nxt.get("value") or "{}") if "value" in nxt else nxt
                except Exception as e:
                    logger.info(f"[feiyu_video] 字幕重试失败（不影响画面）: {e}")
                    break
                if nxt.get("ok"):
                    info = nxt
                    if len(info.get("subs") or []) > 1:
                        logger.info("[feiyu_video] 字幕列表重试后有内容了")
                        break
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

    def _download_full(self, url: str, max_sec: int = 0) -> tuple[str, float]:
        """整片（或前 max_sec 秒）下到本地，返回 (路径, 时长)。失败抛异常。

        单独拆出来是为了让它能跟「探测 + 取字幕」并行跑 ——
        串行时最坏 60(探测) + 25(字幕) + 330(下载抽帧) = 415 秒，90 秒配置下功能直接不可用。
        """
        path = self._ytdlp_fetch(url, full=True, max_sec=max_sec)
        dur = self._duration(path)
        if dur <= 0 and max_sec > 0:
            dur = float(max_sec)
        return path, dur

    def _pick_frames(self, path: str, dur: float, doc, n: int = 0) -> tuple[list, list]:
        """按台词密度选点并定点抽帧。返回 (帧, 选点)。

        字幕**不完整**（覆盖率不够、中间断层、只到前四分之一）时不能按密度抽：
        残缺字幕会把所有帧都吸到有字幕的那一段，后半段一帧都没有。这种情况退回等间隔。
        """
        n = n or self._shot_count()
        # 长片只下了前段：字幕还是全片的，超出行长的那几条不能进选点，
        # 否则 _bucket 会把它们全塞进末桶、_extract_at 抽空帧（静默少帧）。
        cues = [c for c in (doc.cues if doc is not None else []) if c.mid <= dur + 1.0]
        if doc is not None and doc.cover.ok and cues:
            spots = picks_mod.plan(cues, dur, n=n)
            how = "按台词密度选点"
        else:
            spots = picks_mod.even_spots(dur, n)
            how = "等间隔"
            if doc is not None:
                logger.info(f"[feiyu_video] 字幕不完整（{doc.cover.reason()}），抽帧退回等间隔")
        frames = self._extract_at(path, [sp.t for sp in spots])
        logger.info(f"[feiyu_video] {how} {picks_mod.describe(spots)} · 片子 {dur:.0f}s")
        return frames, spots

    def _frames_by_plan(self, url: str, meta: dict, doc, *, max_sec: int = 0) -> tuple[list[str], float, list]:
        """下整片 → 选点 → 定点抽帧（串行版外壳）。失败返回 ([], 0, [])。

        现在 _watch_gather 走的是并行版（_download_full + _pick_frames）；
        这里留着是为了外部调用方和单测不炸。
        """
        path = ""
        try:
            path, dur = self._download_full(url, max_sec=max_sec)
            if dur <= 0:
                dur = float(meta.get("dur") or 0)
                if max_sec > 0:
                    dur = min(dur, float(max_sec))
            frames, spots = self._pick_frames(path, dur, doc)
            return frames, dur, spots
        except Exception as e:
            logger.warning(f"[feiyu_video] 整片抽帧失败 {url[:80]}: {e}")
            return [], 0.0, []
        finally:
            if path:
                self._cleanup_download(path)

    def _watch_cache_get(self, ref: str):
        """同一条视频在 see_cache_seconds 内复用上一轮的 ctx。

        hook 会在**每次工具调用后的续跑**再触发一次，没有这层缓存的话，
        她追问一句「第二个人说啥」就要再等一整轮下载。
        """
        import time as _time

        cache = self.__dict__.setdefault("_watch_cache", {})
        hit = cache.get(ref)
        if not hit:
            return None
        ts, ctx = hit
        if _time.time() - ts > max(30, int(self.cfg.get("see_cache_seconds") or 600)):
            cache.pop(ref, None)
            return None
        return ctx

    def _watch_cache_put(self, ref: str, ctx) -> None:
        import time as _time

        cache = self.__dict__.setdefault("_watch_cache", {})
        cache[ref] = (_time.time(), ctx)
        if len(cache) > 40:
            for k in sorted(cache, key=lambda x: cache[x][0])[:15]:
                cache.pop(k, None)

    def _long_note(self, ctx) -> str:
        """长片只看了前段时的那句实话；没截断就返回空串。"""
        if ctx is None or "long" not in (getattr(ctx, "errors", None) or []):
            return ""
        cap = max(120, int(self.cfg.get("long_sync_sec") or 3600))
        mins = max(1, int(round(cap / 60)))
        return (
            f"这条片子太长，画面只看了前 {mins} 分钟——后面的台词你手上有、画面没有。"
            f"他要是问后半段的东西，就说画面没跟到那儿，别替他补。"
        )

    def _shot_count(self) -> int:
        """这一轮给几张画面：配置值，但卡在硬上限内。"""
        cap = int(DEFAULT_CONFIG.get("see_shots_max") or 12)
        try:
            want = int(self.cfg.get("see_shots") or DEFAULT_CONFIG.get("see_shots") or 10)
        except Exception:
            want = 10
        return max(1, min(cap, want))

    async def _subs_chain(self, url: str, ctx):
        """字幕那条链：取页面信息 → 取字幕正文。失败只记错误，不抛。"""
        meta: dict = {}
        try:
            meta, subs_list = await asyncio.wait_for(
                asyncio.to_thread(self._bili_probe, url), timeout=60
            )
        except Exception as e:
            logger.warning(f"[feiyu_video] 取页面信息失败 {url[:80]}: {e}")
            ctx.add_error("probe")
            return meta, None
        if meta:
            ctx.dur = float(meta.get("dur") or 0)
        if not subs_list:
            return meta, None
        try:
            doc = await asyncio.wait_for(
                asyncio.to_thread(self._subs_doc, meta, subs_list), timeout=25
            )
        except Exception as e:
            logger.warning(f"[feiyu_video] 字幕处理失败: {e}")
            ctx.add_error("subs")
            return meta, None
        if doc is None:
            ctx.add_error("subs")
            return meta, None
        ctx.subs = doc
        for w in subs_mod.self_check(doc):
            logger.info(f"[feiyu_video] 字幕可疑：{w}")
        return meta, doc

    async def _picture_chain(self, url: str, ctx):
        """画面那条链：先问时长 → 决定要不要截断 → 下整片。返回 (路径, 时长)。

        这条链**不依赖字幕**，所以能和字幕链同时跑：总耗时从「三段相加」变成「取较慢的一条」。
        时长用 yt-dlp 的元数据问（秒级），不为它去等 bsk 探测那 20-60 秒。
        """
        total = 0.0
        try:
            quick = await asyncio.wait_for(asyncio.to_thread(self._ytdlp_meta, url), timeout=45)
            total = float(quick.get("dur") or 0)
        except Exception as e:
            logger.info(f"[feiyu_video] 取时长失败（不影响下载）{url[:60]}: {e}")
        cap = max(120, int(self.cfg.get("long_sync_sec") or 3600))
        max_sec = 0
        if total > cap:
            max_sec = cap
            ctx.add_error("long")
            logger.info(
                f"[feiyu_video] 片子 {total:.0f}s 超过 {cap}s，这一轮只看前 {cap}s"
                f"（后面的等主人问起来再说，不假装全片都看了）"
            )
        try:
            async with self._video_sem:
                path, dur = await asyncio.wait_for(
                    asyncio.to_thread(self._download_full, url, max_sec), timeout=360
                )
            return path, (dur if dur > 0 else total)
        except asyncio.TimeoutError:
            logger.warning(f"[feiyu_video] 下载超时 {url[:80]}")
            ctx.add_error("frames-timeout")
        except Exception as e:
            logger.warning(f"[feiyu_video] 下载失败 {url[:80]}: {e}")
            ctx.add_error("frames")
        return "", 0.0

    async def _watch_gather(self, ref: str, *, how: str = "tool", ctx=None):
        """一次观看的全部产物：字幕 + 帧。hook 与 tool 共用同一份。

        失败一律降级（不抛）：拿不到字幕就只要画面，拿不到画面就只要字幕。
        **ctx 可以由调用方先建好传进来** —— 外面用 wait_for 包着的时候，
        超时会把整个协程取消掉，只有 ctx 在调用方手里，已经读到的东西才留得下来。

        两条链并行：字幕（bsk 探测 + 取正文）与画面（问时长 + 下整片）互不等待，
        都到齐了才做「按台词选点 + 抽帧」—— 那一步是本地 ffmpeg，十几秒。
        """
        if wctx_mod is None:
            return None
        if ctx is None:
            ctx = wctx_mod.WatchContext(ref=ref, how=how)
        url = ref if str(ref).startswith(("http://", "https://")) else ""

        if not (url and self._platform(url) == "bilibili" and self.cfg.get("subs_enable", True)):
            try:
                frames, note = await self._frames_for_llm(ref)
                ctx.frames = frames
                if note:
                    logger.info(f"[feiyu_video] 页面自带说明（不进上下文）: {note[:80]}")
                if not frames:
                    ctx.add_error("frames")
            except Exception as e:
                logger.warning(f"[feiyu_video] 取画面失败 {url[:80]}: {e}")
                ctx.add_error("frames")
            logger.info(f"[feiyu_video] 观看结果 {ctx.brief()}")
            return ctx

        subs_task = asyncio.create_task(self._subs_chain(url, ctx))
        pic_task = asyncio.create_task(self._picture_chain(url, ctx))
        results = await asyncio.gather(subs_task, pic_task, return_exceptions=True)

        meta, doc = {}, None
        if isinstance(results[0], tuple):
            meta, doc = results[0]
        elif results[0] is not None:
            logger.warning(f"[feiyu_video] 字幕链异常: {results[0]}")
        path, dur = "", 0.0
        if isinstance(results[1], tuple):
            path, dur = results[1]
        elif results[1] is not None:
            logger.warning(f"[feiyu_video] 画面链异常: {results[1]}")

        if dur > 0:
            ctx.dur = dur
        elif meta.get("dur"):
            ctx.dur = float(meta.get("dur") or 0)

        try:
            if path and dur > 0 and picks_mod is not None:
                frames, spots = await asyncio.to_thread(self._pick_frames, path, dur, ctx.subs)
                ctx.frames = frames
                ctx.plan = spots
        except Exception as e:
            logger.warning(f"[feiyu_video] 抽帧失败 {url[:80]}: {e}")
            ctx.add_error("frames")
        finally:
            if path:
                self._cleanup_download(path)

        if not ctx.frames:
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
        # B3：同一条视频短时间内反复出现（hook 会在每次工具调用后的续跑再触发），
        # 直接复用上一轮的产物，不重新下载。
        ctx = self._watch_cache_get(ref)
        if ctx is None:
            if wctx_mod is None:
                return
            # B1：ctx 必须在 wait_for 外面建。超时会把整个协程取消掉，
            # ctx 只有在我们手上，已经读到的字幕才不会跟着一起没。
            ctx = wctx_mod.WatchContext(ref=ref, how="hook")
            budget = max(float(self.cfg.get("see_timeout") or 420), 480.0)
            try:
                await asyncio.wait_for(
                    self._watch_gather(ref, how="hook", ctx=ctx), timeout=budget
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[feiyu_video] 自己看：超过 {budget:.0f}s，用已经拿到的部分 {ref[:80]}"
                )
            except Exception as e:
                logger.warning(f"[feiyu_video] 自己看：失败 {ref[:80]}: {e}")
            self._watch_cache_put(ref, ctx)
            if not ctx.subs and not ctx.frames:
                return

        can_image = self._main_supports_image(event)
        handed = 0
        if can_image and ctx.frames:
            picked = list(ctx.frames)[: self._shot_count()]
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
        if text and self._inject_temp(req, text):
            handed += 1

        if not handed:
            logger.info(f"[feiyu_video] 这次什么都没取到 · {ctx.brief()} · {ref[:80]}")
            return
        hint = self._watch_hint(ctx, handed=handed, can_image=can_image)
        if not self._inject_temp(req, hint):
            logger.info("[feiyu_video] 提示语没注进去（没有临时通道），这轮只有原始内容")
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
