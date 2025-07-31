
import os
import signal
import asyncio
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from utils.responses import load_responses, match_response
from commands import general, moderation, application, osint
from commands.application import init_db
from commands.application import ApplicationView
from discord import app_commands
from commands import pruning_logic
from commands.osint import blackbird
from commands.pruning_logic import load_prune_schedule, save_prune_schedule, prune_attachments, LAST_PRUNE_FILE
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

token = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

LAST_PRUNE_FILE = Path("last_prune.txt")

def get_last_prune_time():
    if LAST_PRUNE_FILE.exists():
        with LAST_PRUNE_FILE.open() as f:
            return datetime.fromisoformat(f.read().strip())
    return None

def set_last_prune_time(dt: datetime):
    with LAST_PRUNE_FILE.open("w") as f:
        f.write(dt.isoformat())

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
    await bot.load_extension("commands.osint")
async def main():
    async with bot:
        await load_extensions()
        await bot.start(TOKEN)
@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user}")

    #Start Task Loops
    scheduled_prune.start()

    # Command Reg
    bot.add_command(general.reload_responses)
    bot.add_command(general.list_responses)
    general.setup(bot.tree)
    moderation.setup(bot.tree)
    application.setup(bot.tree)
    bot.tree.add_command(signal_command)
    bot.tree.add_command(blackbird)
    pruning_logic.setup(bot.tree)
    await bot.tree.sync()

    # Load responses
    load_responses()

    # Application Button Refresh
    bot.add_view(ApplicationView())

    conn = sqlite3.connect("applications.db")
    c = conn.cursor()
    c.execute("SELECT message_id, user_id, data FROM pending_applications")
    rows = c.fetchall()
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

@bot.tree.command(name="post_application", description="Post the application button")
@app_commands.checks.has_permissions(administrator=True)
async def post_application(interaction: discord.Interaction):
    await interaction.response.send_message("Click below to apply!", view=ApplicationView())

@tasks.loop(minutes=10)
async def scheduled_prune():
    await bot.wait_until_ready()

    now = datetime.now(timezone.utc)

    try:
        last_prune = get_last_prune_time() or now
    except Exception as e:
        print(f"[scheduled_prune] Failed to read last_prune.txt: {e}")
        last_prune = now
    config = load_prune_schedule()
    interval = config.get("interval", 72)
    unit = config.get("unit", "hours")
    time_kwargs = {unit: interval} if unit != "years" else {"days": interval * 365}
    next_run = last_prune + timedelta(**time_kwargs)
    channel_id = config.get("channel_id")

    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    now = datetime.now(timezone.utc)
    last_prune = get_last_prune_time()

    if not last_prune or (now - last_prune).total_seconds() >= interval * 3600:
        class DummyUser:
            def __init__(self, guild):
                self.roles = [type("Role", (), {"name": "Staff"})()]
                self.mention = bot.user.mention
                self.guild = guild

        dummy_interaction = type("DummyInteraction", (), {
            "user": DummyUser(channel.guild),
            "guild": channel.guild,
            "followup": channel,
            "response": type("Resp", (), {"is_done": lambda: True, "defer": lambda **kwargs: None})()
        })()

        dummy_interaction = DummyInteraction()
        deleted_count, csv_file = await prune_attachments(dummy_interaction, channel, 3, "days", "images")
        set_last_prune_time(datetime.now(timezone.utc))
        log_channel = bot.get_channel(BLACKBIRDLOGS_ID)
        if log_channel:
            await log_channel.send(
                f"Scheduled prune completed in {channel.mention} at <t:{int(now.timestamp())}:f>. `{deleted_count}` messages deleted.",
                file=csv_file if csv_file else None
            )



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

