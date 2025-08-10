
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
from commands import pruning_logic
from commands.osint import blackbird
from commands.pruning_logic import load_prune_schedule, get_last_prune_time, set_last_prune_time, prune_attachments, UNIT_SECONDS
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
import commands.music as music

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
    

async def main():
    async with bot:
        await bot.start(TOKEN)
@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user}")
    discord.opus.load_opus("/usr/lib/libopus.so")
    print(">>> Opus loaded?", discord.opus.is_loaded())

    #Start Task Loops
    scheduled_prune.start()
    # Command Reg
    bot.add_command(general.reload_responses)
    bot.add_command(general.list_responses)
    general.setup(bot.tree)
    await load_extensions()
    bot.tree.add_command(signal_command)
    bot.tree.add_command(blackbird)
    pruning_logic.setup(bot.tree)
    await bot.tree.sync()

    # Load responses
    load_responses()

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

@tasks.loop(minutes=30)
async def scheduled_prune():
    await bot.wait_until_ready()

    cfg = load_prune_schedule()
    chan_id = cfg.get("channel_id")
    if not chan_id:
        print("[scheduled_prune] no prune channel configured")
        return

    channel = bot.get_channel(chan_id)
    log_chan = bot.get_channel(PRUNE_LOG_CHANNEL_ID)
    if not channel:
        print(f"[scheduled_prune] prune channel {chan_id} not found")
        return

    amt  = cfg["interval"]
    unit = cfg["unit"]
    threshold = amt * UNIT_SECONDS.get(unit, 0)

    now  = datetime.now(timezone.utc)
    last = get_last_prune_time()

    if last is None:
        print(f"[scheduled_prune] first run, pruning immediately")
        do_prune = True
    else:
        elapsed = (now - last).total_seconds()
        print(f"[scheduled_prune] elapsed={elapsed:.1f}s threshold={threshold:.1f}s")
        do_prune = (elapsed >= threshold)

    if not do_prune:
        return

    class DummyUser:
        def __init__(self, g): 
            self.roles   = [type("R", (), {"name":"Staff"})()]
            self.guild   = g
            self.mention = bot.user.mention

    class DummyResponse:
        def is_done(self):      return True
        async def defer(self, **_): pass

    class DummyFollowup:
        async def send(self, content=None, **kw):
            return await channel.send(content, **{k:v for k,v in kw.items() if k!="ephemeral"})

    dummy = type("DI", (), {})()
    dummy.user     = DummyUser(channel.guild)
    dummy.guild    = channel.guild
    dummy.response = DummyResponse()
    dummy.followup = DummyFollowup()

    deleted = await prune_attachments(dummy, channel, amt, unit, "all")
    set_last_prune_time(now)
    print(f"[scheduled_prune] pruned {len(deleted)} items")

    if log_chan and deleted:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["id","author","created","attachment","channel"])
        writer.writeheader()
        writer.writerows(deleted)
        buf.seek(0)
        f = discord.File(fp=io.BytesIO(buf.read().encode()), filename="prune_log.csv")
        await log_chan.send(f"Auto-prune on {channel.mention}: deleted {len(deleted)} attachments.", file=f)
@bot.event
async def on_message(message):
    await bot.process_commands(message)
    if message.author.bot:
        return
    response = match_response(message, bot)
    if response:
        await message.channel.send(response)

if __name__ == "__main__":
    import threading

    if token:
        bot.run(token)
    else:
        print("Bot token not found in .env file.")

