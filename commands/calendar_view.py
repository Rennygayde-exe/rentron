"""Google Calendar view: /calendar shows the current month's events.

Reads a PRIVATE calendar via a service account, so there is no OAuth flow and
no browser needed on the host. Share the calendar with the service account's
email address and it can read it.

Only `google-auth` is required, not google-api-python-client: that library is
synchronous and heavy, and all we need is a bearer token. The token is minted
in a worker thread (blocking crypto) and the actual request goes out over
aiohttp, so the event loop is never blocked.

NOTE the import of google.auth is guarded. A cog that raises ImportError at
load time takes down load_extensions(), which runs inside on_ready() and would
silently skip every step after it - persistent views, the control API, command
sync. That failure mode has bitten this bot before. If the dependency is
missing, /calendar explains how to fix it instead.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import discord
from discord import Interaction, app_commands
from discord.ext import commands
from dotenv import load_dotenv

try:
    from google.oauth2 import service_account as _google_sa
    from google.auth.transport.requests import Request as _GoogleRequest
    GOOGLE_AVAILABLE = True
except ImportError:
    _google_sa = None
    _GoogleRequest = None
    GOOGLE_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger(__name__)
SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or str(BASE_DIR / "data" / "google_service_account.json")
def _calendar_ids() -> list[str]:
    """Calendars to merge. GOOGLE_CALENDAR_IDS takes a comma separated list;
    GOOGLE_CALENDAR_ID stays supported so an existing single-calendar setup
    keeps working."""
    raw = os.getenv("GOOGLE_CALENDAR_IDS") or os.getenv("GOOGLE_CALENDAR_ID") or ""
    return [c.strip() for c in raw.replace("\n", ",").split(",") if c.strip()]


CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")
TIMEZONE_NAME = os.getenv("CALENDAR_TIMEZONE", "America/Los_Angeles")
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
API = "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"

_token: str | None = None
_token_expiry: datetime | None = None


def _local_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TIMEZONE_NAME)
    except Exception:
        return timezone.utc


def configuration_problem() -> str | None:
    """Return a human explanation of why the calendar cannot be read, or None."""
    if not GOOGLE_AVAILABLE:
        return ("The `google-auth` library isn't installed on the server yet.\n"
                "An admin needs to run:\n"
                "```\nsudo /home/ren/rentron/rentron/source/bin/pip install google-auth\n```")
    if not _calendar_ids():
        return ("No calendar is configured. An admin needs to set `GOOGLE_CALENDAR_IDS` "
                "in the bot's `.env` (Calendar Settings, Integrate calendar, Calendar ID). "
                "Separate several with commas to merge them.")
    if not os.path.exists(SA_JSON):
        return (f"The service account key file is missing (`{SA_JSON}`).\n"
                "An admin needs to place the JSON key there and set "
                "`GOOGLE_SERVICE_ACCOUNT_JSON` if it lives elsewhere.")
    return None


def _mint_token() -> tuple[str, datetime]:
    """Blocking. Signs a JWT and exchanges it for an access token."""
    creds = _google_sa.Credentials.from_service_account_file(SA_JSON, scopes=SCOPES)
    creds.refresh(_GoogleRequest())
    expiry = creds.expiry
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return creds.token, (expiry or datetime.now(timezone.utc) + timedelta(minutes=30))


async def get_token() -> str:
    """Cached access token, refreshed a minute before it lapses."""
    global _token, _token_expiry
    now = datetime.now(timezone.utc)
    if _token and _token_expiry and now < _token_expiry - timedelta(seconds=60):
        return _token
    _token, _token_expiry = await asyncio.to_thread(_mint_token)
    return _token


async def fetch_all(start: datetime, end: datetime, limit: int = 50) -> tuple[list[dict], list[str]]:
    """Fetch every configured calendar concurrently and merge them.

    Returns (events, errors). One calendar failing - almost always because it
    was not shared with the service account - must not blank out the others,
    so failures are collected and reported alongside whatever did load.
    """
    ids = _calendar_ids()
    results = await asyncio.gather(
        *(fetch_events(cid, start, end, limit) for cid in ids), return_exceptions=True)

    events, errors = [], []
    for cid, res in zip(ids, results):
        if isinstance(res, Exception):
            short = cid.split("@")[0][:28]
            errors.append(f"`{short}`: {res}")
            continue
        cal_name, items = res
        for ev in items:
            ev["_calendar"] = cal_name or cid.split("@")[0]
        events.extend(items)

    tz = _local_tz()
    events.sort(key=lambda e: _parse_start(e, tz)[0])
    return events, errors


async def fetch_events(calendar_id: str, start: datetime, end: datetime,
                       limit: int = 50) -> tuple[str, list[dict]]:
    token = await get_token()
    params = {
        "timeMin": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "timeMax": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": str(limit),
    }
    url = API.format(cal=calendar_id.replace("/", "%2F"))
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params,
                               headers={"Authorization": f"Bearer {token}"},
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 404:
                raise RuntimeError("not found, check the calendar ID")
            if resp.status in (401, 403):
                raise RuntimeError("not shared with the service account")
            if resp.status != 200:
                raise RuntimeError(f"Google returned HTTP {resp.status}")
            data = await resp.json()
    return data.get("summary", ""), data.get("items", [])


def _parse_start(event: dict, tz) -> tuple[datetime, bool]:
    """Returns (local_start, is_all_day). All day events carry `date`, not `dateTime`."""
    start = event.get("start", {})
    if "dateTime" in start:
        dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        return dt.astimezone(tz), False
    d = datetime.fromisoformat(start["date"])
    return d.replace(tzinfo=tz), True


def build_table(events: list[dict], tz) -> str:
    rows = []
    for ev in events:
        when_dt, all_day = _parse_start(ev, tz)
        when = f"{when_dt:%a %d %b}" + ("  all day" if all_day else f" {when_dt:%H:%M}")
        title = (ev.get("summary") or "(no title)").strip()
        where = (ev.get("location") or "").strip()
        cal = (ev.get("_calendar") or "").strip()
        rows.append((when, title, where, cal))

    if not rows:
        return ""
    has_where = any(r[2] for r in rows)
    has_cal = len({r[3] for r in rows if r[3]}) > 1

    w_when = max(len("WHEN"), max(len(r[0]) for r in rows))
    w_title = min(30, max(len("EVENT"), max(len(r[1]) for r in rows)))
    w_where = min(20, max(len("WHERE"), max((len(r[2]) for r in rows), default=0))) if has_where else 0
    w_cal = min(16, max(len("CALENDAR"), max((len(r[3]) for r in rows), default=0))) if has_cal else 0

    header = f"{'WHEN':<{w_when}}  {'EVENT':<{w_title}}"
    if has_where:
        header += f"  {'WHERE':<{w_where}}"
    if has_cal:
        header += f"  {'CALENDAR':<{w_cal}}"
    out = [header, "=" * len(header)]
    for when, title, where, cal in rows:
        line = f"{when:<{w_when}}  {title[:w_title]:<{w_title}}"
        if has_where:
            line += f"  {where[:w_where]:<{w_where}}"
        if has_cal:
            line += f"  {cal[:w_cal]}"
        out.append(line.rstrip())
    return "\n".join(out)


class CalendarView(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="calendar", description="Show this month's events")
    @app_commands.describe(month="0 for this month, 1 for next month, and so on")
    async def calendar(self, interaction: Interaction,
                       month: app_commands.Range[int, 0, 12] = 0):
        await interaction.response.defer()

        problem = configuration_problem()
        if problem:
            await interaction.followup.send(
                f"**The calendar isn't set up yet.**\n{problem}", ephemeral=True)
            return

        tz = _local_tz()
        now = datetime.now(tz)
        year, mon = now.year, now.month + month
        year += (mon - 1) // 12
        mon = (mon - 1) % 12 + 1
        start = datetime(year, mon, 1, tzinfo=tz)
        end = datetime(year + (mon // 12), (mon % 12) + 1, 1, tzinfo=tz)
        window_start = max(start, now) if month == 0 else start

        try:
            events, errors = await fetch_all(window_start, end)
        except Exception as exc:
            log.warning("calendar fetch failed: %s", exc)
            await interaction.followup.send(
                "**Couldn't reach Google Calendar.** Try again shortly.", ephemeral=True)
            return

        heading = f"{start:%B %Y}"
        problems = ("\n\n⚠️ Couldn't read: " + "; ".join(errors)) if errors else ""
        if not events:
            await interaction.followup.send(
                f"**{heading}**\nNothing on the calendar"
                + (" for the rest of this month." if month == 0 else " this month.")
                + problems)
            return

        table = build_table(events, tz)
        body = f"```\n{table}\n```"
        if len(body) > 4000:
            keep = table[:3900].rsplit("\n", 1)[0]
            body = f"```\n{keep}\n```\n*Truncated. {len(events)} events in total.*"

        embed = discord.Embed(
            title=f"📅 {heading}",
            description=body,
            color=discord.Color.blurple(),
        )
        n_cal = len({e.get("_calendar") for e in events if e.get("_calendar")})
        embed.set_footer(
            text=f"{len(events)} event{'' if len(events) == 1 else 's'}"
                 f"{f' across {n_cal} calendars' if n_cal > 1 else ''} · times in {TIMEZONE_NAME}")
        await interaction.followup.send(embed=embed, content=problems or None)


async def setup(bot: commands.Bot):
    await bot.add_cog(CalendarView(bot))
