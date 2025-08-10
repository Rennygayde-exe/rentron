
import shlex
import yt_dlp
import discord
from collections import deque
from dataclasses import dataclass
from discord.ext import commands
from discord import app_commands
import asyncio, random

YDL_OPTS = {
    "format": (
        "bestaudio[protocol^=http][acodec!=none][ext=webm]/"
        "bestaudio[protocol^=http][acodec!=none][ext=m4a]/"
        "bestaudio[protocol^=http][acodec!=none]/"
        "bestaudio"
    ),
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "extractor_args": {
        "youtube": {
            "player_client": ["tv", "web"]
        }
    },
}

FF_COMMON = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -protocol_whitelist file,https,tcp,tls,crypto"


def _ff_headers(h: dict) -> str:
    h = {"Referer": "https://www.youtube.com", "User-Agent": h.get("User-Agent", "Mozilla/5.0"), **(h or {})}
    blob = "".join(f"{k}: {v}\r\n" for k, v in h.items())
    import shlex
    return f"-headers {shlex.quote(blob)}"

def _ff_args_for(song_url: str, headers: dict, start: int):
    before = f"{FF_COMMON} {_ff_headers(headers)}"
    if start and "m3u8" in song_url:
        return before, f"-vn -ss {start}"
    if start:
        return f"{before} -ss {start}", "-vn"
    return before, "-vn"

@dataclass
class Song:
    id: str | None
    title: str
    url: str
    page_url: str
    duration: int
    requester_id: int
    headers: dict
class GuildMusicState:
    def __init__(self):
        self.queue = deque()
        self.now = None
        self.player_task = None
        self.lock = asyncio.Lock()
        self.volume = 1.0
        self.autoplay = False
        self.seek_requested = False
        self.skip_requested = False
        self.next_start = None

class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}

    def _state(self, guild_id: int) -> GuildMusicState:
        return self.states.setdefault(guild_id, GuildMusicState())

    async def _extract(self, query: str) -> Song:
        def _do():
            with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
                info = ydl.extract_info(query, download=False)
                if "entries" in info:
                    info = info["entries"][0]

                url = info.get("url")
                if not url:
                    fmts = info.get("formats") or []
                    pick = next((f for f in fmts if f.get("ext") == "m4a" and f.get("acodec") != "none" and (f.get("protocol") or "").startswith("http")), None)
                    if not pick:
                        pick = next((f for f in fmts if "m3u8" in (f.get("protocol") or "") and f.get("acodec") != "none"), None)
                    if pick:
                        url = pick["url"]

                return Song(
                    id=info.get("id"),
                    title=info.get("title") or "Unknown",
                    url=url or info.get("webpage_url") or query,
                    page_url=info.get("webpage_url") or query,
                    duration=int(info.get("duration") or 0),
                    requester_id=0,
                    headers=info.get("http_headers") or {},
                )
        return await asyncio.to_thread(_do)

    async def _ensure_voice(self, interaction: discord.Interaction, channel: discord.VoiceChannel | None):
        vc = interaction.guild.voice_client
        if vc and vc.channel and (not channel or vc.channel.id == channel.id):
            return vc
        if not channel:
            if not isinstance(interaction.user, discord.Member) or not interaction.user.voice:
                raise RuntimeError("Join a voice channel first or provide one.")
            channel = interaction.user.voice.channel
        return await channel.connect()

    async def _run_player(self, guild: discord.Guild):
        state = self._state(guild.id)
        vc: discord.VoiceClient = guild.voice_client
        last_song = None

        while True:
            if state.now is None:
                if not state.queue:
                    state.player_task = None
                    break
                state.now = state.queue.popleft()

            song = state.now
            start = int(state.next_start or 0)
            state.next_start = None

            before, opts = _ff_args_for(song.url, song.headers, start)
            src = discord.FFmpegPCMAudio(song.url, before_options=before, options=opts)
            xform = discord.PCMVolumeTransformer(src, volume=state.volume)

            done = asyncio.Event()
            def _after(err):
                self.bot.loop.call_soon_threadsafe(done.set)

            vc.play(xform, after=_after)
            await done.wait()

            if state.skip_requested:
                state.skip_requested = False
                state.now = None
                continue

            if state.seek_requested:
                state.seek_requested = False
                continue

            last_song = song
            state.now = None

            if state.autoplay and not state.queue and last_song:
                picks = await self._search(last_song.title, 6)
                nxt = next((p for p in picks if p.id and last_song.id and p.id != last_song.id), None)
                if nxt:
                    state.queue.append(nxt)

    @app_commands.command(name="join", description="Join a voice channel")
    @app_commands.describe(channel="Voice channel")
    async def join(self, interaction: discord.Interaction, channel: discord.VoiceChannel | None = None):
        await interaction.response.defer(ephemeral=False)
        await self._ensure_voice(interaction, channel)
        await interaction.followup.send("Joined.", ephemeral=False)

    @app_commands.command(name="play", description="Queue a song by URL or search")
    @app_commands.describe(query="URL or search terms")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=False)
        vc = await self._ensure_voice(interaction, None)
        song = await self._extract(query)
        song.requester_id = interaction.user.id
        state = self._state(interaction.guild_id)
        state.queue.append(song)
        if not state.player_task or state.player_task.done():
            state.player_task = asyncio.create_task(self._run_player(interaction.guild))
        await interaction.followup.send(f"Queued: {song.title}")

    @app_commands.command(name="skip", description="Skip current song")
    async def skip(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            self._state(interaction.guild_id).skip_requested = True
            vc.stop()
            await interaction.followup.send("Skipped.", ephemeral=False)
        else:
            await interaction.followup.send("Nothing is playing.", ephemeral=False)

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.followup.send("Paused.")
        else:
            await interaction.followup.send("Nothing to pause.", ephemeral=False)

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.followup.send("Resumed.")
        else:
            await interaction.followup.send("Nothing to resume.", ephemeral=False)

    @app_commands.command(name="nowplaying", description="Show current song")
    async def nowplaying(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        s = state.now
        if not s:
            await interaction.response.send_message("Nothing is playing.", ephemeral=False)
            return
        await interaction.response.send_message(f"Now: {s.title} ({s.page_url})")

    @app_commands.command(name="queue", description="Show queue")
    async def queue_cmd(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        if not state.queue:
            await interaction.response.send_message("Queue is empty.", ephemeral=False)
            return
        lines = []
        for i, s in enumerate(list(state.queue)[:15], start=1):
            lines.append(f"{i}. {s.title}")
        more = f"\n… +{len(state.queue)-15} more" if len(state.queue) > 15 else ""
        await interaction.response.send_message("Queue:\n" + "\n".join(lines) + more)

    @app_commands.command(name="remove", description="Remove a song from the queue")
    @app_commands.describe(index="1-based index")
    async def remove(self, interaction: discord.Interaction, index: int):
        state = self._state(interaction.guild_id)
        if index < 1 or index > len(state.queue):
            await interaction.response.send_message("Invalid index.", ephemeral=False)
            return
        s = list(state.queue)[index - 1]
        del state.queue[index - 1]
        await interaction.response.send_message(f"Removed: {s.title}")

    @app_commands.command(name="move", description="Reorder the queue")
    @app_commands.describe(src="from index", dst="to index")
    async def move(self, interaction: discord.Interaction, src: int, dst: int):
        state = self._state(interaction.guild_id)
        q = state.queue
        if not (1 <= src <= len(q) and 1 <= dst <= len(q)):
            await interaction.response.send_message("Invalid indexes.", ephemeral=False)
            return
        lst = list(q)
        item = lst.pop(src - 1)
        lst.insert(dst - 1, item)
        state.queue = deque(lst)
        await interaction.response.send_message("Moved.")

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        lst = list(state.queue)
        random.shuffle(lst)
        state.queue = deque(lst)
        await interaction.response.send_message("Shuffled.")

    @app_commands.command(name="clear", description="Clear the queue")
    async def clear(self, interaction: discord.Interaction):
        state = self._state(interaction.guild_id)
        state.queue.clear()
        await interaction.response.send_message("Cleared.")

    @app_commands.command(name="leave", description="Disconnect and stop")
    async def leave(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        state = self._state(interaction.guild_id)
        state.queue.clear()
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect(force=True)
        state.now = None
        state.player_task = None
        await interaction.followup.send("Left.", ephemeral=False)

    @app_commands.command(name="volume", description="Set playback volume (0-150)")
    @app_commands.describe(percent="0-150")
    async def volume(self, interaction: discord.Interaction, percent: int):
        await interaction.response.defer(ephemeral=True)
        percent = max(0, min(150, percent))
        state = self._state(interaction.guild_id)
        state.volume = percent / 100.0
        vc = interaction.guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = state.volume
        await interaction.followup.send(f"Volume set to {percent}%", ephemeral=True)

    @app_commands.command(name="seek", description="Seek current song to position")
    @app_commands.describe(position="hh:mm:ss or mm:ss or seconds")
    async def seek(self, interaction: discord.Interaction, position: str):
        await interaction.response.defer(ephemeral=True)
        state = self._state(interaction.guild_id)
        s = state.now
        vc = interaction.guild.voice_client
        if not s or not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.followup.send("Nothing is playing.", ephemeral=True)
            return

        def parse_pos(text: str) -> int:
            if text.isdigit():
                return int(text)
            parts = [int(p) for p in text.split(":")]
            if len(parts) == 2:
                return parts[0]*60 + parts[1]
            if len(parts) == 3:
                return parts[0]*3600 + parts[1]*60 + parts[2]
            raise ValueError

        try:
            secs = parse_pos(position)
        except Exception:
            await interaction.followup.send("Invalid time format.", ephemeral=True)
            return

        if s.duration and secs >= s.duration:
            secs = max(0, s.duration - 1)

        state.next_start = secs
        state.seek_requested = True
        if vc.is_paused():
            vc.resume()
        vc.stop()
        await interaction.followup.send(f"Seeking to {secs}s.", ephemeral=True)

    @app_commands.command(name="autoplay", description="Toggle autoplay when queue ends")
    @app_commands.describe(mode="on or off")
    async def autoplay(self, interaction: discord.Interaction, mode: str):
        mode_l = mode.lower()
        if mode_l not in ("on", "off"):
            await interaction.response.send_message("Use on or off.", ephemeral=True)
            return
        state = self._state(interaction.guild_id)
        state.autoplay = (mode_l == "on")
        await interaction.response.send_message(f"Autoplay {'enabled' if state.autoplay else 'disabled'}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
