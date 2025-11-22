
import os
import signal
import csv
import asyncio
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from utils.responses import load_responses, match_response
from commands import general, moderation, application, osint, music
from commands.application import (
    init_db,
    ApplicationView,
    TicketCloseView,
)
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
from commands.application import (
    ApplicationReviewView,
    store_pending_application,
    delete_pending,
    process_application_decision,
    update_application_status,
    parse_application_embed,
    refresh_ticket_views,
)
from commands.tickets import refresh_claimed_ticket_views
from types import SimpleNamespace
import discord.opus
import pkgutil, importlib
import utils.responses as r
from aiohttp import web
import subprocess


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
CONTROL_API_HOST = os.getenv("BOT_CONTROL_HOST", "127.0.0.1")
CONTROL_API_PORT = int(os.getenv("BOT_CONTROL_PORT", "8765"))
CONTROL_API_TOKEN = os.getenv("BOT_CONTROL_TOKEN", "")
XP_CHANNEL_ID = int(os.getenv("XP_NOTIFICATION_CHANNEL_ID", "0"))
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
REPO_ROOT = Path(__file__).resolve().parent
LAST_COMMIT_FILE = REPO_ROOT / "data" / "last_commit.txt"

token = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

LAST_PRUNE_FILE = Path("last_prune.txt")
control_app = web.Application()
control_runner = None
control_site = None
control_server_started = False


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


async def handle_control_decision(request: web.Request):
    if CONTROL_API_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {CONTROL_API_TOKEN}":
            return web.json_response({"message": "Unauthorized."}, status=401)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"message": "Invalid JSON payload."}, status=400)
    user_id = payload.get("userId") or payload.get("user_id")
    decision = (payload.get("decision") or payload.get("status") or "").strip().lower()
    reason = (payload.get("reason") or "").strip()
    reviewer = payload.get("reviewer") or "External Admin"
    if not user_id:
        return web.json_response({"message": "userId is required."}, status=400)
    if decision not in {"approved", "denied"}:
        return web.json_response({"message": "Decision must be 'approved' or 'denied'."}, status=400)
    try:
        applicant_id = int(user_id)
    except (TypeError, ValueError):
        return web.json_response({"message": "userId must be an integer."}, status=400)
    await bot.wait_until_ready()
    try:
        result = await process_application_decision(
            bot,
            applicant_id=applicant_id,
            approved=(decision == "approved"),
            reviewer_name=reviewer,
            reason=reason,
        )
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    except Exception as exc:
        logging.exception("Failed to handle external decision", exc_info=exc)
        return web.json_response({"message": "Internal error processing decision."}, status=500)
    message = f"Application {decision} for {result['user_id']}."
    if reason:
        message += f" Reason: {reason}"
    return web.json_response({"message": message, "result": result})


control_app.add_routes([web.post("/applications/decision", handle_control_decision)])


async def handle_control_prune(request: web.Request):
    if CONTROL_API_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {CONTROL_API_TOKEN}":
            return web.json_response({"message": "Unauthorized."}, status=401)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    await bot.wait_until_ready()
    guild_id_raw = payload.get("guildId") or os.getenv("HOME_GUILD_ID") or os.getenv("GUILD_ID") or 0
    try:
        guild_id = int(guild_id_raw)
    except (TypeError, ValueError):
        return web.json_response({"message": "Invalid guild ID."}, status=400)
    guild = bot.get_guild(guild_id)
    if guild is None:
        try:
            guild = await bot.fetch_guild(guild_id)
        except discord.HTTPException:
            guild = None
    if guild is None:
        return web.json_response({"message": "Guild not found."}, status=404)
    with sqlite3.connect(application.DB_PATH) as con:
        pending_rows = con.execute("SELECT message_id,user_id FROM pending_applications").fetchall()
        application_rows = con.execute("SELECT user_id FROM applications WHERE status='pending'").fetchall()
    removed = []
    checked = set()

    async def ensure_member(uid: int):
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None
        return member

    for message_id, user_id in pending_rows:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            uid = None
        if not uid:
            delete_pending(message_id=message_id)
            continue
        checked.add(uid)
        member = await ensure_member(uid)
        if member:
            continue
        delete_pending(user_id=uid, message_id=message_id)
        update_application_status(uid, 'closed')
        removed.append(uid)

    for (user_id,) in application_rows:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            continue
        if uid in checked:
            continue
        member = await ensure_member(uid)
        if member:
            continue
        update_application_status(uid, 'closed')
        removed.append(uid)

    return web.json_response({"removed": removed, "count": len(removed)})


control_app.add_routes([web.post("/applications/prune", handle_control_prune)])


async def start_control_api():
    global control_runner, control_site, control_server_started
    if control_server_started:
        return
    control_runner = web.AppRunner(control_app)
    await control_runner.setup()
    control_site = web.TCPSite(control_runner, CONTROL_API_HOST, CONTROL_API_PORT)
    await control_site.start()
    control_server_started = True
    print(f"[control] Admin API listening on {CONTROL_API_HOST}:{CONTROL_API_PORT}")

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


def _run_git_command(args: list[str]) -> str:
    return subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), *args],
        text=True,
        stderr=subprocess.STDOUT,
    )


async def announce_recent_commits(bot: commands.Bot) -> None:
    if not XP_CHANNEL_ID:
        return
    try:
        head = _run_git_command(["rev-parse", "HEAD"]).strip()
    except Exception as exc:
        logging.warning("Unable to determine current commit: %s", exc)
        return
    last_hash = ""
    if LAST_COMMIT_FILE.exists():
        try:
            last_hash = LAST_COMMIT_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            last_hash = ""
    if head == last_hash:
        return
    if not last_hash:
        LAST_COMMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_COMMIT_FILE.write_text(head, encoding="utf-8")
        return
    try:
        log_raw = _run_git_command(
            ["log", f"{last_hash}..{head}", "--pretty=format:%H%x1f%an%x1f%s%x1e", "--no-merges"]
        )
    except Exception as exc:
        logging.warning("Failed to read git log for announcements: %s", exc)
        return
    records = [entry for entry in log_raw.strip().split("\x1e") if entry.strip()]
    if not records:
        LAST_COMMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_COMMIT_FILE.write_text(head, encoding="utf-8")
        return
    updates = []
    for record in records:
        parts = record.split("\x1f")
        if len(parts) < 3:
            continue
        commit_hash, author, subject = parts[:3]
        commit_url = f"https://github.com/{GITHUB_REPO}/commit/{commit_hash}" if GITHUB_REPO else commit_hash
        updates.append((commit_hash, author, subject, commit_url))
    if not updates:
        LAST_COMMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_COMMIT_FILE.write_text(head, encoding="utf-8")
        return
    message_lines = ["**Latest Bot Updates**"]
    for commit_hash, author, subject, url in reversed(updates):
        short_hash = commit_hash[:7]
        if url == commit_hash:
            message_lines.append(f"- `{short_hash}` {subject} — {author}")
        else:
            message_lines.append(f"- [`{short_hash}`]({url}) {subject} — {author}")
    channel = bot.get_channel(XP_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(XP_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None
    if channel is None:
        logging.warning("XP channel %s not found for commit announcement.", XP_CHANNEL_ID)
        return
    try:
        await channel.send("\n".join(message_lines))
    except (discord.Forbidden, discord.HTTPException) as exc:
        logging.warning("Failed to send commit announcement: %s", exc)
        return
    LAST_COMMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_COMMIT_FILE.write_text(head, encoding="utf-8")


async def _backfill_pending_applications_from_history(channel: discord.abc.Messageable) -> int:
    """Recreate pending application rows by parsing historical embeds if needed."""
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return 0
    bot_id = bot.user.id if bot.user else None
    with sqlite3.connect(application.DB_PATH) as con:
        existing_ids = {row[0] for row in con.execute("SELECT message_id FROM pending_applications")}
    restored = 0
    try:
        async for message in channel.history(limit=200):
            if bot_id and message.author and message.author.id != bot_id:
                continue
            if not message.embeds or not message.components:
                continue
            embed = message.embeds[0]
            if (embed.title or "").lower() != "new application":
                continue
            applicant_id, parsed = parse_application_embed(embed)
            if not applicant_id or message.id in existing_ids:
                continue
            store_pending_application(message.id, applicant_id, parsed)
            existing_ids.add(message.id)
            restored += 1
    except (discord.Forbidden, discord.HTTPException) as exc:
        print(f"Unable to scan staff review channel for pending applications: {exc}")
        return 0
    if restored:
        print(f"Backfilled {restored} pending application record(s) from channel history.")
    return restored


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

    await refresh_ticket_views(bot)
    await refresh_claimed_ticket_views(bot)
    staff_channel = bot.get_channel(STAFF_REVIEW_CHANNEL_ID)
    rows = []
    if staff_channel:
        await _backfill_pending_applications_from_history(staff_channel)
        with sqlite3.connect(application.DB_PATH) as con:
            rows = con.execute("SELECT message_id, user_id, data FROM pending_applications").fetchall()
        for message_id, user_id, raw in rows:
            try:
                msg = await staff_channel.fetch_message(message_id)
            except Exception as e:
                print(f"Failed to fetch review message {message_id}: {e}")
                continue

            app_data = None
            try:
                app_data = json.loads(raw)
            except json.JSONDecodeError:
                embed = msg.embeds[0] if msg.embeds else None
                _, parsed = parse_application_embed(embed)
                if parsed:
                    app_data = parsed
                    store_pending_application(message_id, user_id, parsed)
                else:
                    print(f"Invalid application data for {message_id}; skipping.")
                    continue

            if msg.components:
                review_view = ApplicationReviewView(applicant_id=user_id, application_data=app_data or {})
                review_view.review_msg_id = message_id
                bot.add_view(review_view, message_id=message_id)
                continue

            try:
                await _attach_review_view_to_message(staff_channel, msg, user_id, app_data or {})
            except Exception as e:
                print(f"Failed to reattach review view for {message_id}: {e}")

    await announce_recent_commits(bot)
    print("Bot is ready and applications work!.")
    bot.loop.create_task(start_control_api())


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
