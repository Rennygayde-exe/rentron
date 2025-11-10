
import os
import signal
import csv
import asyncio
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from utils.responses import load_responses, match_response
from commands import general, moderation, application, osint, music
from commands.application import init_db
from commands.application import init_db, ApplicationView, TicketCloseView
from discord import app_commands
from commands.osint import blackbird
from signal_handler import signal_command
from discord.ui import View, Button, Modal, TextInput
from discord import Interaction, TextStyle
import logging
import io
import time
import json
import sqlite3
from pathlib import Path
import datetime
from datetime import datetime, timedelta, timezone
from utils import DummyInteraction
from commands.application import ApplicationReviewView
from types import SimpleNamespace
import discord.opus
import pkgutil, importlib
import utils.responses as r


try:
    import nacl
except ImportError:
    print("PyNaCl is not installed!")
else:
    print("PyNaCl is installed.")


log_buffer = io.StringIO()
handler = logging.StreamHandler(log_buffer)
formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', "%Y-%m-%d %H:%M:%S")
handler.setFormatter(formatter)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(handler)

load_dotenv()
BLACKBIRDLOGS_ID = int(os.getenv("BLACKBIRDLOGS_ID", 0))
STAFF_REVIEW_CHANNEL_ID = int(os.getenv("STAFF_REVIEW_CHANNEL_ID", "0"))
PRUNE_LOG_CHANNEL_ID = int(os.getenv("STAFF_REVIEW_CHANNEL_ID", "0"))

token = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

LAST_PRUNE_FILE = Path("last_prune.txt")


def shutdown_handler(sig, frame):
    print("\n[main] Shutdown signal received.")
    try:
        loop = asyncio.get_event_loop()
        for task in asyncio.all_tasks(loop):
            task.cancel()
    except RuntimeError:
        pass
    exit(0)

signal.signal(signal.SIGINT, shutdown_handler)

async def load_extensions():
    await bot.load_extension("commands.music")
    await bot.load_extension("commands.e2simulator")
    await bot.load_extension("commands.ssh")
    await bot.load_extension("commands.tts")
    await bot.load_extension("commands.admin_reload")
    await bot.load_extension("commands.moderation")
    await bot.load_extension("commands.application")
    await bot.load_extension("commands.pruning_logic")
    await bot.load_extension("commands.say")
    await bot.load_extension("commands.keyword_alerts")
    await bot.load_extension("commands.vsp")
    await bot.load_extension("commands.encode")
    await bot.load_extension("commands.tickets")
    await bot.load_extension("commands.audit")
    await bot.load_extension("commands.regexsearch")
    
    

async def main():
    async with bot:
        await bot.start(TOKEN)
@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user}")
    discord.opus.load_opus("/usr/lib/libopus.so")
    print(">>> Opus loaded?", discord.opus.is_loaded())

    # Command Reg
    bot.add_command(general.reload_responses)
    bot.add_command(general.list_responses)
    general.setup(bot.tree)
    await load_extensions()
    bot.tree.add_command(signal_command)
    bot.tree.add_command(blackbird)
    await bot.tree.sync()

    # Load responses
    r.load_responses()
    general.load_out_of_office()
    # Application Button Refresh
    bot.add_view(ApplicationView())
    bot.add_view(TicketCloseView())

    conn = sqlite3.connect("applications.db")
    c = conn.cursor()
    c.execute("SELECT message_id, user_id, data FROM pending_applications")
    rows = c.fetchall()
    conn.close()

    conn = sqlite3.connect("applications.db")
    c = conn.cursor()
    c.execute("SELECT message_id, channel_id FROM tickets")
    for msg_id, chan_id in c.fetchall():
        channel = bot.get_channel(chan_id)
        if not channel:
            continue
        try:
            await channel.fetch_message(msg_id)
            bot.add_view(TicketCloseView(), message_id=msg_id)
        except Exception:
            pass
    conn.close()
    staff_channel = bot.get_channel(STAFF_REVIEW_CHANNEL_ID)
    if staff_channel:
        for message_id, user_id, raw in rows:
            try:
                msg = await staff_channel.fetch_message(message_id)
                app_data = json.loads(raw)
                view = ApplicationReviewView(applicant_id=user_id, application_data=app_data)

                bot.add_view(view, message_id=msg.id)

                await msg.edit(view=view)

            except Exception as e:
                print(f"Failed to reattach review view for {message_id}: {e}")

    print("Bot is ready and applications work!.")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild or not message.content:
        return

    txt = message.content
    if message.mentions:
        mention_responses = []
        seen_ids = set()
        for member in message.mentions:
            if member.bot or member.id == message.author.id:
                continue
            if member.id in seen_ids:
                continue
            seen_ids.add(member.id)
            status = general.get_out_of_office_status(member.id)
            if status:
                note = status.get("message") or "is currently out of office."
                mention_responses.append(f"{member.display_name} is out of office: {note}")
        if mention_responses:
            await message.channel.send(
                "\n".join(mention_responses),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    for entry in r.RESPONSES:
        if r.match_response(txt, entry):
            resp = entry.get("response", "")
            if resp:
                await message.channel.send(resp, allowed_mentions=discord.AllowedMentions.none())
            break

    await bot.process_commands(message)
if __name__ == "__main__":
    import threading

    if token:
        bot.run(token)
    else:
        print("Bot token not found in .env file.")
