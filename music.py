import os
import re
import io
import json
import time
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
    "format": "bestaudio/best",
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
}

AUDIO_FILTERS = {
    "off": {"label": "Normal (No Filter)", "af": ""},
    "bassboost": {"label": "Bassboost", "af": "bass=g=12:f=110:w=0.6"},
    "nightcore": {"label": "Nightcore", "af": "asetrate=48000*1.22,aresample=48000,atempo=1.05"},
    "vaporwave": {"label": "Slowed + Reverb", "af": "asetrate=48000*0.84,aresample=48000,aecho=0.8:0.88:60:0.4"},
    "8d": {"label": "8D Surround Audio", "af": "apulsator=hz=0.125"},
    "karaoke": {"label": "Vocal Reducer", "af": "stereotools=mlev=0.015625"},
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


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
        # Safe migration for existing databases
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

        # Progress bar & timestamp
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

        # Speaker icon + purple volume bar + percentage
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
if not next_track:
                self.current = None
                if not self.mode_247 and self.text_channel:
                    await self.text_channel.send("⏹️ Queue finished. Enable `z!247` to stay in voice 24/7.")
                return

            stream_data = await self.get_playable_stream(next_track)
            if not stream_data:
                if self.text_channel:
                    await self.text_channel.send(f"⚠️ Skipped **{next_track['title']}** (unable to extract stream).")
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
            await interaction.response.send_message("Join the bot's voice channel to use controls.", ephemeral=True)
            return False
        if interaction.user.voice.channel.id != interaction.guild.voice_client.channel.id:
            await interaction.response.send_message("You must be in the same voice channel.", ephemeral=True)
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
        await interaction.response.edit_message(content="⏹️ Playback stopped and queue cleared.", attachments=[], view=None)

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
            await ctx.send("❌ You must be in a voice channel first!", ephemeral=True)
            return None

        target_vc = ctx.author.voice.channel
        vc = ctx.guild.voice_client
        if not vc:
            vc = await target_vc.connect(self_deaf=True)
        elif vc.channel.id != target_vc.id:
            await vc.move_to(target_vc)
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
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        guild = member.guild
        vc = guild.voice_client
        player = self.get_player(guild)

        # 1. Bot 24/7 auto-reconnect
        if member.id == self.bot.user.id:
            if before.channel and not after.channel and player.mode_247:
                await asyncio.sleep(2)
                try:
                    await before.channel.connect(self_deaf=True)
                except Exception:
                    pass
            return

        # 2. Automatic VC Role assignment on join / removal on leave
        if not member.bot:
            cfg = get_music_config(guild.id)
            vc_role_id = cfg.get("vc_role_id")
            if vc_role_id and guild.me.guild_permissions.manage_roles:
                vc_role = guild.get_role(vc_role_id)
                if vc_role and vc_role < guild.me.top_role:
                    # Member joined any voice channel
                    if before.channel is None and after.channel is not None:
                        if vc_role not in member.roles:
                            try:
                                await member.add_roles(vc_role, reason="Joined voice channel (VC Auto-Role)")
                            except (discord.Forbidden, discord.HTTPException):
                                pass
                    # Member disconnected from voice channels completely
                    elif before.channel is not None and after.channel is None:
                        if vc_role in member.roles:
                            try:
                                await member.remove_roles(vc_role, reason="Left voice channel (VC Auto-Role)")
                            except (discord.Forbidden, discord.HTTPException):
                                pass
                    # Member switched between two voice channels
                    elif before.channel is not None and after.channel is not None:
                        if vc_role not in member.roles:
                            try:
                                await member.add_roles(vc_role, reason="Active in voice channel (VC Auto-Role)")
                            except (discord.Forbidden, discord.HTTPException):
                                pass

        # 3. Auto-disconnect when alone (if 24/7 mode is off)
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
            await ctx.send("⛔ Permission denied. You need **Manage Roles** or a **Staff** role to use this command.", ephemeral=True)
            return

        if role:
            if role.is_default():
                await ctx.send("❌ You cannot set `@everyone` as the VC auto-role.", ephemeral=True)
                return
            if role >= ctx.guild.me.top_role:
                await ctx.send("❌ I cannot assign this role because it is higher than or equal to my top role.", ephemeral=True)
                return

            update_music_config(ctx.guild.id, vc_role_id=role.id)

            # Sync members currently inside any voice channel
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

            extra = f" (Synced **{synced}** member(s) currently in VC)" if synced > 0 else ""
            await ctx.send(
                f"✅ **VC Auto-Role Enabled!** Members will receive {role.mention} when joining any voice channel, and it will be removed when they leave.{extra}"
            )
        else:
            update_music_config(ctx.guild.id, vc_role_id=None)
            await ctx.send("✅ **VC Auto-Role Disabled.** Roles will no longer be added or removed on voice join/leave.")

    @commands.hybrid_command(
        name="play",
        aliases=["p"],
        description="Play a song or playlist from Spotify, Apple Music, Amazon Music, or YouTube.",
    )
    @app_commands.describe(query="Song name or Spotify / Apple Music / Amazon Music / YouTube link")
    async def play_cmd(self, ctx: commands.Context, *, query: str):
        vc = await self._ensure_voice(ctx)
        if not vc:
            return

        await ctx.defer()
        player = self.get_player(ctx.guild)
        player.text_channel = ctx.channel

        platform, tracks = await resolve_external_tracks(query)
        if not tracks:
            await ctx.send(f"❌ Could not find any playable tracks for: `{query}`")
            return

        for t in tracks:
            t["requester"] = ctx.author.mention
            t["platform"] = platform
            player.queue.append(t)

        if len(tracks) == 1:
            await ctx.send(f"✅ Queued **{tracks[0]['title']}** `[{platform}]`")
        else:
            await ctx.send(f"✅ Added **{len(tracks)} tracks** from **{platform}** playlist to the queue!")

        if not vc.is_playing() and not vc.is_paused():
            await player.play_next()

    @commands.hybrid_group(
        name="search",
        invoke_without_command=True,
        description="Search and play playlists or tracks from Spotify, Apple Music, Amazon Music, or YouTube.",
    )
    async def search_group(self, ctx: commands.Context, *, query: str):
        await self.play_cmd(ctx, query=query)

    @search_group.command(
        name="play",
        description="Load and play a full playlist from Spotify, Apple Music, Amazon Music, or YouTube.",
    )
    @app_commands.describe(playlist_url="Playlist link from Spotify, Apple Music, Amazon Music, or YouTube")
    async def search_play_cmd(self, ctx: commands.Context, *, playlist_url: str):
        vc = await self._ensure_voice(ctx)
        if not vc:
            return

        await ctx.defer()
        player = self.get_player(ctx.guild)
        player.text_channel = ctx.channel

        platform, tracks = await resolve_external_tracks(playlist_url)
        if not tracks:
            await ctx.send(f"❌ No tracks found at `{playlist_url}`. Make sure the playlist is public!")
            return

        for t in tracks:
            t["requester"] = ctx.author.mention
            t["platform"] = platform
            player.queue.append(t)

        await ctx.send(
            f"📀 **Playlist Loaded ({platform})!** Queued **{len(tracks)}** track(s). First up: **{tracks[0]['title']}**"
        )

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
        await ctx.send(f"🎶 **Autoplay Mode:** {status}")

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
            await ctx.send(f"♾️ **24×7 Music Mode ENABLED!** Locked to <#{vc_id}>.")
        else:
            await ctx.send("♾️ **24×7 Music Mode DISABLED.**")

    @commands.hybrid_command(name="volume", aliases=["vol"], description="Set music playback volume (1% to 200%).")
    @app_commands.describe(level="Volume percentage between 1 and 200")
    async def volume_cmd(self, ctx: commands.Context, level: int):
        player = self.get_player(ctx.guild)
        new_vol = player.set_volume(level)
        await player.refresh_interface()
        await ctx.send(f"🔊 Volume set to **{new_vol}%**.")

    @commands.hybrid_command(name="volup", aliases=["volumeup"], description="Increase music playback volume by 10%.")
    async def volup_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        new_vol = player.set_volume(int(round(player.volume * 100)) + 10)
        await player.refresh_interface()
        await ctx.send(f"🔊 Volume increased to **{new_vol}%**.")

    @commands.hybrid_command(name="voldown", aliases=["volumedown"], description="Decrease music playback volume by 10%.")
    async def voldown_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        new_vol = player.set_volume(int(round(player.volume * 100)) - 10)
        await player.refresh_interface()
        await ctx.send(f"🔉 Volume decreased to **{new_vol}%**.")

    @commands.hybrid_command(name="loop", description="Cycle loop mode (Off / Track / Queue).")
    async def loop_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        cycle = {"off": "track", "track": "queue", "queue": "off"}
        player.loop_mode = cycle[player.loop_mode]
        await player.refresh_interface()
        await ctx.send(f"🔁 Loop mode set to **{player.loop_mode.upper()}**.")

    @commands.hybrid_command(name="shuffle", description="Shuffle the upcoming tracks in the queue.")
    async def shuffle_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        if len(player.queue) < 2:
            await ctx.send("❌ Need at least 2 songs in the queue to shuffle.", ephemeral=True)
            return
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        await ctx.send(f"🔀 Shuffled **{len(items)}** tracks in the queue.")

    @commands.hybrid_command(name="skip", aliases=["next"], description="Skip the currently playing song.")
    async def skip_cmd(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await ctx.send("⏭️ Skipped to the next track.")
        else:
            await ctx.send("❌ Nothing is currently playing.", ephemeral=True)

    @commands.hybrid_command(name="pause", description="Pause the current track.")
    async def pause_cmd(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        player = self.get_player(ctx.guild)
        if vc and vc.is_playing():
            vc.pause()
            player.paused_started_at = time.monotonic()
            await player.refresh_interface()
            await ctx.send("⏸️ Music paused.")
        else:
            await ctx.send("❌ Nothing is playing to pause.", ephemeral=True)

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
            await ctx.send("▶️ Music resumed.")
        else:
            await ctx.send("❌ Music is not paused.", ephemeral=True)

    @commands.hybrid_command(name="queue", aliases=["q"], description="View the upcoming music queue.")
    async def queue_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        if not player.current and not player.queue:
            await ctx.send("📭 The music queue is currently empty.", ephemeral=True)
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
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="nowplaying", aliases=["np"], description="Show the custom music player card and controls.")
    async def nowplaying_cmd(self, ctx: commands.Context):
        player = self.get_player(ctx.guild)
        if not player.current:
            await ctx.send("❌ No song is currently playing.", ephemeral=True)
            return

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
        await ctx.send("⏹️ Stopped music, disabled 24/7 mode, and disconnected from voice.")


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
# ==============================================================================
# STANDALONE RUNNER (IF RUNNING music.py BY ITSELF)
# ==============================================================================
def _standalone_prefix(bot_instance, message: discord.Message):
    if message.author and message.author.id == AUTHORIZED_OWNER_ID:
        return ["z! ", "z!", "!", "t!", ""]
    return ["z! ", "z!", "!", "t!"]


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
