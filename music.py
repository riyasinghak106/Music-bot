import os
import re
import io
import json
import time
import shutil
import random
import sqlite3
import asyncio
from collections import deque
from typing import Optional

import aiohttp
import yt_dlp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

# ==============================================================================
# CONFIGURATION
# ==============================================================================
TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
AUTHORIZED_OWNER_ID = 1443810072265363518

DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
MUSIC_DB_FILE = os.path.join(DATA_DIR, "music_data.db")

YTDL_OPTIONS = {
    "format": "bestaudio*/bestaudio/best",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": False,
    "extract_flat": "in_playlist",
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "extractor_args": {
        "youtube": {
            "player_client": ["android_vr", "web", "tv_embedded"],
        }
    },
}

AUDIO_FILTERS = {
    "off": {"label": "Normal (No Filter)", "af": ""},
    "bassboost": {"label": "Bassboost", "af": "bass=g=12:f=110:w=0.6"},
    "nightcore": {"label": "Nightcore", "af": "asetrate=48000*1.22,aresample=48000,atempo=1.05"},
    "vaporwave": {"label": "Slowed + Reverb", "af": "asetrate=48000*0.84,aresample=48000,aecho=0.8:0.88:60:0.4"},
    "8d": {"label": "8D Surround Audio", "af": "apulsator=hz=0.125"},
    "karaoke": {"label": "Vocal Reducer", "af": "stereotools=mlev=0.015625"},
}

MUSIC_COMMAND_NAMES = {
    "play", "p", "search", "autoplay", "ap", "247", "24x7", "stay",
    "volume", "vol", "volup", "volumeup", "voldown", "volumedown",
    "loop", "shuffle", "skip", "next", "pause", "resume",
    "queue", "q", "nowplaying", "np", "stop", "dc", "leave",
    "vcrole", "voicerole", "autorolevc",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


def _get_ffmpeg_path() -> Optional[str]:
    """Finds system ffmpeg or falls back to the bundled imageio-ffmpeg binary."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return None


# ==============================================================================
# SQLITE PERSISTENCE (24/7 MODE, AUTOPLAY, VOLUME, VC AUTO-ROLE)
# ==============================================================================
def init_music_db():
    with sqlite3.connect(MUSIC_DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS music_settings (
                guild_id INTEGER PRIMARY KEY,
                mode_247 INTEGER DEFAULT 0,
                voice_channel_id INTEGER DEFAULT NULL,
                text_channel_id INTEGER DEFAULT NULL,
                autoplay INTEGER DEFAULT 0,
                volume INTEGER DEFAULT 70,
                vc_role_id INTEGER DEFAULT NULL
            )
        """)
        cur.execute("PRAGMA table_info(music_settings)")
        columns = [row[1] for row in cur.fetchall()]
        if "vc_role_id" not in columns:
            cur.execute("ALTER TABLE music_settings ADD COLUMN vc_role_id INTEGER DEFAULT NULL")
        conn.commit()


def get_music_config(guild_id: int) -> dict:
    with sqlite3.connect(MUSIC_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM music_settings WHERE guild_id = ?", (guild_id,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO music_settings (guild_id) VALUES (?)", (guild_id,))
            conn.commit()
            cur.execute("SELECT * FROM music_settings WHERE guild_id = ?", (guild_id,))
            row = cur.fetchone()
        return dict(row)


def update_music_config(guild_id: int, **kwargs):
    with sqlite3.connect(MUSIC_DB_FILE) as conn:
        cur = conn.cursor()
        for k, v in kwargs.items():
            cur.execute(f"UPDATE music_settings SET {k} = ? WHERE guild_id = ?", (v, guild_id))
        conn.commit()


def get_all_247_guilds() -> list[dict]:
    with sqlite3.connect(MUSIC_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM music_settings WHERE mode_247 = 1 AND voice_channel_id IS NOT NULL")
        return [dict(r) for r in cur.fetchall()]


def _can_manage_vc_role(member: discord.Member) -> bool:
    if member.id == AUTHORIZED_OWNER_ID:
        return True
    if not member.guild:
        return False
    if member.id == member.guild.owner_id or member.guild_permissions.administrator or member.guild_permissions.manage_roles:
        return True
    return any(
        r.name.lower() in ["staff", "ticket manager", "moderator", "mod", "admin", "support team"]
        for r in member.roles
    )


# ==============================================================================
# PILL-SHAPED PLAYER CARD GENERATOR (PILLOW)
# ==============================================================================
def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    )
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _format_duration(seconds: Optional[int]) -> str:
    if not seconds or seconds < 0:
        return "0:00"
    seconds = int(seconds)
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def _truncate_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while len(text) > 1 and draw.textlength(text + "...", font=font) > max_width:
        text = text[:-1]
    return text + "..."


async def generate_music_card(
    title: str,
    artist: str,
    thumbnail_url: Optional[str],
    elapsed_sec: int,
    duration_sec: int,
    volume_pct: int,
    active_filter: str = "off",
) -> discord.File:
    thumb_bytes = None
    if thumbnail_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(thumbnail_url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        thumb_bytes = await resp.read()
        except Exception:
            pass

    def _render() -> io.BytesIO:
        W, H = 920, 260
        radius = H // 2

        if thumb_bytes:
            try:
                art_raw = Image.open(io.BytesIO(thumb_bytes)).convert("RGB")
                bg = ImageOps.fit(art_raw, (W, H), method=Image.Resampling.LANCZOS)
                bg = bg.filter(ImageFilter.GaussianBlur(radius=28))
                overlay = Image.new("RGBA", (W, H), (16, 18, 26, 205))
                canvas = Image.alpha_composite(bg.convert("RGBA"), overlay)
            except Exception:
                art_raw = None
                canvas = Image.new("RGBA", (W, H), (22, 24, 34, 255))
        else:
            art_raw = None
            canvas = Image.new("RGBA", (W, H), (22, 24, 34, 255))

        sheen = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        s_draw = ImageDraw.Draw(sheen)
        for x in range(W):
            alpha = int(35 * (1 - x / W))
            s_draw.line([(x, 0), (x, H)], fill=(70, 95, 110, alpha))
        canvas = Image.alpha_composite(canvas, sheen)

        art_size = 186
        art_x = 46
        art_y = (H - art_size) // 2
        if art_raw:
            cover = ImageOps.fit(art_raw, (art_size, art_size), method=Image.Resampling.LANCZOS).convert("RGBA")
        else:
            cover = Image.new("RGBA", (art_size, art_size), (45, 48, 68, 255))
            c_draw = ImageDraw.Draw(cover)
            c_draw.ellipse((43, 43, 143, 143), outline=(124, 92, 255, 255), width=6)
            c_draw.ellipse((81, 81, 105, 105), fill=(124, 92, 255, 255))

        art_mask = Image.new("L", (art_size, art_size), 0)
        ImageDraw.Draw(art_mask).rounded_rectangle((0, 0, art_size, art_size), radius=28, fill=255)
        canvas.paste(cover, (art_x, art_y), art_mask)

        draw = ImageDraw.Draw(canvas)

        font_title = _load_font(36, bold=True)
        font_artist = _load_font(23, bold=False)
        font_meta = _load_font(19, bold=True)

        text_x = art_x + art_size + 32
        max_text_w = W - text_x - 65

        clean_title = _truncate_text(draw, title, font_title, max_text_w)
        clean_artist = _truncate_text(draw, artist, font_artist, max_text_w)

        draw.text((text_x, 54), clean_title, font=font_title, fill=(255, 255, 255, 255))
        draw.text((text_x, 104), clean_artist, font=font_artist, fill=(205, 210, 222, 235))

        if active_filter != "off":
            filter_label = AUDIO_FILTERS.get(active_filter, {}).get("label", active_filter.upper())
            draw.text((text_x, 138), f"Filter: {filter_label}", font=_load_font(16, bold=True), fill=(155, 130, 255, 255))

        bar_y = 186
        bar_h = 10
        prog_x = text_x
        prog_w = 240

        draw.rounded_rectangle((prog_x, bar_y, prog_x + prog_w, bar_y + bar_h), radius=5, fill=(65, 62, 82, 255))
        ratio = min(1.0, max(0.0, elapsed_sec / duration_sec)) if duration_sec > 0 else 0.0
        fill_w = int(prog_w * ratio)
        if fill_w > 6:
            draw.rounded_rectangle((prog_x, bar_y, prog_x + fill_w, bar_y + bar_h), radius=5, fill=(124, 92, 255, 255))

        time_str = f"{_format_duration(elapsed_sec)}/{_format_duration(duration_sec)}"
        time_x = prog_x + prog_w + 14
        draw.text((time_x, bar_y - 6), time_str, font=font_meta, fill=(225, 228, 238, 255))

        spk_x = time_x + int(draw.textlength(time_str, font=font_meta)) + 26
        spk_color = (124, 92, 255, 255)
        draw.polygon(
            [
                (spk_x, bar_y + 1),
                (spk_x + 6, bar_y + 1),
                (spk_x + 13, bar_y - 5),
                (spk_x + 13, bar_y + 15),
                (spk_x + 6, bar_y + 9),
                (spk_x, bar_y + 9),
            ],
            fill=spk_color,
        )
        draw.line([(spk_x + 17, bar_y + 1), (spk_x + 17, bar_y + 9)], fill=spk_color, width=2)
        draw.line([(spk_x + 21, bar_y - 2), (spk_x + 21, bar_y + 12)], fill=spk_color, width=2)

        vol_bar_x = spk_x + 32
        vol_bar_w = 95
        draw.rounded_rectangle((vol_bar_x, bar_y, vol_bar_x + vol_bar_w, bar_y + bar_h), radius=5, fill=(65, 62, 82, 255))
        vol_fill = int(vol_bar_w * min(1.0, max(0.0, volume_pct / 100.0)))
        if vol_fill > 6:
            draw.rounded_rectangle((vol_bar_x, bar_y, vol_bar_x + vol_fill, bar_y + bar_h), radius=5, fill=(124, 92, 255, 255))

        vol_text_x = vol_bar_x + vol_bar_w + 12
        draw.text((vol_text_x, bar_y - 6), f"{volume_pct}%", font=font_meta, fill=(225, 228, 238, 255))

        pill_mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(pill_mask).rounded_rectangle((0, 0, W, H), radius=radius, fill=255)

        output = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        output.paste(canvas, (0, 0), pill_mask)

        buf = io.BytesIO()
        output.save(buf, format="PNG")
        buf.seek(0)
        return buf

    loop = asyncio.get_running_loop()
    image_buffer = await loop.run_in_executor(None, _render)
    return discord.File(image_buffer, filename="now_playing.png")


# ==============================================================================
# MULTI-PLATFORM TRACK RESOLVER (SPOTIFY, APPLE MUSIC, AMAZON MUSIC, YOUTUBE)
# ==============================================================================
async def resolve_external_tracks(query: str) -> tuple[str, list[dict]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    # 1. SPOTIFY
    if "open.spotify.com" in query or "spotify.link" in query:
        tracks = []
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(query, allow_redirects=True) as resp:
                    final_url = str(resp.url)

                match = re.search(r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|playlist|album)/([a-zA-Z0-9]+)", final_url)
                if match:
                    kind, item_id = match.group(1), match.group(2)
                    embed_url = f"https://open.spotify.com/embed/{kind}/{item_id}"
                    async with session.get(embed_url) as emb_resp:
                        html = await emb_resp.text()

                    next_data_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
                    if next_data_match:
                        data = json.loads(next_data_match.group(1))
                        entity = (
                            data.get("props", {})
                            .get("pageProps", {})
                            .get("state", {})
                            .get("data", {})
                            .get("entity", {})
                        )
                        if kind == "track":
                            title = entity.get("name") or entity.get("title")
                            artists = ", ".join(a.get("name", "") for a in entity.get("artists", []))
                            if title:
                                tracks.append({
                                    "title": title,
                                    "artist": artists or "Spotify",
                                    "query": f"ytsearch1:{title} {artists} audio",
                                    "url": final_url,
                                })
                        else:
                            for t in entity.get("trackList", []):
                                t_title = t.get("title")
                                t_artist = t.get("subtitle", "")
                                if t_title:
                                    tracks.append({
                                        "title": t_title,
                                        "artist": t_artist or "Spotify",
                                        "query": f"ytsearch1:{t_title} {t_artist} audio",
                                        "url": final_url,
                                    })
        except Exception:
            pass

        if tracks:
            return ("Spotify", tracks)

    # 2. APPLE MUSIC
    if "music.apple.com" in query:
        tracks = []
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(query, allow_redirects=True) as resp:
                    html = await resp.text()

            ld_matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
            for raw_json in ld_matches:
                try:
                    data = json.loads(raw_json.strip())
                    if isinstance(data, dict):
                        if "track" in data and isinstance(data["track"], list):
                            for item in data["track"]:
                                name = item.get("name")
                                by_artist = ""
                                if isinstance(item.get("byArtist"), list) and item["byArtist"]:
                                    by_artist = item["byArtist"][0].get("name", "")
                                elif isinstance(item.get("byArtist"), dict):
                                    by_artist = item["byArtist"].get("name", "")
                                if name:
                                    tracks.append({
                                        "title": name,
                                        "artist": by_artist or "Apple Music",
                                        "query": f"ytsearch1:{name} {by_artist} audio",
                                        "url": query,
                                    })
                        elif data.get("@type") == "MusicRecording" and data.get("name"):
                            name = data["name"]
                            by_artist = ""
                            if isinstance(data.get("byArtist"), list) and data["byArtist"]:
                                by_artist = data["byArtist"][0].get("name", "")
                            tracks.append({
                                "title": name,
                                "artist": by_artist or "Apple Music",
                                "query": f"ytsearch1:{name} {by_artist} audio",
                                "url": query,
                            })
                except Exception:
                    continue

            if not tracks:
                og_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                if og_title:
                    clean = og_title.group(1).replace(" on Apple Music", "").strip()
                    tracks.append({
                        "title": clean,
                        "artist": "Apple Music",
                        "query": f"ytsearch1:{clean} audio",
                        "url": query,
                    })
        except Exception:
            pass

        if tracks:
            return ("Apple Music", tracks)

    # 3. AMAZON MUSIC
    if "music.amazon." in query:
        tracks = []
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(query, allow_redirects=True) as resp:
                    html = await resp.text()

            og_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
            title_tag = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
            raw_title = (og_title.group(1) if og_title else (title_tag.group(1) if title_tag else "")).strip()
            clean = re.sub(r"\s+on Amazon Music.*$", "", raw_title, flags=re.IGNORECASE).strip()

            song_matches = re.findall(r'"title"\s*:\s*"([^"]{2,80})"\s*,\s*"artistName"\s*:\s*"([^"]{2,60})"', html)
            if song_matches:
                seen = set()
                for s_name, a_name in song_matches:
                    key = f"{s_name}-{a_name}".lower()
                    if key not in seen:
                        seen.add(key)
                        tracks.append({
                            "title": s_name,
                            "artist": a_name,
                            "query": f"ytsearch1:{s_name} {a_name} audio",
                            "url": query,
                        })
            elif clean and "amazon music" not in clean.lower():
                tracks.append({
                    "title": clean,
                    "artist": "Amazon Music",
                    "query": f"ytsearch1:{clean} audio",
                    "url": query,
                })
        except Exception:
            pass

        if tracks:
            return ("Amazon Music", tracks)

    # 4. YOUTUBE / DIRECT SEARCH
    loop = asyncio.get_running_loop()
    search_target = query if query.startswith("http") else f"ytsearch1:{query}"

    def _extract():
        return ytdl.extract_info(search_target, download=False)

    info = await loop.run_in_executor(None, _extract)
    if not info:
        return ("Search", [])

    tracks = []
    if "entries" in info:
        for entry in info["entries"]:
            if not entry:
                continue
            entry_url = entry.get("url") or entry.get("webpage_url")
            if entry_url and not entry_url.startswith("http"):
                entry_url = f"https://www.youtube.com/watch?v={entry_url}"
            tracks.append({
                "title": entry.get("title", "Unknown Track"),
                "artist": entry.get("uploader") or entry.get("channel") or "YouTube",
                "query": entry_url or f"ytsearch1:{entry.get('title', query)}",
                "url": entry_url or query,
                "duration": entry.get("duration"),
            })
    else:
        tracks.append({
            "title": info.get("title", query),
            "artist": info.get("uploader") or info.get("channel") or "YouTube",
            "query": info.get("webpage_url", query),
            "url": info.get("webpage_url", query),
            "duration": info.get("duration"),
        })

    platform = "YouTube / Web" if query.startswith("http") else "Search"
    return (platform, tracks)


# ==============================================================================
# GUILD MUSIC PLAYER & QUEUE MANAGER
# ==============================================================================
class GuildMusicPlayer:
    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: deque[dict] = deque()
        self.history: deque[str] = deque(maxlen=25)
        self.current: Optional[dict] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.now_playing_msg: Optional[discord.Message] = None
        self.lock = asyncio.Lock()

        cfg = get_music_config(guild.id)
        self.volume: float = max(1, min(200, cfg["volume"])) / 100.0
        self.autoplay: bool = bool(cfg["autoplay"])
        self.mode_247: bool = bool(cfg["mode_247"])
        self.loop_mode: str = "off"
        self.filter_name: str = "off"

        self.track_started_at: float = 0.0
        self.paused_started_at: Optional[float] = None
        self.total_paused_duration: float = 0.0
        self._suppress_after: bool = False

    def get_elapsed_seconds(self) -> int:
        if not self.current or self.track_started_at <= 0:
            return 0
        now = self.paused_started_at if self.paused_started_at else time.monotonic()
        elapsed = int(now - self.track_started_at - self.total_paused_duration)
        dur = self.current.get("duration") or 0
        if dur > 0:
            return min(max(0, elapsed), dur)
        return max(0, elapsed)

    async def get_playable_stream(self, track: dict) -> Optional[dict]:
        loop = asyncio.get_running_loop()
        target = track["query"]

        def _fetch():
            single_opts = dict(YTDL_OPTIONS)
            single_opts["noplaylist"] = True
            single_opts["extract_flat"] = False
            with yt_dlp.YoutubeDL(single_opts) as ydl_single:
                return ydl_single.extract_info(target, download=False)

        info = await loop.run_in_executor(None, _fetch)
        if not info:
            return None
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                return None
            info = entries[0]

        stream_url = info.get("url")
        if not stream_url:
            return None

        raw_title = track.get("title") or info.get("title", "Unknown Track")
        artist = track.get("artist") or info.get("artist") or info.get("uploader") or "Unknown Artist"

        if " - " in raw_title and artist in ("YouTube", "Search", "Unknown Artist"):
            parts = raw_title.split(" - ", 1)
            artist = parts[0].strip()
            raw_title = parts[1].strip()

        return {
            "stream_url": stream_url,
            "title": raw_title,
            "artist": artist,
            "query": track["query"],
            "webpage_url": info.get("webpage_url") or track.get("url", ""),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration", 0),
            "requester": track.get("requester", "Autoplay"),
            "platform": track.get("platform", "Music"),
        }

    async def fetch_autoplay_track(self) -> Optional[dict]:
        seed = (
            f"{self.current['title']} {self.current['artist']}"
            if self.current
            else (self.history[-1] if self.history else "top hits")
        )
        loop = asyncio.get_running_loop()

        def _search_related():
            opts = dict(YTDL_OPTIONS)
            opts["noplaylist"] = True
            with yt_dlp.YoutubeDL(opts) as ydl_rel:
                return ydl_rel.extract_info(f"ytsearch6:{seed} mix", download=False)

        info = await loop.run_in_executor(None, _search_related)
        if not info or "entries" not in info:
            return None

        for entry in info["entries"]:
            if not entry:
                continue
            title = entry.get("title", "")
            if title and title.lower() not in [h.lower() for h in self.history]:
                entry_url = entry.get("url") or entry.get("webpage_url")
                if entry_url and not entry_url.startswith("http"):
                    entry_url = f"https://www.youtube.com/watch?v={entry_url}"
                return {
                    "title": title,
                    "artist": entry.get("uploader") or "Autoplay",
                    "query": entry_url or f"ytsearch1:{title}",
                    "url": entry_url or "",
                    "requester": "Autoplay",
                    "platform": "Autoplay",
                }
        return None

    def _build_audio_source(self, stream_url: str, seek_seconds: int = 0) -> discord.PCMVolumeTransformer:
        before_opts = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
        if seek_seconds > 0:
            before_opts += f" -ss {seek_seconds}"

        af = AUDIO_FILTERS.get(self.filter_name, {}).get("af", "")
        opts = f'-vn -af "{af}"' if af else "-vn"

        ffmpeg_exe = _get_ffmpeg_path() or "ffmpeg"
        raw_source = discord.FFmpegPCMAudio(
            stream_url,
            executable=ffmpeg_exe,
            before_options=before_opts,
            options=opts,
        )
        return discord.PCMVolumeTransformer(raw_source, volume=self.volume)

    async def apply_filter_live(self):
        vc = self.guild.voice_client
        if not vc or not self.current:
            return
        elapsed = self.get_elapsed_seconds()
        self._suppress_after = True
        vc.stop()

        transformed = self._build_audio_source(self.current["stream_url"], seek_seconds=elapsed)
        self.track_started_at = time.monotonic() - elapsed
        self.paused_started_at = None
        self.total_paused_duration = 0.0

        def _after_playback(error):
            if self._suppress_after:
                self._suppress_after = False
                return
            if error:
                print(f"Player error in {self.guild.name}: {error}")
            asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

        vc.play(transformed, after=_after_playback)

    async def build_player_card_file(self) -> Optional[discord.File]:
        if not self.current:
            return None
        return await generate_music_card(
            title=self.current["title"],
            artist=self.current["artist"],
            thumbnail_url=self.current.get("thumbnail"),
            elapsed_sec=self.get_elapsed_seconds(),
            duration_sec=self.current.get("duration") or 0,
            volume_pct=int(round(self.volume * 100)),
            active_filter=self.filter_name,
        )

    async def refresh_interface(self, interaction: Optional[discord.Interaction] = None):
        if not self.current:
            return
        card_file = await self.build_player_card_file()
        view = MusicControlView(self)
        if interaction:
            if not interaction.response.is_done():
                await interaction.response.edit_message(attachments=[card_file], view=view)
            elif self.now_playing_msg:
                await self.now_playing_msg.edit(attachments=[card_file], view=view)
        elif self.now_playing_msg:
            try:
                await self.now_playing_msg.edit(attachments=[card_file], view=view)
            except discord.HTTPException:
                pass

    async def play_next(self):
        async with self.lock:
            vc: Optional[discord.VoiceClient] = self.guild.voice_client
            if not vc or not vc.is_connected():
                self.current = None
                return

            if not _get_ffmpeg_path():
                if self.text_channel:
                    err_embed = discord.Embed(
                        title="❌ FFmpeg Not Found on Server",
                        description=(
                            "`ffmpeg` is not installed in this Railway container.\n"
                            "Add `imageio-ffmpeg>=0.5.1` to `requirements.txt` and redeploy."
                        ),
                        color=discord.Color.red(),
                    )
                    await self.text_channel.send(embed=err_embed)
                return

            next_track = None
            if self.loop_mode == "track" and self.current:
                next_track = dict(self.current)
            else:
                if self.loop_mode == "queue" and self.current:
                    self.queue.append(dict(self.current))
                if self.queue:
                    next_track = self.queue.popleft()
                elif self.autoplay:
                    next_track = await self.fetch_autoplay_track()

            if not next_track:
                self.current = None
                if not self.mode_247 and self.text_channel:
                    end_embed = discord.Embed(
                        title="⏹️ Queue Finished",
                        description="All songs in the queue have finished playing. Use `z!247` to stay in voice 24/7.",
                        color=discord.Color.dark_grey(),
                    )
                    await self.text_channel.send(embed=end_embed)
                return

            stream_data = await self.get_playable_stream(next_track)
            if not stream_data:
                if self.text_channel:
                    skip_embed = discord.Embed(
                        title="⚠️ Track Skipped",
                        description=f"Unable to extract audio stream for **{next_track['title']}**.",
                        color=discord.Color.orange(),
                    )
                    await self.text_channel.send(embed=skip_embed)
                asyncio.create_task(self.play_next())
                return

            self.current = stream_data
            self.history.append(stream_data["title"])
            self.track_started_at = time.monotonic()
            self.paused_started_at = None
            self.total_paused_duration = 0.0
            self._suppress_after = False

            transformed = self._build_audio_source(stream_data["stream_url"])

            def _after_playback(error):
                if self._suppress_after:
                    self._suppress_after = False
                    return
                if error:
                    print(f"Player error in {self.guild.name}: {error}")
                asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

            vc.play(transformed, after=_after_playback)

            if self.text_channel:
                card_file = await self.build_player_card_file()
                self.now_playing_msg = await self.text_channel.send(
                    file=card_file,
                    view=MusicControlView(self),
                )

    def set_volume(self, percent: int) -> int:
        percent = max(1, min(200, percent))
        self.volume = percent / 100.0
        vc = self.guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = self.volume
        update_music_config(self.guild.id, volume=percent)
        return percent


# ==============================================================================
# INTERACTIVE MUSIC PLAYER CONTROLS
# ==============================================================================
class AudioFilterSelect(discord.ui.Select):
    def __init__(self, player: GuildMusicPlayer):
        self.player = player
        options = [
            discord.SelectOption(
                label=data["label"],
                value=key,
                default=(player.filter_name == key),
            )
            for key, data in AUDIO_FILTERS.items()
        ]
        super().__init__(placeholder="Select Filters", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.player.filter_name = self.values[0]
        await interaction.response.defer()
        await self.player.apply_filter_live()
        await self.player.refresh_interface()


class MusicControlView(discord.ui.View):
    def __init__(self, player: GuildMusicPlayer):
        super().__init__(timeout=None)
        self.player = player
        self.add_item(AudioFilterSelect(player))

        vc = player.guild.voice_client
        is_paused = bool(vc and vc.is_paused())
        self.pause_resume_btn.label = "Resume" if is_paused else "Pause"
        self.pause_resume_btn.style = discord.ButtonStyle.success if is_paused else discord.ButtonStyle.secondary

        self.loop_btn.label = f"Loop: {player.loop_mode.upper()}" if player.loop_mode != "off" else "Loop"
        self.loop_btn.style = discord.ButtonStyle.success if player.loop_mode != "off" else discord.ButtonStyle.secondary

        self.autoplay_btn.style = discord.ButtonStyle.success if player.autoplay else discord.ButtonStyle.secondary

    async def _check_vc(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == AUTHORIZED_OWNER_ID:
            return True
        if not interaction.user.voice or not interaction.guild.voice_client:
            embed = discord.Embed(
                title="🔊 Join Voice Channel",
                description="Join the bot's voice channel first to use player controls.",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        if interaction.user.voice.channel.id != interaction.guild.voice_client.channel.id:
            embed = discord.Embed(
                title="🔊 Same Voice Channel Required",
                description=f"You must be in {interaction.guild.voice_client.channel.mention} to control playback.",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, row=1)
    async def pause_resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_vc(interaction):
            return
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("Not connected to voice.", ephemeral=True)
            return
        if vc.is_paused():
            vc.resume()
            if self.player.paused_started_at:
                self.player.total_paused_duration += time.monotonic() - self.player.paused_started_at
                self.player.paused_started_at = None
        elif vc.is_playing():
            vc.pause()
            self.player.paused_started_at = time.monotonic()
        else:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return

        await self.player.refresh_interface(interaction)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, row=1)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_vc(interaction):
            return
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            if self.player.loop_mode == "track":
                self.player.loop_mode = "off"
            vc.stop()
            await interaction.response.send_message("⏭️ Skipped track.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, row=1)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_vc(interaction):
            return
        self.player.queue.clear()
        self.player.autoplay = False
        self.player.loop_mode = "off"
        self.player.current = None
        vc = interaction.guild.voice_client
        if vc:
            self.player._suppress_after = True
            vc.stop()
            if not self.player.mode_247:
                await vc.disconnect()
        stop_embed = discord.Embed(
            title="⏹️ Playback Stopped",
            description="Music stopped and the queue has been cleared.",
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(content=None, embed=stop_embed, attachments=[], view=None)

    @discord.ui.button(label="Vol -", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_vc(interaction):
            return
        self.player.set_volume(int(round(self.player.volume * 100)) - 10)
        await self.player.refresh_interface(interaction)

    @discord.ui.button(label="Vol +", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_vc(interaction):
            return
        self.player.set_volume(int(round(self.player.volume * 100)) + 10)
        await self.player.refresh_interface(interaction)

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary, row=2)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_vc(interaction):
            return
        cycle = {"off": "track", "track": "queue", "queue": "off"}
        self.player.loop_mode = cycle[self.player.loop_mode]
        await self.player.refresh_interface(interaction)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary, row=2)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_vc(interaction):
            return
        if len(self.player.queue) < 2:
            await interaction.response.send_message("Need at least 2 tracks in queue to shuffle.", ephemeral=True)
            return
        items = list(self.player.queue)
        random.shuffle(items)
        self.player.queue = deque(items)
        await interaction.response.send_message(f"🔀 Shuffled **{len(items)}** tracks in the queue.", ephemeral=True)

    @discord.ui.button(label="Autoplay", style=discord.ButtonStyle.secondary, row=2)
    async def autoplay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_vc(interaction):
            return
        self.player.autoplay = not self.player.autoplay
        update_music_config(interaction.guild_id, autoplay=1 if self.player.autoplay else 0)
        await self.player.refresh_interface(interaction)


# ==============================================================================
# MUSIC COG (WORKS AS EXTENSION IN bot.py OR STANDALONE BOT)
# ==============================================================================
class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildMusicPlayer] = {}
        self._reconnected_247 = False
        init_music_db()

    def get_player(self, guild: discord.Guild) -> GuildMusicPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildMusicPlayer(self.bot, guild)
        return self.players[guild.id]

    async def _ensure_voice(self, ctx: commands.Context) -> Optional[discord.VoiceClient]:
        if not ctx.author.voice or not ctx.author.voice.channel:
            embed = discord.Embed(
                title="🔊 Join a Voice Channel First",
                description=(
                    f"{ctx.author.mention}, you must join a **Voice Channel** before using the `play` command!\n\n"
                    "👉 Connect to any Voice Channel and try **`z!play <song name>`** or **`@Zeno play <song name>`**."
                ),
                color=discord.Color.red(),
            )
            embed.set_footer(
                text=f"Requested by {ctx.author.display_name}",
                icon_url=ctx.author.display_avatar.url,
            )
            await ctx.reply(embed=embed, mention_author=False)
            return None

        target_vc = ctx.author.voice.channel
        perms = target_vc.permissions_for(ctx.guild.me)
        if not perms.connect or not perms.speak:
            perm_embed = discord.Embed(
                title="⛔ Missing Voice Permissions",
                description=(
                    f"I do not have permission to **Connect** or **Speak** in {target_vc.mention}.\n"
                    "Please check my channel permissions and try again."
                ),
                color=discord.Color.red(),
            )
            await ctx.reply(embed=perm_embed, mention_author=False)
            return None

        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()

        vc = ctx.guild.voice_client
        try:
            if not vc:
                vc = await target_vc.connect(timeout=15.0, reconnect=True, self_deaf=True)
            elif vc.channel.id != target_vc.id:
                await vc.move_to(target_vc)
        except asyncio.TimeoutError:
            err_embed = discord.Embed(
                title="❌ Voice Connection Timed Out",
                description=(
                    "Could not complete the Discord voice handshake.\n"
                    "Ensure `intents.voice_states = True` is enabled in `bot.py` and redeploy."
                ),
                color=discord.Color.red(),
            )
            await ctx.reply(embed=err_embed, mention_author=False)
            return None
        except (discord.ClientException, RuntimeError) as e:
            err_embed = discord.Embed(
                title="❌ Voice Dependency Error",
                description=f"`{e}`\nMake sure `discord.py[voice]>=2.5.0`, `davey>=0.1.0`, and `PyNaCl>=1.5.0` are in `requirements.txt`.",
                color=discord.Color.red(),
            )
            await ctx.reply(embed=err_embed, mention_author=False)
            return None
        except Exception as e:
            err_embed = discord.Embed(
                title="❌ Failed to Join Voice Channel",
                description=f"Error: `{type(e).__name__}: {e}`",
                color=discord.Color.red(),
            )
            await ctx.reply(embed=err_embed, mention_author=False)
            return None

        return vc

    @commands.Cog.listener()
    async def on_ready(self):
        if self._reconnected_247:
            return
        self._reconnected_247 = True
        for row in get_all_247_guilds():
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                continue
            vc_channel = guild.get_channel(row["voice_channel_id"])
            if isinstance(vc_channel, discord.VoiceChannel):
                try:
                    if not guild.voice_client:
                        await vc_channel.connect(self_deaf=True)
                    player = self.get_player(guild)
                    player.mode_247 = True
                    if row["text_channel_id"]:
                        player.text_channel = guild.get_channel(row["text_channel_id"])
                except Exception as e:
                    print(f"Failed 24/7 reconnect in {guild.name}: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot or not self.bot.user:
            return

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        content = message.content.strip()
        bot_id = self.bot.user.id
        mention_prefixes = (f"<@{bot_id}>", f"<@!{bot_id}>")
        stripped = None

        if content.startswith(mention_prefixes):
            for mp in mention_prefixes:
                if content.startswith(mp):
                    stripped = content[len(mp):].strip()
                    break
            if not stripped:
                help_embed = discord.Embed(
                    title="🎶 Zeno Music System",
                    description=(
                        f"Join a Voice Channel and use **`z!play <song name>`** or "
                        f"**`{self.bot.user.mention} play <song name>`** to start playing!"
                    ),
                    color=discord.Color.blurple(),
                )
                await message.reply(embed=help_embed, mention_author=False)
                return
        elif content.lower().startswith("z!"):
            stripped = content[2:].strip()

        if not stripped:
            return

        cmd_word = stripped.split()[0].lower()
        if cmd_word not in MUSIC_COMMAND_NAMES:
            return

        prefixes = await self.bot.get_prefix(message)
        if isinstance(prefixes, str):
            active_prefix = prefixes
        else:
            non_empty = [p for p in prefixes if p]
            active_prefix = non_empty[0] if non_empty else "!"

        original_content = message.content
        try:
            message.content = f"{active_prefix}{stripped}"
            new_ctx = await self.bot.get_context(message)
            if new_ctx.valid:
                await self.bot.invoke(new_ctx)
        finally:
            message.content = original_content

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        guild = member.guild
        vc = guild.voice_client
        player = self.get_player(guild)

        if member.id == self.bot.user.id:
            if before.channel and not after.channel and player.mode_247:
                await asyncio.sleep(2)
                try:
                    await before.channel.connect(self_deaf=True)
                except Exception:
                    pass
            return

        if not member.bot:
            cfg = get_music_config(guild.id)
            vc_role_id = cfg.get("vc_role_id")
            if vc_role_id and guild.me.guild_permissions.manage_roles:
                vc_role = guild.get_role(vc_role_id)
                if vc_role and vc_role < guild.me.top_role:
                    if before.channel is None and after.channel is not None:
                        if vc_role not in member.roles:
                            try:
                                await member.add_roles(vc_role, reason="Joined voice channel (VC Auto-Role)")
                            except (discord.Forbidden, discord.HTTPException):
                                pass
                    elif before.channel is not None and after.channel is None:
                        if vc_role in member.roles:
                            try:
                                await member.remove_roles(vc_role, reason="Left voice channel (VC Auto-Role)")
                            except (discord.Forbidden, discord.HTTPException):
                                pass
                    elif before.channel is not None and after.channel is not None:
                        if vc_role not in member.roles:
                            try:
                                await member.add_roles(vc_role, reason="Active in voice channel (VC Auto-Role)")
                            except (discord.Forbidden, discord.HTTPException):
                                pass

        if vc and vc.channel and not player.mode_247:
            human_members = [m for m in vc.channel.members if not m.bot]
            if len(human_members) == 0:
                await asyncio.sleep(15)
                if vc.is_connected() and len([m for m in vc.channel.members if not m.bot]) == 0 and not player.mode_247:
                    player.queue.clear()
                    await vc.disconnect()

    @commands.hybrid_command(
        name="vcrole",
        aliases=["voicerole", "autorolevc"],
        description="Set or disable the role automatically given when joining any voice channel.",
    )
    @app_commands.describe(role="Role to auto-assign in voice channels (leave empty to disable)")
    async def vcrole_cmd(self, ctx: commands.Context, role: Optional[discord.Role] = None):
        if not _can_manage_vc_role(ctx.author):
            err_embed = discord.Embed(
                title="⛔ Permission Denied",
                description="You need **Manage Roles** or a **Staff** role to configure the VC auto-role.",
                color=discord.Color.red(),
            )
            await ctx.reply(embed=err_embed, mention_author=False)
            return

        if role:
            if role.is_default():
                await ctx.reply(
                    embed=discord.Embed(description="❌ You cannot set `@everyone` as the VC auto-role.", color=discord.Color.red()),
                    mention_author=False,
                )
                return
            if role >= ctx.guild.me.top_role:
                await ctx.reply(
                    embed=discord.Embed(
                        description="❌ I cannot assign this role because it is higher than or equal to my top role.",
                        color=discord.Color.red(),
                    ),
                    mention_author=False,
                )
                return

            update_music_config(ctx.guild.id, vc_role_id=role.id)

            synced = 0
            if ctx.guild.me.guild_permissions.manage_roles:
                for channel in ctx.guild.voice_channels:
                    for member in channel.members:
                        if not member.bot and role not in member.roles:
                            try:
                                await member.add_roles(role, reason="Synced active voice channel member")
                                synced += 1
                            except (discord.Forbidden, discord.HTTPException):
                                pass

            extra = f"\n👥 Synced **{synced}** member(s) currently in VC." if synced > 0 else ""
            ok_embed = discord.Embed(
                title="✅ VC Auto-Role Enabled",
                description=f"Members will receive {role.mention} when joining any voice channel, and it will be removed when they leave.{extra}",
                color=discord.Color.green(),
            )
            await ctx.reply(embed=ok_embed, mention_author=False)
        else:
            update_music_config(ctx.guild.id, vc_role_id=None)
            off_embed = discord.Embed(
                title="✅ VC Auto-Role Disabled",
                description="Roles will no longer be added or removed when members join or leave voice channels.",
                color=discord.Color.orange(),
            )
            await ctx.reply(embed=off_embed, mention_author=False)

    @commands.hybrid_command(
        name="play",
        aliases=["p"],
        description="Play a song or playlist from Spotify, Apple Music, Amazon Music, or YouTube.",
    )
    @app_commands.describe(query="Song name or Spotify / Apple Music / Amazon Music / YouTube link")
    async def play_cmd(self, ctx: commands.Context, *, query: Optional[str] = None):
        vc = await self._ensure_voice(ctx)
        if not vc:
            return

        if not query or not query.strip():
            prompt_embed = discord.Embed(
                title="🎵 Enter a Song Name or Link",
                description=(
                    f"Joined {vc.channel.mention}!\n"
                    "Please provide a song name or link: **`z!play <song name>`**"
                ),
                color=discord.Color.blurple(),
            )
            await ctx.reply(embed=prompt_embed, mention_author=False)
            return

        player = self.get_player(ctx.guild)
        player.text_channel = ctx.channel

        searching_embed = discord.Embed(
            title="🎧 Joined Voice & Searching...",
            description=(
                f"🔊 **Voice Channel:** {vc.channel.mention}\n"
                f"🔎 **Searching:** `{query}`"
            ),
            color=discord.Color.blurple(),
        )
        searching_embed.set_footer(
            text=f"Requested by {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url,
        )
        status_msg = await ctx.reply(embed=searching_embed, mention_author=False)

        platform, tracks = await resolve_external_tracks(query)
        if not tracks:
            not_found_embed = discord.Embed(
                title="❌ Song Not Found",
                description=f"Could not find any playable tracks for: `{query}`",
                color=discord.Color.red(),
            )
            await status_msg.edit(embed=not_found_embed)
            return

        for t in tracks:
            t["requester"] = ctx.author.mention
            t["platform"] = platform
            player.queue.append(t)

        if len(tracks) == 1:
            queued_embed = discord.Embed(
                title="✅ Joined VC & Added to Queue",
                description=(
                    f"🔊 **Voice Channel:** {vc.channel.mention}\n"
                    f"🎶 **Track:** **{tracks[0]['title']}**\n"
                    f"📡 **Source:** `{platform}`"
                ),
                color=discord.Color.green(),
            )
        else:
            queued_embed = discord.Embed(
                title="✅ Joined VC & Playlist Loaded",
                description=(
                    f"🔊 **Voice Channel:** {vc.channel.mention}\n"
                    f"📀 **Added:** **{len(tracks)} tracks** from **{platform}**\n"
                    f"▶️ **First Up:** **{tracks[0]['title']}**"
                ),
                color=discord.Color.green(),
            )
        queued_embed.set_footer(
            text=f"Requested by {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url,
        )
        await status_msg.edit(embed=queued_embed)

        if not vc.is_playing() and not vc.is_paused():
            await player.play_next()

    @commands.hybrid_group(
        name="search",
        invoke_without_command=True,
        description="Search and play playlists or tracks from Spotify, Apple Music, Amazon Music, or YouTube.",
    )
    async def search_group(self, ctx: commands.Context, *, query: Optional[str] = None):
        await self.play_cmd(ctx, query=query)

    @search_group.command(
        name="play",
        description="Load and play a full playlist from Spotify, Apple Music, Amazon Music, or YouTube.",
    )
    @app_commands.describe(playlist_url="Playlist link from Spotify, Apple Music, Amazon Music, or YouTube")
    async def search_play_cmd(self, ctx: commands.Context, *, playlist_url: Optional[str] = None):
        vc = await self._ensure_voice(ctx)
        if not vc:
            return

        if not playlist_url or not playlist_url.strip():
            prompt_embed = discord.Embed(
                title="📀 Enter a Playlist Link",
                description="Please provide a public playlist URL: **`z!search play <playlist link>`**",
                color=discord.Color.blurple(),
            )
            await ctx.reply(embed=prompt_embed, mention_author=False)
            return

        player = self.get_player(ctx.guild)
        player.text_channel = ctx.channel

        loading_embed = discord.Embed(
            title="📀 Joining Voice & Loading Playlist...",
            description=(
                f"🔊 **Voice Channel:** {vc.channel.mention}\n"
                f"🔎 **Fetching Playlist:** `{playlist_url}`"
            ),
            color=discord.Color.blurple(),
        )
        status_msg = await ctx.reply(embed=loading_embed, mention_author=False)

        platform, tracks = await resolve_external_tracks(playlist_url)
        if not tracks:
            err_embed = discord.Embed(
                title="❌ Playlist Not Found",
                description=f"No tracks found at `{playlist_url}`. Make sure the playlist is public!",
                color=discord.Color.red(),
            )
            await status_msg.edit(embed=err_embed)
            return

        for t in tracks:
            t["requester"] = ctx.author.mention
            t["platform"] = platform
            player.queue.append(t)

        done_embed = discord.Embed(
            title=f"✅ Playlist Loaded ({platform})",
            description=(
                f"🔊 **Voice Channel:** {vc.channel.mention}\n"
                f"🎶 **Queued:** **{len(tracks)}** track(s)\n"
                f"▶️ **First Up:** **{tracks[0]['title']}**"
            ),
            color=discord.Color.green(),
        )
        done_embed.set_footer(
            text=f"Requested by {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url,
        )
        await status_msg.edit(embed=done_embed)

        if not vc.is_playing() and not vc.is_paused():
            await player.play_next()

    @commands.hybrid_command(
        name="autoplay",
        aliases=["ap"],
        description="Toggle Autoplay mode so the bot automatically queues and plays related songs.",
    )
    async def autoplay_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        player.text_channel = ctx.channel
        player.autoplay = not player.autoplay
        update_music_config(ctx.guild.id, autoplay=1 if player.autoplay else 0)
        await player.refresh_interface()

        status = "ENABLED 🔁 (Bot will keep playing related songs automatically)" if player.autoplay else "DISABLED"
        embed = discord.Embed(
            title="🎶 Autoplay Mode",
            description=f"Autoplay is now **{status}**.",
            color=discord.Color.green() if player.autoplay else discord.Color.orange(),
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(
        name="247",
        aliases=["24x7", "stay"],
        description="Toggle 24×7 mode so the bot stays in the voice channel permanently.",
    )
    async def mode_247_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        player.text_channel = ctx.channel
        player.mode_247 = not player.mode_247

        vc_id = None
        if player.mode_247:
            vc = ctx.guild.voice_client or await self._ensure_voice(ctx)
            if not vc:
                player.mode_247 = False
                return
            vc_id = vc.channel.id

        update_music_config(
            ctx.guild.id,
            mode_247=1 if player.mode_247 else 0,
            voice_channel_id=vc_id,
            text_channel_id=ctx.channel.id,
        )

        if player.mode_247:
            embed = discord.Embed(
                title="♾️ 24×7 Music Mode Enabled",
                description=f"Locked to <#{vc_id}> permanently.",
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="♾️ 24×7 Music Mode Disabled",
                description="Bot will now leave when the voice channel becomes empty.",
                color=discord.Color.orange(),
            )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="volume", aliases=["vol"], description="Set music playback volume (1% to 200%).")
    @app_commands.describe(level="Volume percentage between 1 and 200")
    async def volume_cmd(self, ctx: commands.Context, level: int):
        player = self.get_player(ctx.guild)
        new_vol = player.set_volume(level)
        await player.refresh_interface()
        await ctx.reply(
            embed=discord.Embed(description=f"🔊 Volume set to **{new_vol}%**.", color=discord.Color.blurple()),
            mention_author=False,
        )

    @commands.hybrid_command(name="volup", aliases=["volumeup"], description="Increase music playback volume by 10%.")
    async def volup_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        new_vol = player.set_volume(int(round(player.volume * 100)) + 10)
        await player.refresh_interface()
        await ctx.reply(
            embed=discord.Embed(description=f"🔊 Volume increased to **{new_vol}%**.", color=discord.Color.blurple()),
            mention_author=False,
        )

    @commands.hybrid_command(name="voldown", aliases=["volumedown"], description="Decrease music playback volume by 10%.")
    async def voldown_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        new_vol = player.set_volume(int(round(player.volume * 100)) - 10)
        await player.refresh_interface()
        await ctx.reply(
            embed=discord.Embed(description=f"🔉 Volume decreased to **{new_vol}%**.", color=discord.Color.blurple()),
            mention_author=False,
        )

    @commands.hybrid_command(name="loop", description="Cycle loop mode (Off / Track / Queue).")
    async def loop_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        cycle = {"off": "track", "track": "queue", "queue": "off"}
        player.loop_mode = cycle[player.loop_mode]
        await player.refresh_interface()
        await ctx.reply(
            embed=discord.Embed(description=f"🔁 Loop mode set to **{player.loop_mode.upper()}**.", color=discord.Color.blurple()),
            mention_author=False,
        )

    @commands.hybrid_command(name="shuffle", description="Shuffle the upcoming tracks in the queue.")
    async def shuffle_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        if len(player.queue) < 2:
            await ctx.reply(
                embed=discord.Embed(description="❌ Need at least 2 songs in the queue to shuffle.", color=discord.Color.red()),
                mention_author=False,
            )
            return
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        await ctx.reply(
            embed=discord.Embed(description=f"🔀 Shuffled **{len(items)}** tracks in the queue.", color=discord.Color.green()),
            mention_author=False,
        )

    @commands.hybrid_command(name="skip", aliases=["next"], description="Skip the currently playing song.")
    async def skip_cmd(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await ctx.reply(
                embed=discord.Embed(description="⏭️ Skipped to the next track.", color=discord.Color.blurple()),
                mention_author=False,
            )
        else:
            await ctx.reply(
                embed=discord.Embed(description="❌ Nothing is currently playing.", color=discord.Color.red()),
                mention_author=False,
            )

    @commands.hybrid_command(name="pause", description="Pause the current track.")
    async def pause_cmd(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        player = self.get_player(ctx.guild)
        if vc and vc.is_playing():
            vc.pause()
            player.paused_started_at = time.monotonic()
            await player.refresh_interface()
            await ctx.reply(
                embed=discord.Embed(description="⏸️ Music paused.", color=discord.Color.orange()),
                mention_author=False,
            )
        else:
            await ctx.reply(
                embed=discord.Embed(description="❌ Nothing is playing to pause.", color=discord.Color.red()),
                mention_author=False,
            )

    @commands.hybrid_command(name="resume", description="Resume paused music.")
    async def resume_cmd(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        player = self.get_player(ctx.guild)
        if vc and vc.is_paused():
            vc.resume()
            if player.paused_started_at:
                player.total_paused_duration += time.monotonic() - player.paused_started_at
                player.paused_started_at = None
            await player.refresh_interface()
            await ctx.reply(
                embed=discord.Embed(description="▶️ Music resumed.", color=discord.Color.green()),
                mention_author=False,
            )
        else:
            await ctx.reply(
                embed=discord.Embed(description="❌ Music is not paused.", color=discord.Color.red()),
                mention_author=False,
            )

    @commands.hybrid_command(name="queue", aliases=["q"], description="View the upcoming music queue.")
    async def queue_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        if not player.current and not player.queue:
            await ctx.reply(
                embed=discord.Embed(description="📭 The music queue is currently empty.", color=discord.Color.dark_grey()),
                mention_author=False,
            )
            return

        embed = discord.Embed(title="🎶 Server Music Queue", color=discord.Color.blurple())
        if player.current:
            embed.add_field(
                name="Now Playing",
                value=f"**{player.current['title']}** — *{player.current['artist']}* `[{player.current['platform']}]`",
                inline=False,
            )

        if player.queue:
            upcoming = "\n".join(
                f"`{idx}.` {t['title']} `[{t.get('platform', 'Music')}]`"
                for idx, t in enumerate(list(player.queue)[:10], start=1)
            )
            if len(player.queue) > 10:
                upcoming += f"\n*...and {len(player.queue) - 10} more track(s)*"
            embed.add_field(name="Up Next", value=upcoming, inline=False)

        embed.set_footer(
            text=f"Volume: {int(round(player.volume * 100))}% | Loop: {player.loop_mode.upper()} | Autoplay: {'ON' if player.autoplay else 'OFF'}"
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="nowplaying", aliases=["np"], description="Show the custom music player card and controls.")
    async def nowplaying_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        if not player.current:
            await ctx.reply(
                embed=discord.Embed(description="❌ No song is currently playing.", color=discord.Color.red()),
                mention_author=False,
            )
            return

        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()
        card_file = await player.build_player_card_file()
        player.now_playing_msg = await ctx.send(file=card_file, view=MusicControlView(player))

    @commands.hybrid_command(name="stop", aliases=["dc", "leave"], description="Stop playback, clear queue, and leave voice.")
    async def stop_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        player.queue.clear()
        player.autoplay = False
        player.loop_mode = "off"
        player.mode_247 = False
        player.current = None
        update_music_config(ctx.guild.id, autoplay=0, mode_247=0, voice_channel_id=None)

        vc = ctx.guild.voice_client
        if vc:
            player._suppress_after = True
            vc.stop()
            await vc.disconnect()
        await ctx.reply(
            embed=discord.Embed(
                title="⏹️ Disconnected",
                description="Stopped music, disabled 24/7 mode, and left the voice channel.",
                color=discord.Color.red(),
            ),
            mention_author=False,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))


# ==============================================================================
# STANDALONE RUNNER (IF RUNNING music.py BY ITSELF)
# ==============================================================================
def _standalone_prefix(bot_instance, message: discord.Message):
    prefixes = ["z! ", "z!", "Z! ", "Z!", "!", "t!"]
    if message.author and message.author.id == AUTHORIZED_OWNER_ID:
        prefixes.append("")
    return commands.when_mentioned_or(*prefixes)(bot_instance, message)


class StandaloneMusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix=_standalone_prefix, intents=intents)

    async def setup_hook(self):
        await self.load_extension("music")
        await self.tree.sync()
        print("Standalone Music Bot commands synchronized.")

    async def on_ready(self):
        print(f"Music Bot online as {self.user} (ID: {self.user.id})")


if __name__ == "__main__":
    StandaloneMusicBot().run(TOKEN)
