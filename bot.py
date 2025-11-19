
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
from commands.application import ApplicationReviewView, store_pending_application, delete_pending
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
    await bot.load_extension("commands.xp")
    await bot.load_extension("commands.role_menu")
    
    

def _build_application_embed_from_data(app_data: dict, applicant_id: int) -> discord.Embed:
    embed = discord.Embed(title="New Application", color=discord.Color.blue())
    embed.add_field(name="Preferred Name", value=app_data.get("name", "Unknown"), inline=False)
    embed.add_field(name="Pronouns", value=app_data.get("pronouns", "Unknown"), inline=False)
    embed.add_field(name="Branch", value=app_data.get("branch_choice", "Unknown"), inline=False)
    embed.add_field(name="Status", value=app_data.get("status_choice", "Unknown"), inline=False)
    embed.add_field(name="Referral Source", value=app_data.get("refer", "Unknown"), inline=False)
    embed.set_footer(text=f"Applicant ID {applicant_id}")
    return embed


async def _attach_review_view_to_message(
    target_channel: discord.abc.Messageable,
    message: discord.Message,
    applicant_id: int,
    app_data: dict,
):
    view = ApplicationReviewView(applicant_id=applicant_id, application_data=app_data, review_msg_id=message.id)
    try:
        await message.edit(view=view)
        bot.add_view(view, message_id=message.id)
        return
    except discord.HTTPException as exc:
        if getattr(exc, "code", None) != 50005:
            raise

    embed = message.embeds[0] if message.embeds else _build_application_embed_from_data(app_data, applicant_id)
    new_msg = await target_channel.send(embed=embed, view=view)
    view.review_msg_id = new_msg.id
    bot.add_view(view, message_id=new_msg.id)
    store_pending_application(new_msg.id, applicant_id, app_data)
    delete_pending(message_id=message.id)
    print(f"Reposted review message {message.id} as {new_msg.id} (original not authored by bot).")


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
            except Exception as e:
                print(f"Failed to fetch review message {message_id}: {e}")
                continue
            try:
                app_data = json.loads(raw)
            except json.JSONDecodeError:
                print(f"Invalid application data for {message_id}; skipping.")
                continue
            try:
                await _attach_review_view_to_message(staff_channel, msg, user_id, app_data)
            except Exception as e:
                print(f"Failed to reattach review view for {message_id}: {e}")

    # Audit persistent application views and register any missing ones
    conn = sqlite3.connect("applications.db")
    c = conn.cursor()
    c.execute("SELECT message_id, user_id, data FROM pending_applications")
    pending_rows = c.fetchall()
    conn.close()
    if staff_channel:
        for message_id, user_id, raw in pending_rows:
            if any(view for view in bot.persistent_views if isinstance(view, ApplicationReviewView) and getattr(view, "review_msg_id", None) == message_id):
                continue
            app_data = None
            try:
                app_data = json.loads(raw)
            except json.JSONDecodeError:
                pass
            review_view = ApplicationReviewView(applicant_id=user_id, application_data=app_data or {})
            review_view.review_msg_id = message_id
            bot.add_view(review_view, message_id=message_id)

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
