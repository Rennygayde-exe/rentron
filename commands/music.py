# commands/music.py
import discord
from discord import app_commands, Interaction
from discord.ext import commands
import yt_dlp
from discord import VoiceChannel
from discord import FFmpegPCMAudio
import asyncio
import os




@app_commands.command(name="join", description="Make the bot join your voice channel.")
async def join(interaction: Interaction):
    vc = interaction.user.voice.channel if interaction.user.voice else None
    if not vc:
        await interaction.response.send_message(
            "You're not in a voice channel!", ephemeral=True
        )
        return

    await interaction.response.defer()
    await vc.connect()
    await interaction.followup.send(f"Joined **{vc.name}**.")

@app_commands.command(name="play", description="Play audio from a YouTube URL.")
@app_commands.describe(url="YouTube video URL")
async def play(interaction: discord.Interaction, url: str):
    voice = interaction.guild.voice_client
    if not voice or not voice.is_connected():
        return await interaction.response.send_message(
            "I’m not in a voice channel—use /join first.", ephemeral=True
        )

    await interaction.response.defer()

    ytdl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
    }
    with yt_dlp.YoutubeDL(ytdl_opts) as ytdl:
        info = ytdl.extract_info(url, download=False)
    if 'entries' in info:
        info = info['entries'][0]
    audio_url = info.get('url')
    if not audio_url:
        return await interaction.followup.send(
            "Could not retrieve any playable audio URL.", ephemeral=True
        )

    before_opts = (
        '-reconnect 1 '
        '-reconnect_streamed 1 '
        '-reconnect_delay_max 5 '
        '-protocol_whitelist file,http,https,tcp,tls,crypto'
    )
    ffmpeg_opts = '-vn -ar 48000 -ac 2'

    source = FFmpegPCMAudio(
        audio_url,
        before_options=before_opts,
        options=ffmpeg_opts
    )

    voice.stop()
    voice.play(source, after=lambda e: print(f"Player error: {e}") if e else None)

    title = info.get('title', 'unknown track')
    await interaction.followup.send(f"Now playing: **{title}**")
@app_commands.command(name="stop", description="Stop playback and leave the channel.")
async def stop(interaction: Interaction):
    voice = interaction.guild.voice_client
    if not voice or not voice.is_connected():
        await interaction.response.send_message(
            "I'm not in a voice channel.", ephemeral=True
        )
        return

    await interaction.response.defer()
    await voice.disconnect()
    await interaction.followup.send("Bai!.")

async def setup(bot: commands.Bot):
    bot.tree.add_command(join)
    bot.tree.add_command(play)
    bot.tree.add_command(stop)
