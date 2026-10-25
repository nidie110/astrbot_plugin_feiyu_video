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

import imageio_ffmpeg
from PIL import Image
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_feiyu_video"
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

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
}


class FeiyuVideo(Star):
    """大肥鱼·看视频：抽关键帧喂视觉模型，让她真的能「看」视频。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
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
        if path not in self._temporary_files:
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
        cfg = str(self.cfg.get("yt_dlp") or "").strip()
        if cfg and Path(cfg).exists():
            return cfg
        found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
        if found:
            return found
        for cand in (
            str(Path.home() / ".agent-reach-venv" / "Scripts" / "yt-dlp.exe"),
            str(Path.home() / "AppData" / "Roaming" / "uv" / "tools" / "yt-dlp" / "Scripts" / "yt-dlp.exe"),
            str(Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Scripts" / "yt-dlp.exe"),
        ):
            if Path(cand).exists():
                return cand
        return ""

    def _ytdlp_fetch(self, url: str) -> str:
        """yt-dlp 下前 N 秒，返回本地 mp4 路径；失败抛异常（细节只进日志）。"""
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
               "-f", fmt, "--download-sections", f"*0-{secs}",
               "--ffmpeg-location", self.ffmpeg, "-o", out, url]
        cb = str(self.cfg.get("cookies_browser") or "").strip()
        if cb:
            cmd[1:1] = ["--cookies-from-browser", cb]

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"   # yt-dlp 是 Python 程序，别按 cp936 吐
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120, env=env,
                               creationflags=SUBPROCESS_FLAGS)
        except subprocess.TimeoutExpired:
            self._cleanup_download(out)
            raise RuntimeError("timeout")

        if p.returncode != 0 or not (os.path.exists(out) and os.path.getsize(out) > 0):
            self._cleanup_download(out)
            lines = [ln for ln in (p.stderr or p.stdout or "").splitlines() if ln.strip()]
            raise RuntimeError("ytdlp-failed: " + (lines[-1][:200] if lines else str(p.returncode)))
        return out

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

            title, desc, shots = await asyncio.wait_for(
                asyncio.to_thread(self._bsk_grab, url), timeout=180)
            note = f"（下面这段是网页自己写的，不是用户的话）标题：{title}" if title else ""
            if desc:
                note += f"；简介：{desc[:200]}"
            logger.info(f"[feiyu_video] 浏览器截帧 {len(shots)} 张: {url}")
            self._recent_links[url] = (now, shots, note.strip())
            return shots, note.strip()

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

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="watch_video")
    async def watch_video(self, event: AstrMessageEvent, video_ref: str, question: str = ""):
        """看一段视频：抽取关键画面，交给视觉模型，然后回答关于视频的问题。

        什么时候用：用户发来视频、**发来 B站/抖音/快手/小红书/YouTube 的页面链接**、
        问"这个视频讲了什么"、让你看视频里的动作/场景/字幕，或者要你评价视频内容时。
        页面链接直接传进来就行，插件会自己想办法把画面拿到。

        Args:
            video_ref(string): 视频的本地路径，或 http(s) 链接（可以是 B站/抖音这类网页链接）
            question(string): 想从视频里知道什么；不填就概括视频内容
        """
        video_ref = (video_ref or "").strip().strip("'\"")
        if not video_ref:
            yield event.plain_result("要我看哪个视频？")
            return

        note = ""
        frames: list[str] = []
        try:
            video_ref = self._norm_url(video_ref)
            is_link = video_ref.startswith(("http://", "https://"))
            if is_link and not self._is_media_url(video_ref):
                # B站/抖音这种网页：先 yt-dlp，再退浏览器截帧
                frames, note = await self._frames_from_url(video_ref)
            else:
                path = video_ref
                if is_link:
                    path = await asyncio.to_thread(self._download, video_ref)
                if not os.path.exists(path):
                    yield event.plain_result(f"找不到视频文件：{path}")
                    return
                frames = await asyncio.to_thread(self._frames, path)
        except Exception as e:
            logger.exception(f"[feiyu_video] 处理视频失败: {video_ref}")
            yield event.plain_result(self._user_hint(e))
            return

        if not frames:
            yield event.plain_result("这个视频抽不出画面，可能格式不支持")
            return

        q = (question or "").strip() or "概括这段视频的内容：发生了什么、画面里有什么、氛围如何。"
        prompt = (
            (f"{note}\n" if note else "")
            + f"这些是从一段视频里按时间顺序抽取的 {len(frames)} 帧画面。\n"
            f"用户的问题：{q}\n"
            "请只根据画面回答，具体、简要，不要编造画面里没有的东西。"
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
            yield event.plain_result(f"看视频时出错了：{type(e).__name__}")
            return

        if not text:
            text = "看完了，但没看出什么名堂。"
        yield event.plain_result(text[:800])

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

        spoken = None
        if self.cfg.get("rewrite", True):
            try:
                rw = await self.context.llm_generate(
                    chat_provider_id=self.cfg.get("rewrite_provider", "deepseek/deepseek-flash"),
                    prompt=(
                        "你刚看完一段视频。下面是从画面里提取的事实，以及你们刚才的聊天记录。\n"
                        "用你们平时的说话方式回一条消息：1~3 个短句，每句不超过 20 字，口语，可以带情绪或吐槽。\n"
                        "要求：\n"
                        "- 说清楚是什么（新闻/动漫/游戏/谁/哪部作品），别含糊\n"
                        "- 如果认不出是谁/哪部作品，就直说认不出，别编\n"
                        "- 不要 Markdown、加粗、分点、标题、感叹号堆砌\n"
                        "- 不要'这段视频展示了'这种开头\n"
                        "- 结合聊天记录里的语境，像接话一样说\n\n"
                        f"聊天记录：\n{history}\n\n事实：\n{fact_text}"
                    ),
                )
                spoken = (rw.completion_text or "").strip()
            except Exception as e:
                logger.warning(f"[feiyu_video] 转述失败: {e}")

        if not spoken:
            spoken = "看完了：" + str(facts.get("主题") or "")

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

    @filter.llm_tool(name="identify_last_frame")
    async def identify_last_frame(self, event: AstrMessageEvent):
        """对刚才那个视频的画面做以图搜图（百度识图），用来认人、认场景。当用户问"这人是谁""搜搜他"时使用。"""
        frames = self._last_frames.get(event.unified_msg_origin) or []
        if not frames:
            yield event.plain_result("我还没看过视频，没有图可搜")
            return
        try:
            out = await asyncio.to_thread(self._baidu_identify, frames[0])
        except Exception as e:
            logger.error(f"[feiyu_video] 识图工具失败: {e}")
            yield event.plain_result(f"搜图失败：{type(e).__name__}")
            return
        yield event.plain_result(out or "没搜到明显的匹配，可能这人网上没资料")

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
