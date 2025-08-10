import os, uuid
from typing import Literal
import discord
from discord import app_commands
from discord.ext import commands
import edge_tts

EMPEROR_FILTER = (
    "aecho=0.6:0.6:60|80:0.30|0.25,"
    "aresample=44100,"
    "asetrate=44100*0.707107,atempo=1.414214,"
    "bass=g=8:f=200,treble=g=-2,"
    "compand=attacks=0.1:decays=0.3:points=-80/-80|-20/-10|0/-3|20/0:soft-knee=6:gain=5"
)

EMPEROR_CANDIDATES = [
    "en-US-ChristopherNeural",
    "en-US-GuyNeural"
]

def clamp(v, lo, hi): return max(lo, min(hi, v))
def rate_str(n: int) -> str: return f"{n:+d}%"

class TTS(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._voices = None

    async def _ensure_voice(self, interaction: discord.Interaction, channel: discord.VoiceChannel | None):
        vc = interaction.guild.voice_client
        if vc and vc.channel and (not channel or vc.channel.id == getattr(channel, "id", None)):
            return vc
        if not channel:
            m = interaction.user
            if not isinstance(m, discord.Member) or not m.voice or not isinstance(m.voice.channel, discord.VoiceChannel):
                raise RuntimeError("Join a voice channel or specify one.")
            channel = m.voice.channel
        return await channel.connect()

    async def _list_voices(self):
        if self._voices is None:
            self._voices = await edge_tts.list_voices()
        return self._voices

    async def _resolve_voice(self, preferred: list[str] | None, fallback_locale: str = "en-GB", fallback_gender: str = "Male") -> str:
        voices = await self._list_voices()
        names = {v["ShortName"] for v in voices}
        if preferred:
            for p in preferred:
                if p in names:
                    return p
        for v in voices:
            if v.get("Locale") == fallback_locale and v.get("Gender") == fallback_gender:
                return v["ShortName"]
        return "en-US-GuyNeural"

    async def _synthesize(self, *, text: str, voice: str, rate: int, pitch: str | None) -> str:
        path = f"/tmp/tts_{uuid.uuid4().hex}.mp3"
        kwargs = {"text": text, "voice": voice, "rate": rate_str(rate)}
        if pitch:
            kwargs["pitch"] = pitch
        await edge_tts.Communicate(**kwargs).save(path)
        return path

    @app_commands.command(name="tts", description="Speak text in a voice channel")
    @app_commands.describe(
        text="What to say",
        style="normal or emperor",
        voice="Override ShortName (optional)",
        rate="−50..50 (%)",
        pitch="Pitch string (e.g. +2st or -10%) (optional)",
        channel="Voice channel (optional)"
    )
    async def tts(
        self,
        interaction: discord.Interaction,
        text: str,
        style: Literal["normal","emperor"] = "normal",
        voice: str | None = None,
        rate: int | None = None,
        pitch: str | None = None,
        channel: discord.VoiceChannel | None = None
    ):
        await interaction.response.defer(ephemeral=True)
        vc = await self._ensure_voice(interaction, channel)

        if style == "emperor":
            if not voice:
                voice = await self._resolve_voice(EMPEROR_CANDIDATES)
            edge_rate = -6 if rate is None else clamp(rate, -50, 50)
            edge_pitch = None if pitch is None else pitch 
            ff_opts = f"-vn -af {EMPEROR_FILTER}"
        else:
            if not voice:
                voice = await self._resolve_voice(None, fallback_locale="en-US")
            edge_rate = 0 if rate is None else clamp(rate, -50, 50)
            edge_pitch = pitch
            ff_opts = "-vn"

        try:
            path = await self._synthesize(text=text, voice=voice, rate=edge_rate, pitch=edge_pitch)
        except Exception as e:
            try:
                path = await self._synthesize(text=text, voice=voice, rate=0, pitch=None)
            except Exception as e2:
                await interaction.followup.send(f"TTS failed: {e2}", ephemeral=True)
                return

        if vc.is_playing():
            vc.stop()
        src = discord.FFmpegPCMAudio(path, options=ff_opts)
        def _after(_):
            try: os.remove(path)
            except OSError: pass
        vc.play(discord.PCMVolumeTransformer(src, volume=1.0), after=_after)
        await interaction.followup.send(f"Speaking with {style} style ({voice}).", ephemeral=True)

    @app_commands.command(name="tts_voices", description="List available voices (first 25)")
    @app_commands.describe(filter="Substring to match")
    async def tts_voices(self, interaction: discord.Interaction, filter: str | None = None):
        await interaction.response.defer(ephemeral=True)
        voices = await self._list_voices()
        if filter:
            f = filter.lower()
            voices = [v for v in voices if f in v["ShortName"].lower() or f in v["Locale"].lower() or f in v["Gender"].lower()]
        voices = voices[:25]
        lines = [f'{v["ShortName"]} ({v["Locale"]}, {v["Gender"]})' for v in voices]
        await interaction.followup.send("\n".join(lines) if lines else "No matches.", ephemeral=True)

    @app_commands.command(name="tts_stop", description="Stop current TTS")
    async def tts_stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message("Stopped.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing to stop.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(TTS(bot))