import os, io, csv, json, random, sqlite3, asyncio, traceback, logging, re
from contextlib import contextmanager
from pathlib import Path
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")
DB_PATH = str(Path(
    os.getenv("APPLICATIONS_DB")
    or os.getenv("APPLICATIONS_DB_PATH")
    or (BASE_DIR / "applications.db")
))


@contextmanager
def _connect(path):
    con = sqlite3.connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()
STAFF_REVIEW_CHANNEL_ID = int(os.getenv("STAFF_REVIEW_CHANNEL_ID", "0"))
TICKET_LOG_CHANNEL_ID = int(os.getenv("TICKET_LOG_CHANNEL_ID", "0"))
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "0"))
VERIFIED_ROLE_NAME = os.getenv("VERIFIED_ROLE_NAME", "Verified")
VERIFIED_ROLE_ID = int(os.getenv("VERIFIED_ROLE_ID") or 0)
PENDING_ROLE_NAME = os.getenv("PENDING_ROLE_NAME", "Pending Application")
PENDING_ROLE_ID = int(os.getenv("PENDING_ROLE_ID", "0") or 0)
WELCOME_CHANNEL_ID = int(
    os.getenv("WELCOME_CHANNEL_ID")
    or os.getenv("XP_NOTIFICATION_CHANNEL_ID", "0")
    or 0
)
MOTD_FILE = Path(os.getenv("MOTD_FILE") or (BASE_DIR / "motd.md"))
APPLICATION_FOLLOWUP_CATEGORY_ID = int(os.getenv("APPLICATION_FOLLOWUP_CATEGORY_ID", "0"))
FOLLOWUP_MESSAGE_TEXT = (
    "Hello, just following up here, more information is needed to complete your application! "
    "If you need assistance, please use @Staff"
)
FOLLOWUP_DELAY = timedelta(days=2)

APPLICATION_ERROR_LOG = BASE_DIR / "application_errors.log"

TICKET_GREETING_TEXT = os.getenv(
    "TICKET_GREETING_TEXT",
    "{user}, a staff member will assist you shortly.",
)
TICKET_GREETINGS_FILE = Path(
    os.getenv("TICKET_GREETINGS_FILE") or (BASE_DIR / "data" / "ticket_greetings.json")
)
TICKET_GREETING_IMAGE = (os.getenv("TICKET_GREETING_IMAGE") or "").strip()
TICKET_GREETING_PHRASE = "a staff member will assist you shortly."
CHANNEL_TAG_RE = re.compile(r"#([\w\-]+)")

STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0") or 0)

FORM_VERSION = 2

NEW_ACCOUNT_FLAG_DAYS = int(os.getenv("NEW_ACCOUNT_FLAG_DAYS", "30") or 30)

_NO_INVITER_ANSWERS = {"none", "n/a", "na", "no one", "noone", "nobody", "-", "self", "myself"}


def is_staff(user: discord.abc.User | discord.Member | None) -> bool:
    """True if the user is a guild staff member.

    Accepts either the configured STAFF_ROLE_ID or a Manage Server /
    Administrator permission, so the gate keeps working if the role ID is unset.
    """
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and (perms.administrator or perms.manage_guild):
        return True
    if STAFF_ROLE_ID:
        roles = getattr(user, "roles", None) or []
        return any(getattr(role, "id", None) == STAFF_ROLE_ID for role in roles)
    return False


async def deny_non_staff(interaction: discord.Interaction) -> bool:
    """Reject a non-staff interaction. Returns True if the user may proceed."""
    if is_staff(interaction.user):
        return True
    try:
        await interaction.response.send_message(
            "Only staff can use this control.", ephemeral=True
        )
    except Exception:
        pass
    return False

def load_motd_text() -> str:
    try:
        content = MOTD_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception:
        logging.exception("Failed to read MOTD from %s", MOTD_FILE)
        return ""
    return content.strip()

def fetch_pending_application_entry(applicant_id: int, message_id: int | None = None):
    with _connect(DB_PATH) as con:
        row = None
        if message_id:
            row = con.execute(
                "SELECT message_id,user_id,data FROM pending_applications WHERE message_id=?",
                (int(message_id),),
            ).fetchone()
        if not row:
            row = con.execute(
                "SELECT message_id,user_id,data FROM pending_applications WHERE user_id=? ORDER BY message_id DESC LIMIT 1",
                (int(applicant_id),),
            ).fetchone()
    if not row:
        return None
    mid, uid, raw = row
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    return {"message_id": mid, "user_id": uid, "data": data}


def log_application_error(context: str, error: Exception):
    try:
        APPLICATION_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        with APPLICATION_ERROR_LOG.open("a", encoding="utf-8") as fp:
            fp.write(f"[{discord.utils.utcnow().isoformat()}] {context}: {error}\n")
            fp.write("".join(traceback.format_exception(type(error), error, error.__traceback__)))
            fp.write("\n")
    except Exception:
        pass


async def send_application_error(interaction: discord.Interaction, message: str):
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)
        else:
            await interaction.followup.send(message, ephemeral=True)
    except Exception:
        pass

def _render_channel_mentions(guild: discord.Guild | None, text: str) -> str:
    if guild is None or not text:
        return text

    def repl(match: re.Match) -> str:
        channel_name = match.group(1)
        channel = discord.utils.get(guild.channels, name=channel_name)
        if channel:
            return channel.mention
        return match.group(0)

    return CHANNEL_TAG_RE.sub(repl, text)

async def send_welcome_message(bot: commands.Bot, member: discord.Member):
    if not WELCOME_CHANNEL_ID:
        return
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(WELCOME_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None
    if channel is None:
        logging.warning("Welcome channel %s not found for MOTD broadcast.", WELCOME_CHANNEL_ID)
        return
    motd = load_motd_text()
    if not motd:
        motd = "Please welcome {member}!"
    raw_message = motd.replace("{member}", member.mention).replace("{name}", member.display_name)
    message = _render_channel_mentions(member.guild, raw_message)
    try:
        await channel.send(message)
    except (discord.Forbidden, discord.HTTPException):
        logging.warning("Failed to send welcome message for %s in channel %s", member.id, WELCOME_CHANNEL_ID)

def build_application_nickname(name: str, pronouns: str) -> str | None:
    name_clean = (name or "").strip()
    pronouns_clean = (pronouns or "").strip()
    if not name_clean:
        return None

    if pronouns_clean:
        candidate = f"{name_clean} ({pronouns_clean})"
        if len(candidate) <= 32:
            return candidate

        available = 32 - len(name_clean) - 3
        if available > 1:
            truncated_pronouns = pronouns_clean[:available].rstrip()
            if truncated_pronouns:
                candidate = f"{name_clean} ({truncated_pronouns})"
                if len(candidate) <= 32:
                    return candidate

    return name_clean[:32]


def init_db():
    with _connect(DB_PATH) as con:
        c = con.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS applications(
            user_id INTEGER PRIMARY KEY, submitted_at TEXT, status TEXT NOT NULL DEFAULT 'pending')""")
        c.execute("""CREATE TABLE IF NOT EXISTS pending_applications(
            message_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, data TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets(
            message_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS app_sessions(
            message_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, data TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS captcha_verified(
            user_id INTEGER PRIMARY KEY, verified_at TEXT NOT NULL)""")
        existing_cols = {row[1] for row in c.execute("PRAGMA table_info(applications)")}
        for column in ("decided_by", "decided_at", "decision_reason"):
            if column not in existing_cols:
                c.execute(f"ALTER TABLE applications ADD COLUMN {column} TEXT")
        con.commit()

def has_submitted(user_id:int)->bool:
    with _connect(DB_PATH) as con:
        return con.execute("SELECT 1 FROM applications WHERE user_id=?", (int(user_id),)).fetchone() is not None

BLOCKING_STATUSES = {"pending", "approved", "denied"}

def application_block_reason(user_id:int)->str|None:
    """Why this person cannot start a new application, or None if they can."""
    status = get_application_status(user_id)
    if status not in BLOCKING_STATUSES:
        return None
    if status == "pending":
        return ("You already have an application waiting on staff review. "
                "Someone will get to it as soon as they can.")
    if status == "approved":
        return ("Your application was already approved — you're in. If something looks "
                "wrong with your roles, ask a staff member.")
    return ("Your previous application was declined. If you would like it reconsidered, "
            "please reach out to a staff member.")

def get_application_status(user_id:int)->str|None:
    with _connect(DB_PATH) as con:
        row = con.execute("SELECT status FROM applications WHERE user_id=?", (int(user_id),)).fetchone()
    return row[0] if row else None

def get_application_record(user_id:int)->dict|None:
    with _connect(DB_PATH) as con:
        row = con.execute(
            "SELECT status, submitted_at, decided_by, decided_at, decision_reason "
            "FROM applications WHERE user_id=?", (int(user_id),)
        ).fetchone()
    if not row:
        return None
    return dict(zip(("status", "submitted_at", "decided_by", "decided_at", "decision_reason"), row))

def reset_application(user_id:int)->bool:
    """Clear a user's application so they can apply again. Returns True if a row was removed."""
    with _connect(DB_PATH) as con:
        cur = con.execute("DELETE FROM applications WHERE user_id=?", (int(user_id),))
        con.execute("DELETE FROM pending_applications WHERE user_id=?", (int(user_id),))
        con.execute("DELETE FROM app_sessions WHERE user_id=?", (int(user_id),))
        con.commit()
        return cur.rowcount > 0

def prune_stale_pending()->int:
    """Drop pending_applications rows whose application has already been decided.

    The startup backfill used to re-create a pending row for every review embed
    still carrying (disabled) buttons, so decided applications accumulated here
    and reappeared as live, clickable reviews after every restart.
    """
    with _connect(DB_PATH) as con:
        cur = con.execute(
            "DELETE FROM pending_applications WHERE user_id IN "
            "(SELECT user_id FROM applications WHERE status IN ('approved','denied','closed'))"
        )
        con.commit()
        return cur.rowcount

def mark_as_submitted(user_id:int, submitted_at:str):
    with _connect(DB_PATH) as con:
        con.execute("INSERT OR REPLACE INTO applications(user_id,submitted_at,status) VALUES(?,?,'pending')",
                    (int(user_id), submitted_at)); con.commit()

def is_captcha_verified(user_id: int) -> bool:
    with _connect(DB_PATH) as con:
        return con.execute(
            "SELECT 1 FROM captcha_verified WHERE user_id=?", (int(user_id),)
        ).fetchone() is not None

def mark_captcha_verified(user_id: int):
    with _connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO captcha_verified(user_id, verified_at) "
            "VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (int(user_id),),
        )
        con.commit()

def update_application_status(user_id:int, status:str, decided_by:str|None=None, reason:str|None=None):
    normalized = (status or 'pending').strip().lower()
    if normalized not in {'pending', 'approved', 'denied', 'closed'}:
        normalized = 'pending'
    with _connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO applications(user_id, submitted_at, status, decided_by, decided_at, decision_reason) "
            "VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?) "
            "ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, "
            "decided_by=excluded.decided_by, decided_at=excluded.decided_at, "
            "decision_reason=excluded.decision_reason",
            (int(user_id), normalized, decided_by, reason),
        );
        con.commit()

_ROLE_NAME_SANITIZE = re.compile(r"[^a-z0-9]+")

def _normalize_role_name(value: str | None) -> str:
    if not value:
        return ""
    return _ROLE_NAME_SANITIZE.sub("", value.lower())

_BRANCH_ROLE_ENV_MAP: dict[str, str] = {
    "army": "ARMY_ROLE_ID",
    "navy": "NAVY_ROLE_ID",
    "marines": "MARINES_ROLE_ID",
    "airforce": "AIR_FORCE_ROLE_ID",
    "coastguard": "COAST_GUARD_ROLE_ID",
    "spaceforce": "SPACE_FORCE_ROLE_ID",
    "family": "FAMILY_ROLE_ID",
}

def _load_branch_role_ids() -> dict[str, int]:
    mapping: dict[str, int] = {}
    for normalized, env_var in _BRANCH_ROLE_ENV_MAP.items():
        raw = os.getenv(env_var)
        if not raw:
            continue
        try:
            role_id = int(raw)
        except (TypeError, ValueError):
            continue
        if role_id > 0:
            mapping[normalized] = role_id
    return mapping

BRANCH_ROLE_IDS = _load_branch_role_ids()

def get_branch_role_id(branch: str) -> int | None:
    normalized = _normalize_role_name(branch)
    if not normalized:
        return None
    return BRANCH_ROLE_IDS.get(normalized)

async def find_role(guild: discord.Guild, role_id: int | None = None, role_name: str | None = None) -> discord.Role | None:
    if guild is None:
        return None

    roles_cache = list(guild.roles)

    async def ensure_roles():
        nonlocal roles_cache
        try:
            roles_cache = await guild.fetch_roles()
        except (discord.Forbidden, discord.HTTPException):
            roles_cache = []

    if role_id:
        role = guild.get_role(role_id)
        if role:
            return role
        await ensure_roles()
        for candidate in roles_cache:
            if candidate.id == role_id:
                return candidate

    if role_name:
        target = _normalize_role_name(role_name)
        if target:
            if not roles_cache:
                await ensure_roles()
            prefix_matches = []
            for candidate in roles_cache:
                normalized = _normalize_role_name(candidate.name)
                if normalized == target:
                    return candidate
                if normalized.startswith(target):
                    prefix_matches.append(candidate)
            if len(prefix_matches) == 1:
                return prefix_matches[0]
            if len(prefix_matches) > 1:
                logging.warning(
                    "Ambiguous role name %r matches %s; refusing to guess. Set the role ID in .env.",
                    role_name,
                    [r.name for r in prefix_matches],
                )
    return None

async def resolve_inviter(guild: discord.Guild | None, raw: str | None):
    """Match a free-text "who invited you" answer against real guild members.

    Returns (status, member) where status is 'none' (applicant said nobody),
    'found', or 'missing'. Never blocks the application - it only annotates the
    staff embed so reviewers can see whether the referral checks out.
    """
    text = (raw or "").strip()
    if not text:
        return "none", None
    cleaned = text.lstrip("@").strip()
    if "#" in cleaned:
        cleaned = cleaned.split("#", 1)[0].strip()
    if cleaned.casefold() in _NO_INVITER_ANSWERS:
        return "none", None
    if guild is None or not cleaned:
        return "missing", None

    needle = cleaned.casefold()

    def matches(candidate) -> bool:
        for attr in ("name", "global_name", "display_name", "nick"):
            value = getattr(candidate, attr, None)
            if value and value.casefold() == needle:
                return True
        return False

    for candidate in guild.members:
        if matches(candidate):
            return "found", candidate
    try:
        results = await guild.query_members(query=cleaned[:32], limit=10)
    except (discord.HTTPException, AttributeError, asyncio.TimeoutError):
        results = []
    for candidate in results:
        if matches(candidate):
            return "found", candidate
    return "missing", None


def _format_age(moment) -> str:
    if moment is None:
        return "unknown"
    delta = discord.utils.utcnow() - moment
    if delta.days >= 365:
        return f"{delta.days // 365}y {delta.days % 365}d"
    if delta.days >= 1:
        return f"{delta.days}d"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours}h"
    return f"{max(delta.seconds // 60, 0)}m"


def build_application_embed(
    data: dict,
    applicant_id: int,
    *,
    user=None,
    member=None,
) -> discord.Embed:
    """Canonical staff-review embed, shared by the DM form and the control API."""
    embed = discord.Embed(title="New Application", color=discord.Color.blue())
    embed.add_field(name="Preferred Name", value=data.get("name") or "Unknown", inline=False)
    embed.add_field(name="Pronouns", value=data.get("pronouns") or "Unknown", inline=False)
    embed.add_field(name="Branch", value=data.get("branch_choice") or "Unknown", inline=False)
    embed.add_field(name="Status", value=data.get("status_choice") or "Unknown", inline=False)
    embed.add_field(name="Referral Source", value=(data.get("refer") or "Unknown")[:1024], inline=False)

    invited_raw = (data.get("invited_by") or "").strip()
    invite_status = data.get("invited_by_status") or ""
    if invited_raw or invite_status:
        if invite_status == "found":
            inviter_id = data.get("invited_by_id")
            label = f"<@{inviter_id}>" if inviter_id else "a current member"
            value = f"{invited_raw}\n✅ Matches {label}"
        elif invite_status == "none":
            value = f"{invited_raw or 'None'}\nℹ️ Applicant reported no inviter"
        else:
            value = f"{invited_raw or 'Unknown'}\n⚠️ No member matching this name found in server"
        embed.add_field(name="Invited By", value=value[:1024], inline=False)

    connection = (data.get("connection") or "").strip()
    if connection:
        embed.add_field(name="Community Connection", value=connection[:1024], inline=False)

    signals = []
    created = getattr(user, "created_at", None)
    if created:
        age_flag = " ⚠️ NEW" if (discord.utils.utcnow() - created).days < NEW_ACCOUNT_FLAG_DAYS else ""
        signals.append(f"Account age: {_format_age(created)} (created {created:%Y-%m-%d}){age_flag}")
    joined = getattr(member, "joined_at", None)
    if joined:
        signals.append(f"Joined server: {_format_age(joined)} ago")
    if user is not None and getattr(user, "avatar", None) is None:
        signals.append("Avatar: default (none set) ⚠️")
    if signals:
        embed.add_field(name="Account Signals", value="\n".join(signals), inline=False)
    return embed


_ANSWER_LABELS = (
    ("name", "Preferred name"),
    ("pronouns", "Pronouns"),
    ("branch_choice", "Branch"),
    ("status_choice", "Status"),
    ("refer", "Where they heard about us"),
    ("invited_by", "Who invited them"),
    ("connection", "Connection to the community"),
)

_INVITE_VERDICT = {
    "found": "verified - that member is in the server",
    "missing": "could not be matched to a current member",
    "none": "said nobody invited them",
}


def build_application_detail_embed(data: dict, applicant_id: int, *, user=None, member=None) -> discord.Embed:
    """The full record for one applicant, including what the review card omits."""
    embed = discord.Embed(title="Full application", color=discord.Color.blurple())

    answers = []
    for key, label in _ANSWER_LABELS:
        value = (data.get(key) or "").strip()
        if not value:
            continue
        if key == "invited_by":
            verdict = _INVITE_VERDICT.get(data.get("invited_by_status") or "")
            resolved = data.get("invited_by_resolved")
            if resolved and data.get("invited_by_status") == "found":
                value = f"{value} → {resolved}"
            if verdict:
                value = f"{value}\n*{verdict}*"
        answers.append(f"**{label}**\n{value}")
    if answers:
        embed.description = "\n\n".join(answers)[:4096]

    identity = [f"Mention: <@{applicant_id}>", f"User ID: `{applicant_id}`"]
    for key, label in (("discord_username", "Username"),
                       ("discord_global_name", "Display name"),
                       ("discord_display_name", "Server nickname")):
        if data.get(key):
            identity.append(f"{label}: {data[key]}")
    identity.append("Currently in server: " + ("yes" if member is not None else "**no**"))
    embed.add_field(name="Discord identity", value="\n".join(identity)[:1024], inline=False)

    signals = []
    created = getattr(user, "created_at", None)
    if created:
        flag = "  ⚠️ under 30 days" if (discord.utils.utcnow() - created).days < NEW_ACCOUNT_FLAG_DAYS else ""
        signals.append(f"Account created: {created:%Y-%m-%d} ({_format_age(created)} ago){flag}")
    joined = getattr(member, "joined_at", None)
    if joined:
        signals.append(f"Joined server: {joined:%Y-%m-%d} ({_format_age(joined)} ago)")
    if user is not None and getattr(user, "avatar", None) is None:
        signals.append("No profile picture set  ⚠️")
    if signals:
        embed.add_field(name="Account signals", value="\n".join(signals)[:1024], inline=False)

    record = get_application_record(applicant_id)
    if record:
        history = [f"Status: **{record.get('status') or 'unknown'}**"]
        if record.get("submitted_at"):
            history.append(f"Submitted: {str(record['submitted_at'])[:19].replace('T', ' ')}")
        if record.get("decided_by"):
            history.append(f"Decided by: {record['decided_by']}")
        if record.get("decided_at"):
            history.append(f"Decided: {str(record['decided_at'])[:19].replace('T', ' ')}")
        if record.get("decision_reason"):
            history.append(f"Reason: {record['decision_reason']}")
        embed.add_field(name="Record", value="\n".join(history)[:1024], inline=False)
    else:
        embed.add_field(
            name="Record",
            value="No application row on file - this entry was rebuilt from the review message.",
            inline=False,
        )
    return embed


async def retire_review_message(bot: commands.Bot, guild: discord.Guild | None, message_id: int | None):
    """Disable the buttons on a review card once its application is decided.

    Decisions can now come from three places - the card itself, the
    /list_pending table, and the control API - so the card is retired centrally
    instead of only where the click happened. Without this, a decided
    application keeps a live Approve button on a stale message.
    """
    if not message_id or not STAFF_REVIEW_CHANNEL_ID:
        return
    channel = guild.get_channel(STAFF_REVIEW_CHANNEL_ID) if guild else None
    if channel is None:
        channel = bot.get_channel(STAFF_REVIEW_CHANNEL_ID)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
        return
    if not message.components:
        return
    try:
        view = discord.ui.View.from_message(message)
        for child in view.children:
            child.disabled = True
        await message.edit(view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def process_application_decision(
    bot: commands.Bot,
    *,
    applicant_id: int,
    approved: bool,
    reviewer_name: str,
    reason: str = "",
    guild: discord.Guild | None = None,
    member: discord.Member | None = None,
    review_message_id: int | None = None,
    application_data: dict | None = None,
):
    resolved_data = dict(application_data or {})
    entry = None
    if not resolved_data:
        entry = fetch_pending_application_entry(applicant_id, review_message_id)
        if entry:
            resolved_data = entry.get("data") or {}
            if review_message_id is None:
                review_message_id = entry.get("message_id")
    else:
        entry = fetch_pending_application_entry(applicant_id, review_message_id)
        if entry and review_message_id is None:
            review_message_id = entry.get("message_id")

    guild_id_raw = resolved_data.get("guild_id")
    if not guild_id_raw and review_message_id:
        with _connect(DB_PATH) as con:
            row = con.execute("SELECT data FROM pending_applications WHERE message_id=?", (int(review_message_id),)).fetchone()
        if row:
            try:
                payload = json.loads(row[0])
                guild_id_raw = payload.get("guild_id")
                if not resolved_data:
                    resolved_data = payload
            except Exception:
                pass
    if not guild_id_raw:
        guild_id_raw = (
            os.getenv("HOME_GUILD_ID")
            or os.getenv("GUILD_ID")
            or os.getenv("DISCORD_HOME_GUILD_ID")
            or os.getenv("DISCORD_GUILD_ID")
            or "0"
        )
    try:
        guild_id = int(guild_id_raw)
    except (TypeError, ValueError):
        guild_id = 0

    guild_obj = guild
    if guild_obj is None and guild_id:
        guild_obj = bot.get_guild(guild_id)
    if guild_obj is None and guild_id:
        try:
            guild_obj = await bot.fetch_guild(guild_id)
        except discord.HTTPException:
            guild_obj = None
    if guild_obj is None:
        raise ValueError("Guild not found for application processing.")

    member_obj = member or guild_obj.get_member(applicant_id)
    if member_obj is None:
        logging.warning("Application decision: cache miss for user %s in guild %s", applicant_id, guild_obj.id)
        try:
            member_obj = await guild_obj.fetch_member(applicant_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member_obj = None
    if member_obj is None:
        try:
            logging.warning("Application decision: fetch_member failed for %s; attempting query_members", applicant_id)
            matches = await guild_obj.query_members(user_ids=[applicant_id], limit=1)
            if matches:
                member_obj = matches[0]
        except (discord.Forbidden, discord.HTTPException):
            member_obj = None
    if member_obj is None:
        logging.error(
            "Application decision: unable to locate user %s in guild %s (%s)",
            applicant_id,
            guild_obj.id,
            guild_obj.name,
        )
        raise ValueError("Applicant is not in the guild.")

    status_label = 'approved' if approved else 'denied'
    branch = (resolved_data.get("branch_choice") or resolved_data.get("branch") or "").strip()
    if approved and branch:
        branch_role_id = get_branch_role_id(branch)
        branch_role_name = branch.title()
        role = None
        if branch_role_id:
            role = await find_role(guild_obj, role_id=branch_role_id)
        if role is None:
            role = await find_role(guild_obj, role_name=branch_role_name)
        if role:
            try:
                await member_obj.add_roles(role)
            except (discord.Forbidden, discord.HTTPException):
                pass
    if approved:
        verified = await find_role(
            guild_obj,
            role_id=VERIFIED_ROLE_ID,
            role_name=VERIFIED_ROLE_NAME,
        )
        if verified:
            try:
                await member_obj.add_roles(verified)
            except (discord.Forbidden, discord.HTTPException):
                logging.warning(
                    "Failed to add verified role %s (%s) to %s",
                    verified.name,
                    verified.id,
                    member_obj.id,
                )
        else:
            logging.warning(
                "Verified role not found (id=%s, name=%s) in guild %s",
                VERIFIED_ROLE_ID,
                VERIFIED_ROLE_NAME,
                guild_obj.id,
            )
    pending_role = await find_role(
        guild_obj,
        role_id=PENDING_ROLE_ID,
        role_name=PENDING_ROLE_NAME,
    )
    if pending_role:
        try:
            await member_obj.remove_roles(pending_role)
        except (discord.Forbidden, discord.HTTPException):
            pass
    if approved:
        name = (resolved_data.get("name") or resolved_data.get("preferred_name") or "").strip()
        pronouns = (resolved_data.get("pronouns") or "").strip()
        nickname = build_application_nickname(name, pronouns)
        if nickname:
            try:
                await member_obj.edit(nick=nickname)
            except (discord.Forbidden, discord.HTTPException):
                pass

    delete_pending(user_id=member_obj.id, message_id=review_message_id)
    update_application_status(member_obj.id, status_label, decided_by=reviewer_name, reason=reason)
    await retire_review_message(bot, guild_obj, review_message_id)

    dm_text = f"Your application has been {status_label}."
    if reason:
        dm_text += f"\nReason: {reason}"
    try:
        await member_obj.send(dm_text)
    except Exception:
        pass

    if approved:
        try:
            await send_welcome_message(bot, member_obj)
        except Exception:
            logging.exception("Failed to send welcome message for %s", member_obj.id)

    log_channel = guild_obj.get_channel(TICKET_LOG_CHANNEL_ID)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Field", "Value"])
    for key, value in (resolved_data or {}).items():
        writer.writerow([key.replace('_', ' ').title(), value])
    writer.writerow(["Decision", "Approved" if approved else "Denied"])
    writer.writerow(["Reason", reason or "None provided"])
    writer.writerow(["Reviewed By", reviewer_name])
    writer.writerow(["Applicant", str(member_obj)])
    buf.seek(0)
    file = discord.File(io.BytesIO(buf.read().encode()), filename=f"{member_obj.id}_app_log.csv")
    log_text = f"Application {status_label} for {member_obj.mention} by {reviewer_name}."
    if reason:
        log_text += f" Reason: {reason}"
    if log_channel:
        try:
            await log_channel.send(log_text, file=file)
        except Exception:
            pass

    return {
        "user_id": member_obj.id,
        "status": status_label,
        "reviewer": reviewer_name,
        "reason": reason,
        "member": str(member_obj),
        "guild_id": guild_obj.id,
        "guild_name": guild_obj.name,
    }

def store_pending_application(message_id:int, user_id:int, data:dict):
    with _connect(DB_PATH) as con:
        con.execute("INSERT OR REPLACE INTO pending_applications(message_id,user_id,data) VALUES(?,?,?)",
                    (int(message_id), int(user_id), json.dumps(data))); con.commit()

def delete_ticket_record(channel_id: int):
    """Forget a closed ticket so startup stops trying to restore its buttons."""
    with _connect(DB_PATH) as con:
        con.execute("DELETE FROM tickets WHERE channel_id=?", (int(channel_id),))
        con.commit()

def record_ticket_message(channel_id: int, message_id: int):
    with _connect(DB_PATH) as con:
        con.execute("DELETE FROM tickets WHERE channel_id=?", (int(channel_id),))
        con.execute(
            "INSERT OR REPLACE INTO tickets(message_id,channel_id) VALUES(?,?)",
            (int(message_id), int(channel_id)),
        )
        con.commit()


def load_ticket_greetings() -> list[str]:
    """Every wording the bot may post when a ticket opens.

    Re-read on each call rather than cached at import, so staff can edit the
    pool and have the next ticket pick it up without a restart. Any problem
    reading it degrades to the single wording in TICKET_GREETING_TEXT - a
    malformed file must never stop a ticket from opening.
    """
    try:
        raw = json.loads(TICKET_GREETINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [TICKET_GREETING_TEXT]
    except (OSError, ValueError) as exc:
        logging.warning(
            "Could not read %s (%s); falling back to the single configured greeting.",
            TICKET_GREETINGS_FILE, exc,
        )
        return [TICKET_GREETING_TEXT]
    if isinstance(raw, dict):
        raw = raw.get("greetings", [])
    if not isinstance(raw, list):
        return [TICKET_GREETING_TEXT]
    variants = [g.strip() for g in raw if isinstance(g, str) and g.strip()]
    return variants or [TICKET_GREETING_TEXT]


def build_ticket_greeting(member: discord.Member | None = None):
    """Build the greeting posted when a ticket channel is created.

    The wording is picked at random from the pool so consecutive tickets do not
    read identically. Returns (content, embed, file) ready to splat into
    `channel.send`. The picture rides in an embed so it renders large and inline
    under the text: a URL is used as-is, a local file is uploaded alongside and
    referenced with attachment://. Both are None when no picture is configured,
    which `send` accepts, so callers need no branching.
    """
    mention = member.mention if member else "there"
    content = random.choice(load_ticket_greetings()).replace("{user}", mention)
    if not TICKET_GREETING_IMAGE:
        return content, None, None

    embed = discord.Embed(color=discord.Color.blurple())
    if TICKET_GREETING_IMAGE.lower().startswith(("http://", "https://")):
        embed.set_image(url=TICKET_GREETING_IMAGE)
        return content, embed, None

    path = Path(TICKET_GREETING_IMAGE)
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.is_file():
        logging.warning(
            "TICKET_GREETING_IMAGE %s not found; posting the ticket greeting without a picture.",
            path,
        )
        return content, None, None

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", path.name) or "greeting.png"
    embed.set_image(url=f"attachment://{safe_name}")
    return content, embed, discord.File(path, filename=safe_name)


def ticket_greeting_markers() -> list[str]:
    """Lowercased snippets that identify a greeting posted by an earlier boot.

    Used on startup to re-attach the Close button when a ticket's stored message
    id has been lost. Covers every wording in the pool, plus the original
    phrasing so tickets opened before the text was customised still match.
    Call once and reuse - it reads the greetings file.
    """
    markers = [TICKET_GREETING_PHRASE]
    for text in load_ticket_greetings():
        marker = max(text.lower().split("{user}"), key=len).strip()
        if len(marker) >= 12 and marker not in markers:
            markers.append(marker)
    return markers

ID_FOOTER_PAREN_RE = re.compile(r"\((\d{15,25})\)")
ID_SNOWFLAKE_RE = re.compile(r"(?<!\d)(\d{15,25})(?!\d)")


def extract_applicant_id(source: str) -> int | None:
    """Recover a Discord user ID from an embed footer or author line."""
    if not source:
        return None
    match = ID_FOOTER_PAREN_RE.search(source) or ID_SNOWFLAKE_RE.search(source)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None

def parse_application_embed(embed: discord.Embed | None) -> tuple[int | None, dict]:
    """Attempt to reconstruct application data from an embed."""
    if embed is None:
        return None, {}
    data: dict[str, str] = {}
    field_map = {
        "preferred name": ("name", "preferred_name"),
        "pronouns": ("pronouns",),
        "branch": ("branch_choice", "branch"),
        "status": ("status_choice", "status"),
        "referral source": ("refer", "referral_source"),
        "invited by": ("invited_by",),
        "community connection": ("connection",),
    }
    skip_fields = {"account signals"}
    for field in embed.fields:
        field_name = (field.name or "").strip().lower()
        if not field_name or field_name in skip_fields:
            continue
        value = field.value or ""
        if field_name == "invited by":
            value = value.split("\n", 1)[0].strip()
        targets = field_map.get(field_name) or (field_name.replace(" ", "_"),)
        for target in targets:
            data[target] = value
    applicant_id: int | None = None
    footer_text = (embed.footer.text or "").strip() if embed.footer else ""
    author_text = (embed.author.name or "").strip() if embed.author else ""
    for source in (footer_text, author_text):
        applicant_id = extract_applicant_id(source)
        if applicant_id:
            break
    if footer_text and "(" in footer_text and ")" in footer_text:
        tag = footer_text.rsplit("(", 1)[0].strip()
        if tag:
            data.setdefault("discord_tag", tag)
    return applicant_id, data

def delete_pending(*, user_id:int|None=None, message_id:int|None=None):
    with _connect(DB_PATH) as con:
        if user_id: con.execute("DELETE FROM pending_applications WHERE user_id=?", (int(user_id),))
        if message_id: con.execute("DELETE FROM pending_applications WHERE message_id=?", (int(message_id),))
        con.commit()

def session_get(mid:int)->dict|None:
    with _connect(DB_PATH) as con:
        r = con.execute("SELECT data FROM app_sessions WHERE message_id=?", (int(mid),)).fetchone()
        return json.loads(r[0]) if r else None

def session_set(mid:int, uid:int, data:dict):
    with _connect(DB_PATH) as con:
        con.execute("INSERT OR REPLACE INTO app_sessions(message_id,user_id,data) VALUES(?,?,?)",
                    (int(mid), int(uid), json.dumps(data))); con.commit()

def session_del(mid:int):
    with _connect(DB_PATH) as con:
        con.execute("DELETE FROM app_sessions WHERE message_id=?", (int(mid),)); con.commit()

def session_owner(mid:int)->int|None:
    with _connect(DB_PATH) as con:
        r = con.execute("SELECT user_id FROM app_sessions WHERE message_id=?", (int(mid),)).fetchone()
        return int(r[0]) if r else None

def clear_sessions_for_user(user_id:int, keep_message_id:int|None=None):
    with _connect(DB_PATH) as con:
        if keep_message_id:
            con.execute("DELETE FROM app_sessions WHERE user_id=? AND message_id<>?",
                        (int(user_id), int(keep_message_id)))
        else:
            con.execute("DELETE FROM app_sessions WHERE user_id=?", (int(user_id),))
        con.commit()

APPLY_COOLDOWN_SECONDS = float(os.getenv("APPLY_COOLDOWN_SECONDS", "60") or 60)
_apply_cooldowns: dict[int, float] = {}

VERIFY_COOLDOWN_SECONDS = float(os.getenv("VERIFY_COOLDOWN_SECONDS", "30") or 30)
_verify_cooldowns: dict[int, float] = {}

APPLICATION_INTRO = (
    "**Let's begin your application.**\n\n"
    "Answer every question below, then press **Submit**. Your answers are reviewed by staff.\n\n"
    "**1.** What is your preferred first name?\n"
    "**2.** What are your pronouns?\n"
    "**3.** Where did you hear about us?\n"
    "**4.** Please provide the Discord username of the user who invited you. "
    "(Type `NONE` if nobody invited you.)\n"
    "**5.** TMH is a community by and for transgender service members, veterans, "
    "and their families. What is your connection to that community?\n"
    "**6.** Select your branch and your status from the dropdowns."
)

def _question_modal(modal_title: str, field_key: str, label: str, *,
                    style: discord.TextStyle = discord.TextStyle.short,
                    placeholder: str | None = None,
                    max_length: int | None = None):
    """Build a one-field modal that writes its answer into the DM form session.

    Modal titles and TextInput labels are capped at 45 characters by Discord, so
    the full wording of each question lives in APPLICATION_INTRO instead.
    """
    class QuestionModal(discord.ui.Modal, title=modal_title):
        def __init__(self, mid: int):
            super().__init__(timeout=None)
            self.mid = mid
            self.v = discord.ui.TextInput(
                label=label,
                style=style,
                required=True,
                placeholder=placeholder,
                max_length=max_length,
            )
            self.add_item(self.v)

        async def on_submit(ms, mi: discord.Interaction):
            d = session_get(ms.mid) or {}
            d[field_key] = str(ms.v.value)
            session_set(ms.mid, mi.user.id, d)
            await mi.response.send_message("Saved.", ephemeral=(mi.guild_id is not None))

    return QuestionModal


def _disabled_form_view() -> "ApplicationFormView":
    """A throwaway, fully-disabled copy of the form.

    The live ApplicationFormView is registered once as a persistent view, so
    every applicant's interaction is dispatched to that single shared instance.
    Disabling `self.children` in a callback would therefore mutate the form for
    everyone; build a fresh instance to edit the message with instead.
    """
    view = ApplicationFormView()
    for child in view.children:
        child.disabled = True
    return view


class ApplicationFormView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the applicant this form was sent to may fill it in."""
        owner = session_owner(interaction.message.id) if interaction.message else None
        if owner is None or owner == interaction.user.id:
            return True
        try:
            await interaction.response.send_message(
                "This application form isn't yours.", ephemeral=True
            )
        except Exception:
            pass
        return False

    @discord.ui.button(label="1. Preferred Name", style=discord.ButtonStyle.secondary, custom_id="app:name", row=0)
    async def name_btn(self, i: discord.Interaction, _: discord.ui.Button):
        modal = _question_modal("Preferred Name", "name", "First name only")
        await i.response.send_modal(modal(i.message.id))

    @discord.ui.button(label="2. Pronouns", style=discord.ButtonStyle.secondary, custom_id="app:pronouns", row=0)
    async def pronouns_btn(self, i: discord.Interaction, _: discord.ui.Button):
        modal = _question_modal("Pronouns", "pronouns", "she/her, he/him, etc.")
        await i.response.send_modal(modal(i.message.id))

    @discord.ui.button(label="3. Referral Source", style=discord.ButtonStyle.secondary, custom_id="app:refer", row=0)
    async def refer_btn(self, i: discord.Interaction, _: discord.ui.Button):
        modal = _question_modal(
            "Referral Source", "refer", "Where did you hear about us?",
            style=discord.TextStyle.paragraph, max_length=500,
        )
        await i.response.send_modal(modal(i.message.id))

    @discord.ui.button(label="4. Who Invited You", style=discord.ButtonStyle.secondary, custom_id="app:invited", row=1)
    async def invited_btn(self, i: discord.Interaction, _: discord.ui.Button):
        modal = _question_modal(
            "Who Invited You", "invited_by", "Discord username of who invited you",
            placeholder="Type NONE if nobody invited you", max_length=64,
        )
        await i.response.send_modal(modal(i.message.id))

    @discord.ui.button(label="5. Your Connection", style=discord.ButtonStyle.secondary, custom_id="app:connection", row=1)
    async def connection_btn(self, i: discord.Interaction, _: discord.ui.Button):
        modal = _question_modal(
            "Community Connection", "connection", "Connection to the trans mil/vet community",
            style=discord.TextStyle.paragraph,
            placeholder="TMH is by and for trans service members, veterans & their families.",
            max_length=1000,
        )
        await i.response.send_modal(modal(i.message.id))

    @discord.ui.select(placeholder="Select your branch",
                       options=[discord.SelectOption(label=x) for x in ["Army","Navy","Marines","Air Force","Coast Guard","Space Force","Family"]],
                       custom_id="app:branch", row=2)
    async def branch_sel(self, i: discord.Interaction, sel: discord.ui.Select):
        d = session_get(i.message.id) or {}
        d["branch_choice"] = sel.values[0]
        session_set(i.message.id, i.user.id, d)
        await i.response.defer()

    @discord.ui.select(placeholder="Select your status",
                       options=[discord.SelectOption(label=x) for x in ["Current","Former","DEP/Future Warrior"]],
                       custom_id="app:status", row=3)
    async def status_sel(self, i: discord.Interaction, sel: discord.ui.Select):
        d = session_get(i.message.id) or {}
        d["status_choice"] = sel.values[0]
        session_set(i.message.id, i.user.id, d)
        await i.response.defer()

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.success, custom_id="app:submit", row=4)
    async def submit_btn(self, i: discord.Interaction, _: discord.ui.Button):
        try:
            await i.response.defer()
        except discord.HTTPException:
            pass
        try:
            data = session_get(i.message.id) or {}

            blocked = application_block_reason(i.user.id)
            if blocked:
                session_del(i.message.id)
                await i.followup.send(blocked)
                return

            if int(data.get("form_version") or 1) < FORM_VERSION:
                session_del(i.message.id)
                try:
                    await i.message.edit(view=_disabled_form_view())
                except discord.HTTPException:
                    pass
                await i.followup.send(
                    "This application form is out of date. Please press **Apply** in the "
                    "server again to start a fresh application. Sorry for the detour!"
                )
                return

            missing = [
                k for k in ("name", "pronouns", "refer", "invited_by", "connection",
                            "branch_choice", "status_choice")
                if not data.get(k)
            ]
            if missing:
                await i.followup.send("Missing: " + ", ".join(m.replace("_", " ") for m in missing))
                return

            if not is_captcha_verified(i.user.id):
                await i.followup.send(
                    "You must complete human verification before submitting. "
                    "Check your DMs for the verification message, or ask staff for help."
                )
                return

            gid = int(data.get("guild_id") or HOME_GUILD_ID or 0)
            guild = i.client.get_guild(gid) if gid else None
            if not guild:
                await i.followup.send("Setup error: guild not found.")
                return

            staff_ch = guild.get_channel(STAFF_REVIEW_CHANNEL_ID)
            data["discord_tag"] = str(i.user)
            data["discord_id"] = str(i.user.id)
            try:
                display_name = i.user.display_name
            except AttributeError:
                display_name = getattr(i.user, "nick", None) or getattr(i.user, "name", None)
            if display_name:
                data["discord_display_name"] = display_name
            username = getattr(i.user, "name", None)
            if username:
                data["discord_username"] = username
            global_name = getattr(i.user, "global_name", None)
            if global_name:
                data["discord_global_name"] = global_name

            invite_status, inviter = await resolve_inviter(guild, data.get("invited_by"))
            data["invited_by_status"] = invite_status
            if inviter is not None:
                data["invited_by_id"] = str(inviter.id)
                data["invited_by_resolved"] = str(inviter)

            member = guild.get_member(i.user.id)
            embed = build_application_embed(data, i.user.id, user=i.user, member=member)
            embed.set_footer(text=f"{i.user} ({i.user.id})")

            if staff_ch:
                review_view = ApplicationReviewView(i.user.id, data)
                review_msg = await staff_ch.send(embed=embed, view=review_view)
                review_view.review_msg_id = review_msg.id
                client = i.client
                if client:
                    try:
                        client.add_view(review_view, message_id=review_msg.id)
                    except Exception:
                        pass
                store_pending_application(review_msg.id, i.user.id, data)

            mark_as_submitted(i.user.id, discord.utils.utcnow().isoformat())
            session_del(i.message.id)

            await i.message.edit(view=_disabled_form_view())
            await i.followup.send("Submitted.")

        except Exception as e:
            log_application_error("application_submit", e)
            try:
                await i.followup.send("Unexpected error submitting. Try again.")
            except Exception:
                pass
            raise


class ApplicationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.primary, custom_id="app:open")
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button):
        blocked = application_block_reason(interaction.user.id)
        if blocked:
            await interaction.response.send_message(blocked, ephemeral=True)
            return
        if not is_captcha_verified(interaction.user.id):
            now = discord.utils.utcnow().timestamp()
            if now - _verify_cooldowns.get(interaction.user.id, 0.0) < VERIFY_COOLDOWN_SECONDS:
                await interaction.response.send_message(
                    "Your verification message is already in your DMs. Complete it, "
                    "then press **Apply** again.",
                    ephemeral=True,
                )
                return
            _verify_cooldowns[interaction.user.id] = now

            from commands.captcha import CaptchaView, build_verification_embed
            try:
                dm = await interaction.user.create_dm()
                await dm.send(embed=build_verification_embed(), view=CaptchaView())
            except discord.Forbidden:
                await interaction.response.send_message(
                    "I could not DM you the verification step. Turn on direct messages from "
                    "server members and press **Apply** again, or ask staff to verify you.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                await interaction.response.send_message(
                    "Something went wrong sending your verification DM. Try again shortly, "
                    "or ask staff for help.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "Check your DMs — verify you're human, then press **Apply** again.",
                ephemeral=True,
            )
            return
        now = discord.utils.utcnow().timestamp()
        last = _apply_cooldowns.get(interaction.user.id, 0.0)
        if now - last < APPLY_COOLDOWN_SECONDS:
            await interaction.response.send_message(
                f"Please wait {int(APPLY_COOLDOWN_SECONDS - (now - last))}s before requesting "
                "another application form, and check your DMs for the one already sent.",
                ephemeral=True,
            )
            return
        _apply_cooldowns[interaction.user.id] = now
        try:
            dm = await interaction.user.create_dm()
            msg = await dm.send(APPLICATION_INTRO, view=ApplicationFormView())
            clear_sessions_for_user(interaction.user.id, keep_message_id=msg.id)
            session_set(msg.id, interaction.user.id, {
                "guild_id": interaction.guild.id,
                "form_version": FORM_VERSION,
            })
            await interaction.response.send_message("Check your DMs.", ephemeral=True)
        except discord.Forbidden:
            _apply_cooldowns.pop(interaction.user.id, None)
            await interaction.response.send_message("Enable DMs and try again.", ephemeral=True)

class ApplicationReviewView(discord.ui.View):
    def __init__(
        self,
        applicant_id: int,
        data: dict | None = None,
        *,
        application_data: dict | None = None,
        review_msg_id: int | None = None,
        **_
    ):
        super().__init__(timeout=None)
        self.applicant_id = int(applicant_id)
        self.data = data or application_data or {}
        self.review_msg_id = review_msg_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Approve / Deny / Open Ticket are staff-only.

        Previously these buttons were gated only by who could see the staff
        review channel, so any member with visibility could approve themselves
        or anyone else.
        """
        if is_staff(interaction.user):
            return True
        logging.warning(
            "Non-staff %s (%s) tried to use application review controls on %s",
            interaction.user,
            interaction.user.id,
            self.applicant_id,
        )
        try:
            await interaction.response.send_message(
                "Only staff can act on applications.", ephemeral=True
            )
        except Exception:
            pass
        return False

    def _load_application_data(self, message_id: int | None = None) -> dict:
        if isinstance(self.data, dict) and self.data:
            return self.data
        mid = message_id or self.review_msg_id
        entry = fetch_pending_application_entry(self.applicant_id, mid)
        if entry:
            self.data = entry.get("data") or {}
        return self.data or {}
    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="application_approve")
    async def approve(self, i: discord.Interaction, _: discord.ui.Button):
        try:
            await self._decide(i, True)
        except Exception as exc:
            log_application_error("application_approve_button", exc)
            await send_application_error(i, "Failed to open approval modal. See application_errors.log for details.")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="application_deny")
    async def deny(self, i: discord.Interaction, _: discord.ui.Button):
        try:
            await self._decide(i, False)
        except Exception as exc:
            log_application_error("application_deny_button", exc)
            await send_application_error(i, "Failed to open denial modal. See application_errors.log for details.")

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, custom_id="application_ticket")
    async def ticket(self, i: discord.Interaction, _: discord.ui.Button):
        await self._ticket(i)

    @discord.ui.button(label="View Application", style=discord.ButtonStyle.secondary, custom_id="application_view")
    async def view_application(self, i: discord.Interaction, _: discord.ui.Button):
        try:
            mid = self.review_msg_id or (i.message.id if i.message else None)
            data = self._load_application_data(mid)
            if not data:
                await i.response.send_message(
                    "No stored answers for this application. It may have been decided already.",
                    ephemeral=True,
                )
                return
            member = i.guild.get_member(self.applicant_id) if i.guild else None
            user = member
            if user is None:
                try:
                    user = await i.client.fetch_user(self.applicant_id)
                except (discord.HTTPException, discord.NotFound):
                    user = None
            embed = build_application_detail_embed(data, self.applicant_id, user=user, member=member)
            await i.response.send_message(embed=embed, ephemeral=True)
        except Exception as exc:
            log_application_error("application_view_button", exc)
            await send_application_error(i, "Could not load this application.")

    async def _decide(self, interaction: discord.Interaction, approved: bool):
        original_message_id = self.review_msg_id or (interaction.message.id if interaction.message else None)
        original_guild = interaction.guild
        view_ref = self

        class Reason(discord.ui.Modal, title=("Approval Reason" if approved else "Denial Reason")):
            reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph)

            async def on_submit(ms, mi: discord.Interaction):
                try:
                    await mi.response.defer(ephemeral=True)
                except discord.HTTPException:
                    pass
                try:
                    guild = mi.guild or original_guild
                    if guild is None:
                        await send_application_error(mi, "Guild not found for this application.")
                        return
                    data = view_ref._load_application_data(original_message_id)
                    member = guild.get_member(view_ref.applicant_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(view_ref.applicant_id)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            member = None
                    if not member:
                        await send_application_error(
                            mi,
                            f"<@{view_ref.applicant_id}> is no longer in the server, so roles cannot be "
                            "assigned. Run `/list_pending` to close applications from people who left.",
                        )
                        return
                    bot_ref = mi.client
                    if not isinstance(bot_ref, commands.Bot):
                        raise RuntimeError("Bot reference unavailable for application decision.")

                    await process_application_decision(
                        bot_ref,
                        applicant_id=member.id,
                        approved=approved,
                        reviewer_name=str(mi.user),
                        reason=ms.reason.value,
                        guild=guild,
                        member=member,
                        review_message_id=original_message_id,
                        application_data=data,
                    )
                except Exception as exc:
                    log_application_error("application_decision_modal", exc)
                    await send_application_error(
                        mi, "An error occurred handling this decision. See application_errors.log for details."
                    )
                    return

                try:
                    for child in view_ref.children:
                        child.disabled = True
                    if mi.message is not None:
                        await mi.message.edit(view=view_ref)
                except Exception as exc:
                    logging.info(
                        "Decision for %s succeeded; could not grey out the review controls: %s",
                        view_ref.applicant_id, exc,
                    )
                try:
                    await mi.followup.send(
                        f"Application {'approved' if approved else 'denied'} for {member.mention}.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass

        await interaction.response.send_modal(Reason())

    async def _ticket(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(self.applicant_id)
        if not member:
            await interaction.response.send_message("User not found.", ephemeral=True); return
        category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Ticket category missing.", ephemeral=True); return
        staff = interaction.guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None
        if staff is None:
            staff = discord.utils.get(interaction.guild.roles, name="Staff")
        if staff is None:
            logging.warning(
                "No staff role resolved for ticket in guild %s; staff will not see the channel.",
                interaction.guild.id,
            )
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True),
        }
        if staff: overwrites[staff] = discord.PermissionOverwrite(view_channel=True)
        ch = await interaction.guild.create_text_channel(
            name=f"ticket-{member.name}", category=category, overwrites=overwrites,
            topic=f"Application of {member.display_name}")
        content, embed, file = build_ticket_greeting(member)
        ticket_msg = await ch.send(content, embed=embed, file=file, view=TicketCloseView())
        await interaction.response.send_message(f"Ticket created: {ch.mention}", ephemeral=True)
        record_ticket_message(ch.id, ticket_msg.id)

class TicketCloseView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Closing a ticket deletes the channel and exports its log - staff only."""
        return await deny_non_staff(interaction)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close_button")
    async def close_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        channel = interaction.channel
        guild = interaction.guild

        class Close(discord.ui.Modal, title="Close Ticket"):
            reason = discord.ui.TextInput(label="Reason for closing", style=discord.TextStyle.paragraph, required=True)

            async def on_submit(ms, mi: discord.Interaction):
                try:
                    await mi.response.defer(ephemeral=True)
                except discord.HTTPException:
                    pass

                try:
                    messages = [m async for m in channel.history(limit=1000, oldest_first=True)]
                    rows = [{"Author": f"{m.author} ({m.author.id})", "Content": m.content,
                             "Time": m.created_at.isoformat()} for m in messages if not m.author.bot]
                    buf = io.StringIO()
                    w = csv.DictWriter(buf, fieldnames=["Author", "Content", "Time"])
                    w.writeheader(); w.writerows(rows); buf.seek(0)
                    file = discord.File(io.BytesIO(buf.read().encode()),
                                        filename=f"{str(mi.user).replace('#','_')}_ticket_log.csv")
                    log = guild.get_channel(TICKET_LOG_CHANNEL_ID) if guild else None
                    if log:
                        await log.send(
                            f"Ticket closed by {mi.user.mention}\nReason: {ms.reason.value}", file=file
                        )
                except Exception as exc:
                    log_application_error("ticket_close_export", exc)
                    await send_application_error(
                        mi, "Could not export the transcript, so the channel was left open. Try again."
                    )
                    return

                try:
                    await mi.followup.send(
                        "Ticket closed. This channel will now self destruct", ephemeral=True
                    )
                except discord.HTTPException:
                    pass

                delete_ticket_record(channel.id)
                await asyncio.sleep(2)
                try:
                    await channel.delete(reason=f"Ticket closed by {mi.user}")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log_application_error("ticket_close_delete", exc)

        await interaction.response.send_modal(Close())

async def refresh_ticket_views(bot: commands.Bot):
    """Ensure every tracked ticket message keeps an active close button view."""
    with _connect(DB_PATH) as con:
        rows = con.execute("SELECT message_id, channel_id FROM tickets").fetchall()
    if not rows:
        return
    bot_id = bot.user.id if bot.user else None
    markers = ticket_greeting_markers()
    pruned = 0
    for stored_message_id, channel_id in rows:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.NotFound:
                delete_ticket_record(channel_id)
                pruned += 1
                continue
            except (discord.Forbidden, discord.HTTPException):
                continue
        message = None
        replaced = False
        if stored_message_id:
            try:
                message = await channel.fetch_message(stored_message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            try:
                async for candidate in channel.history(limit=200, oldest_first=True):
                    if bot_id and candidate.author.id != bot_id:
                        continue
                    content = (candidate.content or "").lower()
                    if candidate.components or any(m in content for m in markers):
                        message = candidate
                        replaced = True
                        break
            except (discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            try:
                message = await channel.send(
                    "Ticket controls restored. Use this button to close the ticket when finished.",
                    view=TicketCloseView(),
                )
            except (discord.Forbidden, discord.HTTPException):
                continue
            replaced = True
        if replaced or message.id != stored_message_id:
            record_ticket_message(channel.id, message.id)
        if not message.components:
            try:
                await message.edit(view=TicketCloseView())
            except (discord.Forbidden, discord.HTTPException):
                try:
                    message = await channel.send(
                        "Ticket controls restored. Use this button to close the ticket when finished.",
                        view=TicketCloseView(),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    continue
                record_ticket_message(channel.id, message.id)
        bot.add_view(TicketCloseView(), message_id=message.id)
    if pruned:
        logging.info("Dropped %s ticket record(s) whose channel no longer exists.", pruned)

PENDING_TABLE_LIMIT = 40
PENDING_SELECT_LIMIT = 25


def _is_plausible_snowflake(value) -> bool:
    """Discord IDs are ~17-19 digits. Anything shorter is not a real user ID."""
    try:
        return 10**14 <= int(value) < 10**20
    except (TypeError, ValueError):
        return False


async def load_pending_queue(guild: discord.Guild) -> list[dict]:
    """The pending queue, with each entry marked present / departed / unresolvable.

    `in_server` is True, False, or None. None means the stored applicant ID is
    corrupt, so we genuinely cannot tell - those must never be treated as
    departed, or a real application gets closed on the strength of a bad ID.
    """
    entries = fetch_pending_entries()

    seen = {e["user_id"] for e in entries}
    with _connect(DB_PATH) as con:
        extra = con.execute("SELECT user_id FROM applications WHERE status='pending'").fetchall()
    for (uid,) in extra:
        if int(uid) not in seen:
            entries.append({
                "message_id": None, "user_id": int(uid), "data": {}, "status": "pending",
                "username": str(uid), "branch": "-", "applied": None,
            })

    trust_cache = bool(getattr(guild, "chunked", False))
    for entry in entries:
        entry["member"] = None
        if not _is_plausible_snowflake(entry["user_id"]):
            entry["in_server"] = None
            continue
        member = guild.get_member(entry["user_id"])
        if member is None and not trust_cache:
            try:
                member = await guild.fetch_member(entry["user_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None
        entry["member"] = member
        entry["in_server"] = member is not None
    return entries


def departed_entries(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("in_server") is False]


def unresolvable_entries(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("in_server") is None]


async def close_departed_applications(
    bot: commands.Bot, guild: discord.Guild, entries: list[dict], closed_by: str
) -> int:
    """Mark each application closed and retire its review card."""
    closed = 0
    for entry in entries:
        delete_pending(user_id=entry["user_id"], message_id=entry.get("message_id"))
        update_application_status(
            entry["user_id"], "closed", decided_by=closed_by, reason="Left the server"
        )
        await retire_review_message(bot, guild, entry.get("message_id"))
        closed += 1
    return closed


class ConfirmCloseDepartedView(discord.ui.View):
    def __init__(self, entries: list[dict], invoker_id: int):
        super().__init__(timeout=180)
        self.entries = entries
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.invoker_id and is_staff(interaction.user):
            return True
        await interaction.response.send_message(
            "Only the person who ran the command can confirm this.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Close them", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        closed = await close_departed_applications(
            interaction.client, interaction.guild, self.entries, str(interaction.user)
        )
        logging.info("%s closed %s application(s) for departed members", interaction.user, closed)
        await interaction.followup.send(
            f"Closed {closed} application{'' if closed == 1 else 's'} for members who have left.",
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=self)
        self.stop()


def fetch_pending_entries() -> list[dict]:
    """Every application still awaiting a decision, newest first."""
    with _connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT p.message_id, p.user_id, p.data, a.status "
            "FROM pending_applications p LEFT JOIN applications a ON a.user_id = p.user_id"
        ).fetchall()
    entries = []
    for mid, uid, raw, status in rows:
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
        entries.append({
            "message_id": int(mid),
            "user_id": int(uid),
            "data": data,
            "status": status,
            "username": (data.get("discord_username") or data.get("discord_tag")
                         or data.get("name") or str(uid)),
            "branch": data.get("branch_choice") or data.get("branch") or "-",
            "applied": discord.utils.snowflake_time(int(mid)),
        })
    entries.sort(key=lambda e: e["applied"], reverse=True)
    return entries


_STATUS_LABEL = {True: "in server", False: "LEFT", None: "? broken id"}


def build_pending_table_embed(entries: list[dict]) -> discord.Embed:
    shown = entries[:PENDING_TABLE_LIMIT]
    name_w = min(22, max(8, max(len(e["username"]) for e in shown)))
    header = f"{'USERNAME':<{name_w}}  {'APPLIED':<10}  {'BRANCH':<13}  STATUS"
    lines = [header, "-" * len(header)]
    for e in shown:
        applied = f"{e['applied']:%Y-%m-%d}" if e.get("applied") else "unknown   "
        lines.append(
            f"{e['username'][:name_w]:<{name_w}}  {applied:<10}  "
            f"{e['branch'][:13]:<13}  {_STATUS_LABEL[e.get('in_server')]}"
        )

    gone = len(departed_entries(entries))
    embed = discord.Embed(
        title=f"Applications awaiting review - {len(entries)}",
        description="```\n" + "\n".join(lines) + "\n```",
        color=discord.Color.orange() if gone else discord.Color.blue(),
    )

    broken = unresolvable_entries(entries)
    if broken:
        embed.add_field(
            name=f"⚠️ {len(broken)} entr{'y' if len(broken) == 1 else 'ies'} with a corrupted ID",
            value=(
                "We cannot tell whether these people left, so they are left alone. "
                "Repair with `/remove_pending` then `/refreshview`:\n"
                + "\n".join(f"`{e['message_id']}` — {e['username']}" for e in broken[:10])
            )[:1024],
            inline=False,
        )

    notes = []
    if len(entries) > PENDING_TABLE_LIMIT:
        notes.append(f"showing the {PENDING_TABLE_LIMIT} most recent")
    if len(entries) > PENDING_SELECT_LIMIT:
        notes.append(f"dropdown lists the newest {PENDING_SELECT_LIMIT}")
    embed.set_footer(text=" · ".join(notes) if notes else discord.utils.MISSING)
    return embed


class CloseDepartedButton(discord.ui.Button):
    """Only added to the list when at least one applicant has actually left."""

    def __init__(self, departed: list[dict], row: int = 1):
        super().__init__(
            label=f"Close {len(departed)} who left",
            style=discord.ButtonStyle.danger,
            row=row,
        )
        self.departed = departed

    async def callback(self, interaction: discord.Interaction):
        preview = "\n".join(
            f"{e['username'][:24]:<24} " + (f"{e['applied']:%Y-%m-%d}" if e.get("applied") else "unknown")
            for e in self.departed[:25]
        )
        if len(self.departed) > 25:
            preview += f"\n… and {len(self.departed) - 25} more"
        embed = discord.Embed(
            title=f"Close {len(self.departed)} application(s)?",
            description="```\n" + preview + "\n```",
            color=discord.Color.orange(),
        )
        embed.set_footer(text="They can apply again if they ever rejoin.")
        await interaction.response.send_message(
            embed=embed,
            view=ConfirmCloseDepartedView(self.departed, interaction.user.id),
            ephemeral=True,
        )


class DepartedApplicantView(discord.ui.View):
    """Shown instead of Approve/Deny when the applicant is no longer a member."""

    def __init__(self, entry: dict):
        super().__init__(timeout=300)
        self.entry = entry

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await deny_non_staff(interaction)

    @discord.ui.button(label="Close application", style=discord.ButtonStyle.danger)
    async def close_one(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await close_departed_applications(
            interaction.client, interaction.guild, [self.entry], str(interaction.user)
        )
        logging.info("%s closed departed application for %s", interaction.user, self.entry["user_id"])
        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            f"Closed the application for **{self.entry['username']}**.", ephemeral=True
        )

    @discord.ui.button(label="View Application", style=discord.ButtonStyle.secondary)
    async def view_full(self, interaction: discord.Interaction, _: discord.ui.Button):
        embed = build_application_detail_embed(
            self.entry["data"], self.entry["user_id"], user=None, member=None
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class PendingListView(discord.ui.View):
    """The applicant picker under /list_pending.

    Selecting someone opens their full application with the same Approve /
    Deny / Open Ticket buttons as the original card, so staff can work straight
    down the list instead of hunting through channel history.
    """

    def __init__(self, entries: list[dict]):
        super().__init__(timeout=600)
        usable = [e for e in entries if e.get("message_id")][:PENDING_SELECT_LIMIT]
        self.entries = {str(e["message_id"]): e for e in usable}
        self.pick.options = [
            discord.SelectOption(
                label=e["username"][:100],
                value=str(e["message_id"]),
                description=(
                    (f"{e['applied']:%Y-%m-%d} · " if e.get("applied") else "")
                    + (f"{e['branch']}" if e.get("in_server") is not False else "has left the server")
                )[:100],
                emoji="⚠️" if e.get("in_server") is not True else None,
            )
            for e in usable
        ]
        departed = departed_entries(entries)
        if departed:
            self.add_item(CloseDepartedButton(departed))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await deny_non_staff(interaction)

    @discord.ui.select(placeholder="Select an applicant to approve, deny, or open a ticket")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        entry = self.entries.get(select.values[0])
        if entry is None:
            await interaction.response.send_message("That entry is no longer available.", ephemeral=True)
            return

        guild = interaction.guild
        member = entry.get("member") or (guild.get_member(entry["user_id"]) if guild else None)
        user = member
        if user is None:
            try:
                user = await interaction.client.fetch_user(entry["user_id"])
            except (discord.HTTPException, discord.NotFound):
                user = None

        embed = build_application_embed(entry["data"], entry["user_id"], user=user, member=member)
        embed.set_footer(text=f"{entry['username']} ({entry['user_id']})")

        link = None
        if guild and STAFF_REVIEW_CHANNEL_ID:
            link = (f"https://discord.com/channels/{guild.id}/"
                    f"{STAFF_REVIEW_CHANNEL_ID}/{entry['message_id']}")
        content = f"[Jump to the original card]({link})" if link else None

        if entry.get("in_server") is False:
            embed.color = discord.Color.orange()
            embed.add_field(
                name="⚠️ No longer in the server",
                value="Roles cannot be assigned, so this application can only be closed.",
                inline=False,
            )
            view = DepartedApplicantView(entry)
        elif entry.get("in_server") is None:
            embed.color = discord.Color.red()
            embed.add_field(
                name="⚠️ Corrupted applicant ID",
                value=(
                    "The stored ID is not a valid Discord ID, so this application cannot be "
                    f"actioned. Repair it with `/remove_pending message_id:{entry['message_id']}` "
                    f"then `/refreshview message_id:{entry['message_id']}`."
                ),
                inline=False,
            )
            view = None
        else:
            view = ApplicationReviewView(
                entry["user_id"], entry["data"], review_msg_id=entry["message_id"]
            )

        await interaction.response.send_message(
            content=content,
            embed=embed,
            view=view if view is not None else discord.utils.MISSING,
            ephemeral=True,
        )


class Applications(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Clear an application when its owner leaves, so rejoining works.

        Roles are only granted at the moment of approval, and leaving strips
        them. Without this, someone who leaves and comes back is approved in
        the database with no roles in the server, and the Apply button tells
        them they are already approved - a dead end only staff can undo.

        'denied' is deliberately NOT cleared. Otherwise a denial could be reset
        by simply leaving and rejoining. Staff can still clear one on purpose
        with /app_reset.
        """
        status = get_application_status(member.id)
        if status not in {"approved", "pending"}:
            return

        entry = fetch_pending_application_entry(member.id)
        review_message_id = entry.get("message_id") if entry else None

        delete_pending(user_id=member.id)
        clear_sessions_for_user(member.id)
        update_application_status(member.id, "closed", decided_by="(left the server)",
                                  reason=f"Left the server while {status}")
        try:
            await retire_review_message(self.bot, member.guild, review_message_id)
        except Exception:
            pass

        logging.info("%s (%s) left while %s; application closed so they can reapply",
                     member, member.id, status)

    async def cog_load(self):
        removed = prune_stale_pending()
        if removed:
            logging.info("Pruned %s already-decided pending application row(s).", removed)
        self.bot.add_view(ApplicationView())
        self.bot.add_view(ApplicationFormView())
        self.bot.add_view(TicketCloseView())

    @app_commands.command(name="app_repost", description="Repost the application button")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Target channel")
    async def app_repost(self, interaction: Interaction, channel: discord.TextChannel | None = None):
        ch = channel or interaction.channel
        await ch.send("Let's begin your application!", view=ApplicationView())
        await interaction.response.send_message("Posted.", ephemeral=True)

    @app_commands.command(name="refreshview", description="Re-attach review buttons to a staff message")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(message_id="Staff-review message ID")
    async def refreshview(self, interaction: Interaction, message_id: str):
        try: mid = int(message_id)
        except ValueError:
            await interaction.response.send_message("Invalid message ID.", ephemeral=True); return
        ch = interaction.client.get_channel(STAFF_REVIEW_CHANNEL_ID)
        if not ch:
            await interaction.response.send_message("Channel not found.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        with _connect(DB_PATH) as con:
            row = con.execute("SELECT user_id,data FROM pending_applications WHERE message_id=?", (mid,)).fetchone()
        msg: discord.Message | None = None
        if not row:
            try:
                msg = await ch.fetch_message(mid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                await interaction.followup.send(f"Could not backfill: {exc}", ephemeral=True); return
            embed = msg.embeds[0] if msg.embeds else None
            uid, parsed = parse_application_embed(embed)
            if not uid:
                await interaction.followup.send("Unable to determine applicant from embed.", ephemeral=True); return
            store_pending_application(mid, uid, parsed); row = (uid, json.dumps(parsed))
        if msg is None:
            try:
                msg = await ch.fetch_message(mid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                await interaction.followup.send(f"Failed to fetch message: {exc}", ephemeral=True); return
        uid, data_json = row
        data = json.loads(data_json)
        refreshed_view = ApplicationReviewView(uid, data, review_msg_id=mid)
        status_msg = "View refreshed."
        try:
            await msg.edit(view=refreshed_view)
        except discord.HTTPException as exc:
            if getattr(exc, "code", None) == 50005:
                embed = msg.embeds[0] if msg.embeds else discord.Embed(title="New Application")
                new_msg = await ch.send(embed=embed, view=refreshed_view)
                refreshed_view.review_msg_id = new_msg.id
                store_pending_application(new_msg.id, uid, data)
                delete_pending(message_id=mid)
                status_msg = (
                    "Original message could not be edited (not authored by the bot). "
                    "A new review message was posted instead."
                )
            else:
                await interaction.followup.send(f"Failed to refresh view: {exc}", ephemeral=True)
                return
        await interaction.followup.send(status_msg, ephemeral=True)

    @app_commands.command(name="list_pending", description="Show applications awaiting review")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_pending(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("Run this in the server.", ephemeral=True)
            return
        entries = await load_pending_queue(interaction.guild)
        if not entries:
            await interaction.followup.send("No applications awaiting review.", ephemeral=True)
            return
        await interaction.followup.send(
            embed=build_pending_table_embed(entries),
            view=PendingListView(entries),
            ephemeral=True,
        )

    @app_commands.command(name="remove_pending", description="Remove a pending application by message ID")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(message_id="Staff-review message ID")
    async def remove_pending(self, interaction: Interaction, message_id: str):
        try: mid = int(message_id)
        except ValueError:
            await interaction.response.send_message("Invalid message ID.", ephemeral=True); return
        delete_pending(message_id=mid)
        await interaction.response.send_message("Removed.", ephemeral=True)

    @app_commands.command(
        name="app_gaps",
        description="Find members who may have slipped through (e.g. while the bot was offline)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        days="How far back to look. Ignored if you give a start date.",
        since="Start date, YYYY-MM-DD (e.g. the day an outage began)",
        until="Optional end date, YYYY-MM-DD",
    )
    async def app_gaps(self, interaction: Interaction,
                       days: app_commands.Range[int, 1, 365] = 14,
                       since: str | None = None, until: str | None = None):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Run this in the server.", ephemeral=True)
            return

        def parse(value: str | None):
            if not value:
                return None
            try:
                return datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                return "bad"

        start, end = parse(since), parse(until)
        if start == "bad" or end == "bad":
            await interaction.followup.send("Dates must look like `2026-08-08`.", ephemeral=True)
            return
        now = discord.utils.utcnow()
        if start is None:
            start = now - timedelta(days=int(days))
        if end is None:
            end = now
        if start >= end:
            await interaction.followup.send("The start date must be before the end date.", ephemeral=True)
            return

        never_verified, verified_never_applied, abandoned, applied = [], [], [], 0
        with _connect(DB_PATH) as con:
            sessions = {int(u) for (u,) in con.execute("SELECT DISTINCT user_id FROM app_sessions")}

        for member in guild.members:
            if member.bot or member.joined_at is None:
                continue
            if not (start <= member.joined_at <= end):
                continue
            if has_submitted(member.id):
                applied += 1
                continue
            if member.id in sessions:
                abandoned.append(member)
            elif is_captcha_verified(member.id):
                verified_never_applied.append(member)
            else:
                never_verified.append(member)

        window = f"{start:%d %b %Y %H:%M} to {end:%d %b %Y %H:%M} UTC"
        total = len(never_verified) + len(verified_never_applied) + len(abandoned)
        if total == 0:
            await interaction.followup.send(
                f"**BOTTOM LINE: nothing slipped through.**\n"
                f"Everyone who joined in this window applied "
                f"({applied} application{'' if applied == 1 else 's'}).\n\n"
                f"Window checked: {window}",
                ephemeral=True)
            return

        if never_verified:
            bluf = (f"**BOTTOM LINE: {len(never_verified)} member"
                    f"{'' if len(never_verified) == 1 else 's'} need action now.**\n"
                    f"They never received a verification DM and cannot apply until you fix it.\n\n"
                    f"Window checked: {window}")
            await interaction.followup.send(bluf, ephemeral=True)
            return
