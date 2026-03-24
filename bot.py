
import os
import signal
import csv
import html
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
from commands.captcha import CaptchaView
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
    mark_as_submitted,
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
import utils.pentagon_pizza_index as pizza_index
from aiohttp import web
import subprocess
import utils.usage_log as usage_log


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
PENTAGON_PIZZA_ASSETS = REPO_ROOT / "web_assets" / "pentagon_pizza"
PENTAGON_PIZZA_VIDEO = REPO_ROOT / "begin_again.mp4"

token = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

LAST_PRUNE_FILE = Path("last_prune.txt")
control_app = web.Application()
control_runner = None
control_site = None
control_server_started = False
PENTAGON_PIZZA_ASSETS.mkdir(parents=True, exist_ok=True)
control_app.router.add_static("/pentagon-pizza/static/", str(PENTAGON_PIZZA_ASSETS), show_index=False)


def _top_role_info(member: discord.Member | None) -> tuple[int | None, str | None]:
    if not member:
        return None, None
    roles = [role for role in getattr(member, "roles", []) if not getattr(role, "is_default", lambda: False)()]
    if not roles:
        return None, None
    top_role = max(roles, key=lambda r: r.position)
    return top_role.id, top_role.name


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


async def handle_control_submit(request: web.Request):
    if CONTROL_API_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {CONTROL_API_TOKEN}":
            return web.json_response({"message": "Unauthorized."}, status=401)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"message": "Invalid JSON payload."}, status=400)
    data = payload.get("data") if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        data = {}
    user_id = payload.get("userId") or payload.get("user_id") or data.get("discord_id") or data.get("discordId")
    if not user_id:
        return web.json_response({"message": "userId is required."}, status=400)
    try:
        applicant_id = int(user_id)
    except (TypeError, ValueError):
        return web.json_response({"message": "userId must be an integer."}, status=400)

    merged = dict(data)
    for key in ("name", "pronouns", "refer", "branch_choice", "status_choice", "email", "phone", "register_ip"):
        value = payload.get(key)
        if value and key not in merged:
            merged[key] = value
    merged.setdefault("discord_id", str(applicant_id))
    for key in ("discord_tag", "discord_username", "discord_display_name", "discord_global_name", "source"):
        value = payload.get(key)
        if value and key not in merged:
            merged[key] = value

    required = [k for k in ("name", "pronouns", "refer", "branch_choice", "status_choice") if not merged.get(k)]
    if required:
        return web.json_response({"message": f"Missing fields: {', '.join(required)}."}, status=400)

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

    staff_channel_id = payload.get("staffChannelId") or STAFF_REVIEW_CHANNEL_ID
    try:
        staff_channel_id = int(staff_channel_id)
    except (TypeError, ValueError):
        staff_channel_id = 0
    staff_channel = guild.get_channel(staff_channel_id) if staff_channel_id else None
    if staff_channel is None and staff_channel_id:
        try:
            staff_channel = await guild.fetch_channel(staff_channel_id)
        except discord.HTTPException:
            staff_channel = None
    if staff_channel is None:
        return web.json_response({"message": "Staff review channel not found."}, status=404)

    embed = _build_application_embed_from_data(merged, applicant_id)
    footer_tag = (
        merged.get("discord_tag")
        or merged.get("discord_username")
        or merged.get("discord_global_name")
        or merged.get("discord_display_name")
        or "Web Applicant"
    )
    embed.set_footer(text=f"{footer_tag} ({applicant_id})")

    review_view = ApplicationReviewView(applicant_id, application_data=merged)
    review_msg = await staff_channel.send(embed=embed, view=review_view)
    review_view.review_msg_id = review_msg.id
    try:
        bot.add_view(review_view, message_id=review_msg.id)
    except Exception:
        pass
    store_pending_application(review_msg.id, applicant_id, merged)
    mark_as_submitted(applicant_id, discord.utils.utcnow().isoformat())

    return web.json_response(
        {"message": "Application submitted.", "messageId": review_msg.id, "userId": str(applicant_id)}
    )


control_app.add_routes([web.post("/applications/submit", handle_control_submit)])


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


def _render_pentagon_pizza_page(graph_b64: str, summary: dict, history_count: int) -> str:
    latest = summary.get("latest") or {}
    super_busy = bool(summary.get("super_busy"))
    busy_class = "super-busy" if super_busy else "calm"
    busy_label = "SUPER BUSY" if super_busy else "Nominal watch"
    busy_count = latest.get("busy_count", 0)
    capacity = latest.get("capacity", 12)
    latest_index = latest.get("index", 0.0)
    avg_index = summary.get("average_index", 0.0)
    max_index = summary.get("max_index", 0.0)
    timestamp = latest.get("timestamp") or "No observations yet"
    stats_line = f"{busy_count} busy out of {capacity} tracked" if latest else "No samples logged yet"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Pentagon Pizza Index</title>
  <style>
    :root {{
      --bg: #0b1022;
      --panel: rgba(12, 21, 44, 0.82);
      --accent: #ff6b35;
      --ink: #e8f0ff;
      --muted: #8fa3bf;
      --cyan: #5de4c7;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "JetBrains Mono", "Fira Code", "Cascadia Code", Menlo, monospace;
      background: radial-gradient(circle at 20% 20%, #111a33, #070c1a 60%), linear-gradient(135deg, #0c1328, #0f0c1b 55%, #1a0f2c);
      color: var(--ink);
      overflow-x: hidden;
    }}
    body.super-busy {{
      box-shadow: inset 0 0 0 4px rgba(255, 107, 53, 0.4);
    }}
    #bg-video {{
      position: fixed;
      inset: 0;
      width: 100%;
      height: 100%;
      object-fit: cover;
      opacity: { "0.92" if super_busy else "0" };
      transition: opacity 0.6s ease;
      pointer-events: none;
      filter: saturate(0.8) brightness(0.65);
      z-index: 0;
    }}
    .overlay {{
      position: fixed;
      inset: 0;
      background: linear-gradient(180deg, rgba(0,0,0,0.55), rgba(7,10,20,0.8));
      z-index: 1;
      pointer-events: none;
    }}
    main {{
      position: relative;
      z-index: 2;
      max-width: 1100px;
      margin: 0 auto;
      padding: 40px 20px 80px;
      display: grid;
      gap: 20px;
    }}
    header h1 {{
      margin: 0 0 8px 0;
      font-size: 28px;
      letter-spacing: 0.5px;
    }}
    header p {{
      margin: 0;
      color: var(--muted);
    }}
    .status {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: rgba(255, 107, 53, 0.12);
      color: { "#0d0d0d" if super_busy else "#ffdab8" };
      border: 1px solid rgba(255, 107, 53, 0.5);
      padding: 8px 12px;
      border-radius: 10px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 1px;
      box-shadow: 0 12px 30px rgba(0,0,0,0.35);
    }}
    .cards {{
      display: grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 16px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid rgba(255,255,255,0.05);
      border-radius: 14px;
      padding: 18px;
      backdrop-filter: blur(12px);
      box-shadow: 0 12px 36px rgba(0,0,0,0.35);
    }}
    .card img.graph {{
      width: 100%;
      display: block;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.08);
      background: #0f162c;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0,1fr));
      gap: 12px;
      margin: 16px 0 6px;
    }}
    .stat {{
      padding: 12px;
      border-radius: 12px;
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.05);
    }}
    .stat .label {{
      font-size: 12px;
      color: var(--muted);
      letter-spacing: 0.4px;
    }}
    .stat .value {{
      font-size: 20px;
      font-weight: 700;
      color: var(--cyan);
    }}
    .chud-wrap {{
      display: grid;
      gap: 8px;
      align-items: center;
      justify-items: center;
      text-align: center;
      padding: 10px;
    }}
    .chud-wrap img {{
      width: 100%;
      max-width: 260px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,0.12);
      box-shadow: 0 10px 30px rgba(0,0,0,0.4);
    }}
    .footnote {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
      margin-top: 10px;
    }}
    @media (max-width: 960px) {{
      .cards {{
        grid-template-columns: 1fr;
      }}
      main {{
        padding: 30px 16px 60px;
      }}
    }}
  </style>
</head>
<body class="{busy_class}">
  <video id="bg-video" autoplay muted loop playsinline src="/pentagon-pizza/video"></video>
  <div class="overlay"></div>
  <main>
    <header>
      <div class="status">{busy_label}</div>
      <h1>Pentagon Pizza Index</h1>
      <p>{stats_line} — last update {html.escape(timestamp)}</p>
    </header>
    <div class="cards">
      <div class="card">
        <img class="graph" src="data:image/png;base64,{graph_b64}" alt="Pentagon pizza history graph">
      </div>
      <div class="card">
        <div class="stats">
          <div class="stat">
            <div class="label">Current index</div>
            <div class="value">{latest_index:.2f}</div>
          </div>
          <div class="stat">
            <div class="label">Average index</div>
            <div class="value">{avg_index:.2f}</div>
          </div>
          <div class="stat">
            <div class="label">Peak index</div>
            <div class="value">{max_index:.2f}</div>
          </div>
        </div>
        <p>Threshold for super busy: {pizza_index.SUPER_BUSY_THRESHOLD:.2f}</p>
        <p>History samples: {history_count}</p>
        <p class="footnote">Index math runs through a Fortran helper with a Python fallback so it survives wherever this bot runs.</p>
        <div class="chud-wrap">
          <img src="/pentagon-pizza/static/chud.png" alt="chud.png">
          <div class="label">Required chud witness</div>
        </div>
      </div>
    </div>
  </main>
  <script>
    const body = document.body;
    const isSuperBusy = body.classList.contains('super-busy');
    const vid = document.getElementById('bg-video');
    if (!isSuperBusy && vid) {{
      vid.pause();
    }} else if (vid) {{
      vid.play().catch(() => {{}});
    }}
  </script>
</body>
</html>"""


async def handle_pentagon_pizza_page(request: web.Request):
    history = pizza_index.load_history()
    summary = pizza_index.summarize(history)
    graph_b64 = pizza_index.render_history_plot(history)
    html_body = _render_pentagon_pizza_page(graph_b64, summary, len(history))
    return web.Response(text=html_body, content_type="text/html")


async def handle_pentagon_pizza_data(request: web.Request):
    history = pizza_index.load_history()
    summary = pizza_index.summarize(history)
    return web.json_response(
        {
            "history": history,
            "summary": summary,
            "threshold": pizza_index.SUPER_BUSY_THRESHOLD,
            "superBusy": bool(summary.get("super_busy")),
        }
    )


async def handle_pentagon_pizza_observe(request: web.Request):
    if CONTROL_API_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {CONTROL_API_TOKEN}":
            return web.json_response({"message": "Unauthorized."}, status=401)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"message": "Invalid JSON payload."}, status=400)
    busy = payload.get("busy") or payload.get("busy_count") or payload.get("busyCount")
    capacity = payload.get("capacity") or payload.get("total_places") or payload.get("totalPlaces") or 12
    if busy is None:
        return web.json_response({"message": "busy is required."}, status=400)
    try:
        busy_int = int(busy)
        capacity_int = int(capacity)
    except (TypeError, ValueError):
        return web.json_response({"message": "busy and capacity must be numbers."}, status=400)
    entry = pizza_index.record_observation(busy_int, capacity_int, source=payload.get("source"))
    summary = pizza_index.summarize()
    return web.json_response(
        {
            "message": "Recorded.",
            "entry": entry,
            "summary": summary,
        }
    )


async def handle_pentagon_pizza_video(request: web.Request):
    if not PENTAGON_PIZZA_VIDEO.exists():
        return web.Response(status=404, text="Video not found.")
    return web.FileResponse(path=PENTAGON_PIZZA_VIDEO)


control_app.add_routes(
    [
        web.get("/pentagon-pizza", handle_pentagon_pizza_page),
        web.get("/pentagon-pizza/data", handle_pentagon_pizza_data),
        web.post("/pentagon-pizza/observe", handle_pentagon_pizza_observe),
        web.get("/pentagon-pizza/video", handle_pentagon_pizza_video),
    ]
)


MUSIC_COOKIES_PATH = os.path.join(str(REPO_ROOT), "data", "yt_cookies.txt")

_MUSIC_PORTAL_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Music Portal</title>
<style>
:root{--bg:#0b1022;--panel:rgba(12,21,44,0.85);--accent:#7289da;--ink:#e8f0ff;--muted:#8fa3bf;--cyan:#5de4c7;--red:#ed4245;--green:#57f287;}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"JetBrains Mono","Fira Code",Menlo,monospace;background:#0b1022;color:var(--ink);min-height:100vh;padding:24px 16px 60px;}
h2{font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;}
.card{background:var(--panel);border:1px solid rgba(255,255,255,0.06);border-radius:14px;padding:18px;backdrop-filter:blur(12px);margin-bottom:16px;}
.now{font-size:18px;font-weight:700;color:var(--cyan);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.sub{font-size:12px;color:var(--muted);margin-top:4px;}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;}
button{background:rgba(114,137,218,0.15);border:1px solid rgba(114,137,218,0.4);color:var(--ink);border-radius:8px;padding:8px 16px;cursor:pointer;font-family:inherit;font-size:13px;transition:background 0.15s;}
button:hover{background:rgba(114,137,218,0.35);}
button.danger{background:rgba(237,66,69,0.15);border-color:rgba(237,66,69,0.4);}
button.danger:hover{background:rgba(237,66,69,0.35);}
input[type=text],textarea{width:100%;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--ink);padding:8px 12px;font-family:inherit;font-size:13px;outline:none;}
input[type=text]:focus,textarea:focus{border-color:var(--accent);}
.play-row{display:flex;gap:8px;margin-bottom:12px;}
.play-row input{flex:1;}
input[type=range]{width:100%;accent-color:var(--accent);}
.vol-row{display:flex;align-items:center;gap:10px;margin-top:12px;}
.vol-row span{font-size:13px;color:var(--muted);white-space:nowrap;}
#vol-val{color:var(--cyan);}
.queue-list{list-style:none;display:grid;gap:6px;max-height:220px;overflow-y:auto;}
.queue-list li{background:rgba(255,255,255,0.04);border-radius:8px;padding:8px 12px;font-size:13px;display:flex;justify-content:space-between;align-items:center;}
.queue-list li span.idx{color:var(--muted);margin-right:8px;}
.toast{position:fixed;bottom:24px;right:24px;background:#23272a;border:1px solid rgba(255,255,255,0.1);border-radius:10px;padding:10px 18px;font-size:13px;opacity:0;transition:opacity 0.3s;pointer-events:none;z-index:100;}
.toast.show{opacity:1;}
#status-dot{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block;margin-right:6px;}
#status-dot.on{background:var(--green);}
textarea{height:120px;resize:vertical;}
</style>
</head>
<body>
<div style="max-width:760px;margin:0 auto;">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:20px;">
    <span id="status-dot"></span>
    <h1 style="font-size:20px;font-weight:700;">Music Portal</h1>
    <span id="guild-name" style="color:var(--muted);font-size:13px;margin-left:auto;"></span>
  </div>

  <div class="card">
    <h2>Now Playing</h2>
    <div id="now-title" class="now">—</div>
    <div id="now-sub" class="sub"></div>
    <div class="btns">
      <button onclick="ctrl('pause')">⏸ Pause</button>
      <button onclick="ctrl('resume')">▶ Resume</button>
      <button onclick="ctrl('skip')">⏭ Skip</button>
      <button onclick="ctrl('stop')" class="danger">⏹ Stop &amp; Leave</button>
    </div>
    <div class="vol-row">
      <span>Volume: <span id="vol-val">100</span>%</span>
      <input type="range" id="vol-slider" min="0" max="150" value="100" oninput="onVol(this.value)">
    </div>
  </div>

  <div class="card">
    <h2>Play a Song</h2>
    <div class="play-row">
      <input type="text" id="query" placeholder="URL or search terms…" onkeydown="if(event.key==='Enter')play()">
      <button onclick="play()">▶ Play</button>
    </div>
  </div>

  <div class="card">
    <h2>Queue (<span id="queue-count">0</span>)</h2>
    <ul class="queue-list" id="queue-list"><li style="color:var(--muted);">Empty</li></ul>
  </div>

  <div class="card">
    <h2>YouTube Cookies</h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:10px;">Paste a Netscape-format cookies.txt here to fix bot detection errors.</p>
    <textarea id="cookie-txt" placeholder="# Netscape HTTP Cookie File&#10;.youtube.com TRUE / FALSE 0 ..."></textarea>
    <div class="btns" style="margin-top:10px;">
      <button onclick="uploadCookies()">💾 Save Cookies</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const TOKEN = localStorage.getItem('mp_token') || '';

function authHeaders(extra){
  const h = {'Content-Type':'application/json',...(extra||{})};
  if(TOKEN) h['Authorization']='Bearer '+TOKEN;
  return h;
}

function toast(msg, err){
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  el.style.borderColor = err ? 'rgba(237,66,69,0.5)' : 'rgba(87,242,135,0.4)';
  clearTimeout(el._t);
  el._t = setTimeout(()=>el.classList.remove('show'), 2800);
}

async function api(path, method='GET', body){
  try {
    const r = await fetch(path, {method, headers:authHeaders(), body: body ? JSON.stringify(body) : undefined});
    return await r.json();
  } catch(e){ toast(e.message, true); return null; }
}

async function ctrl(action, extra={}){
  const d = await api('/music/control','POST',{action,...extra});
  if(d) toast(d.message || action, d.error);
}

async function play(){
  const q = document.getElementById('query').value.trim();
  if(!q) return;
  document.getElementById('query').value='';
  toast('Queueing…');
  const d = await api('/music/play','POST',{query:q});
  if(d) toast(d.message||'Queued', d.error);
}

let volTimer;
function onVol(v){
  document.getElementById('vol-val').textContent=v;
  clearTimeout(volTimer);
  volTimer=setTimeout(()=>ctrl('volume',{value:parseInt(v)}),400);
}

async function uploadCookies(){
  const txt = document.getElementById('cookie-txt').value.trim();
  if(!txt){toast('Nothing to save',true);return;}
  const r = await fetch('/music/cookies',{method:'POST',headers:{'Content-Type':'text/plain',...(TOKEN?{'Authorization':'Bearer '+TOKEN}:{})},body:txt});
  const d = await r.json().catch(()=>({}));
  toast(d.message||'Saved', d.error);
}

function updateStatus(s){
  const dot = document.getElementById('status-dot');
  dot.className = s.playing ? 'on' : '';
  document.getElementById('now-title').textContent = s.now ? s.now.title : '—';
  document.getElementById('now-sub').textContent = s.now ? (s.now.page_url||'') : (s.playing ? 'Playing' : 'Nothing playing');
  if(s.guild) document.getElementById('guild-name').textContent = s.guild;
  const vol = s.volume != null ? Math.round(s.volume*100) : null;
  if(vol != null && document.activeElement !== document.getElementById('vol-slider')){
    document.getElementById('vol-slider').value=Math.min(150,vol);
    document.getElementById('vol-val').textContent=Math.min(150,vol);
  }
  const ql = document.getElementById('queue-list');
  document.getElementById('queue-count').textContent=s.queue.length;
  if(!s.queue.length){
    ql.innerHTML='<li style="color:var(--muted);">Empty</li>';
  } else {
    ql.innerHTML=s.queue.map((t,i)=>`<li><span class="idx">${i+1}.</span>${t.title||t}</li>`).join('');
  }
}

async function poll(){
  const d = await api('/music/status');
  if(d && !d.error) updateStatus(d);
}

// On load, check for token prompt
if(!TOKEN){
  const t = prompt('API token (leave blank if none):');
  if(t) localStorage.setItem('mp_token', t);
  location.reload();
}
poll();
setInterval(poll, 2000);
</script>
</body>
</html>"""


def _music_auth(request: web.Request) -> bool:
    if not CONTROL_API_TOKEN:
        return True
    return request.headers.get("Authorization", "") == f"Bearer {CONTROL_API_TOKEN}"


async def handle_music_page(request: web.Request):
    return web.Response(text=_MUSIC_PORTAL_HTML, content_type="text/html")


async def handle_music_status(request: web.Request):
    if not _music_auth(request):
        return web.json_response({"error": True, "message": "Unauthorized."}, status=401)
    cog = bot.cogs.get("Music")
    guild_id = int(os.getenv("HOME_GUILD_ID", os.getenv("GUILD_ID", "0")))
    guild = bot.get_guild(guild_id) if guild_id else None
    if not cog or not guild:
        return web.json_response({"playing": False, "now": None, "queue": [], "volume": 1.0, "guild": None})
    state = cog._state(guild_id)
    vc = guild.voice_client
    now = state.now
    return web.json_response({
        "playing": bool(vc and vc.is_playing()),
        "paused": bool(vc and vc.is_paused()),
        "guild": guild.name,
        "volume": state.volume,
        "now": {"title": now.title, "page_url": now.page_url, "duration": now.duration} if now else None,
        "queue": [{"title": s.title, "page_url": s.page_url} for s in list(state.queue)],
    })


async def handle_music_play(request: web.Request):
    if not _music_auth(request):
        return web.json_response({"error": True, "message": "Unauthorized."}, status=401)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": True, "message": "Invalid JSON."}, status=400)
    query = (payload.get("query") or "").strip()
    if not query:
        return web.json_response({"error": True, "message": "query is required."}, status=400)
    cog = bot.cogs.get("Music")
    guild_id = int(os.getenv("HOME_GUILD_ID", os.getenv("GUILD_ID", "0")))
    guild = bot.get_guild(guild_id) if guild_id else None
    if not cog or not guild:
        return web.json_response({"error": True, "message": "Music cog or guild not available."}, status=503)
    try:
        song = await cog._extract(query)
    except Exception as exc:
        return web.json_response({"error": True, "message": str(exc)}, status=400)
    state = cog._state(guild_id)
    state.queue.append(song)
    if (not state.player_task or state.player_task.done()) and guild.voice_client:
        state.player_task = asyncio.create_task(cog._run_player(guild))
    return web.json_response({"message": f"Queued: {song.title}"})


async def handle_music_control(request: web.Request):
    if not _music_auth(request):
        return web.json_response({"error": True, "message": "Unauthorized."}, status=401)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": True, "message": "Invalid JSON."}, status=400)
    action = (payload.get("action") or "").strip().lower()
    guild_id = int(os.getenv("HOME_GUILD_ID", os.getenv("GUILD_ID", "0")))
    guild = bot.get_guild(guild_id) if guild_id else None
    if not guild:
        return web.json_response({"error": True, "message": "Guild not found."}, status=404)
    vc = guild.voice_client
    cog = bot.cogs.get("Music")
    state = cog._state(guild_id) if cog else None

    if action == "pause":
        if vc and vc.is_playing():
            vc.pause()
            return web.json_response({"message": "Paused."})
        return web.json_response({"message": "Nothing playing."})
    elif action == "resume":
        if vc and vc.is_paused():
            vc.resume()
            return web.json_response({"message": "Resumed."})
        return web.json_response({"message": "Not paused."})
    elif action == "skip":
        if vc and vc.is_playing() and state:
            state.skip_requested = True
            vc.stop()
            return web.json_response({"message": "Skipped."})
        return web.json_response({"message": "Nothing playing."})
    elif action == "stop":
        if state:
            state.queue.clear()
            state.now = None
        if vc:
            vc.stop()
            await vc.disconnect(force=True)
        return web.json_response({"message": "Stopped and left."})
    elif action == "volume":
        val = payload.get("value")
        if val is None:
            return web.json_response({"error": True, "message": "value required."}, status=400)
        pct = max(0, min(150, int(val)))
        if state:
            state.volume = pct / 100.0
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = pct / 100.0
        return web.json_response({"message": f"Volume set to {pct}%."})
    return web.json_response({"error": True, "message": f"Unknown action: {action}"}, status=400)


async def handle_music_cookies(request: web.Request):
    if not _music_auth(request):
        return web.json_response({"error": True, "message": "Unauthorized."}, status=401)
    try:
        body = await request.text()
    except Exception:
        return web.json_response({"error": True, "message": "Failed to read body."}, status=400)
    if not body.strip():
        return web.json_response({"error": True, "message": "Empty cookies."}, status=400)
    cookies_path = Path(MUSIC_COOKIES_PATH)
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cookies_path.write_text(body, encoding="utf-8")
    except OSError as exc:
        return web.json_response({"error": True, "message": str(exc)}, status=500)
    return web.json_response({"message": f"Cookies saved ({len(body)} bytes). Active on next play."})


control_app.add_routes([
    web.get("/music", handle_music_page),
    web.get("/music/status", handle_music_status),
    web.post("/music/play", handle_music_play),
    web.post("/music/control", handle_music_control),
    web.post("/music/cookies", handle_music_cookies),
])


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
    await bot.load_extension("commands.member_metrics")
    await bot.load_extension("commands.regexsearch")
    await bot.load_extension("commands.xp")
    await bot.load_extension("commands.role_menu")
    await bot.load_extension("commands.scheduler")
    await bot.load_extension("commands.drill")
    await bot.load_extension("commands.deadweight")
    await bot.load_extension("commands.dblookup")
    
    

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

                 
    bot.add_command(general.reload_responses)
    bot.add_command(general.list_responses)
    general.setup(bot.tree)
    await load_extensions()
    bot.tree.add_command(signal_command)
    bot.tree.add_command(blackbird)
    await bot.tree.sync()

                    
    r.load_responses()
    general.load_out_of_office()
                                
    bot.add_view(ApplicationView())
    bot.add_view(TicketCloseView())
    bot.add_view(CaptchaView())

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
async def on_member_join(member: discord.Member):
    embed = discord.Embed(
        title="Welcome to TMHNET",
        description=(
            "Before you can submit an application you must complete a quick human verification.\n\n"
            "Click the **Verify** button below and answer the simple math question to unlock access."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="This helps us keep the community secure.")
    try:
        dm = await member.create_dm()
        await dm.send(embed=embed, view=CaptchaView())
    except discord.Forbidden:
        pass                                                    


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
            response_id = entry.get("id")
            if response_id:
                role_id, role_name = _top_role_info(getattr(message, "author", None))
                usage_log.log_response_usage(response_id, message.guild.id, role_id, role_name)
                sunset = usage_log.get_active_sunset("response", str(response_id))
                if sunset:
                    usage_log.log_sunset_usage(int(sunset["id"]), role_id, role_name)
            break

    await bot.process_commands(message)
if __name__ == "__main__":
    import threading

    if token:
        bot.run(token)
    else:
        print("Bot token not found in .env file.")
