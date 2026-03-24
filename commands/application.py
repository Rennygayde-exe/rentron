import os, io, csv, json, sqlite3, asyncio, traceback, logging, re
from pathlib import Path
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv
from datetime import timedelta

load_dotenv()
BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = str(Path(os.getenv("APPLICATIONS_DB") or (BASE_DIR / "applications.db")))
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
TICKET_GREETING_PHRASE = "a staff member will assist you shortly."
CHANNEL_TAG_RE = re.compile(r"#([\w\-]+)")

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
    with sqlite3.connect(DB_PATH) as con:
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
    with sqlite3.connect(DB_PATH) as con:
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
        con.commit()

def has_submitted(user_id:int)->bool:
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("SELECT 1 FROM applications WHERE user_id=?", (int(user_id),)).fetchone() is not None

def mark_as_submitted(user_id:int, submitted_at:str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR REPLACE INTO applications(user_id,submitted_at,status) VALUES(?,?,'pending')",
                    (int(user_id), submitted_at)); con.commit()

def is_captcha_verified(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            "SELECT 1 FROM captcha_verified WHERE user_id=?", (int(user_id),)
        ).fetchone() is not None

def mark_captcha_verified(user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO captcha_verified(user_id, verified_at) "
            "VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (int(user_id),),
        )
        con.commit()

def update_application_status(user_id:int, status:str):
    normalized = (status or 'pending').strip().lower()
    if normalized not in {'pending', 'approved', 'denied', 'closed'}:
        normalized = 'pending'
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO applications(user_id, submitted_at, status) VALUES(?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?) "
            "ON CONFLICT(user_id) DO UPDATE SET status=excluded.status",
            (int(user_id), normalized),
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
            for candidate in roles_cache:
                normalized = _normalize_role_name(candidate.name)
                if normalized == target or normalized.startswith(target):
                    return candidate
    return None

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
        with sqlite3.connect(DB_PATH) as con:
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
    update_application_status(member_obj.id, status_label)

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
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR REPLACE INTO pending_applications(message_id,user_id,data) VALUES(?,?,?)",
                    (int(message_id), int(user_id), json.dumps(data))); con.commit()

def record_ticket_message(channel_id: int, message_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM tickets WHERE channel_id=?", (int(channel_id),))
        con.execute(
            "INSERT OR REPLACE INTO tickets(message_id,channel_id) VALUES(?,?)",
            (int(message_id), int(channel_id)),
        )
        con.commit()

ID_FOOTER_RE = re.compile(r"(\d{5,})")

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
    }
    for field in embed.fields:
        field_name = (field.name or "").strip().lower()
        if not field_name:
            continue
        value = field.value or ""
        targets = field_map.get(field_name) or (field_name.replace(" ", "_"),)
        for target in targets:
            data[target] = value
    applicant_id: int | None = None
    footer_text = (embed.footer.text or "").strip() if embed.footer else ""
    author_text = (embed.author.name or "").strip() if embed.author else ""
    for source in (footer_text, author_text):
        if not source:
            continue
        match = ID_FOOTER_RE.search(source)
        if match:
            try:
                applicant_id = int(match.group(1))
                break
            except ValueError:
                continue
    if footer_text and "(" in footer_text and ")" in footer_text:
        tag = footer_text.rsplit("(", 1)[0].strip()
        if tag:
            data.setdefault("discord_tag", tag)
    return applicant_id, data

def delete_pending(*, user_id:int|None=None, message_id:int|None=None):
    with sqlite3.connect(DB_PATH) as con:
        if user_id: con.execute("DELETE FROM pending_applications WHERE user_id=?", (int(user_id),))
        if message_id: con.execute("DELETE FROM pending_applications WHERE message_id=?", (int(message_id),))
        con.commit()

def session_get(mid:int)->dict|None:
    with sqlite3.connect(DB_PATH) as con:
        r = con.execute("SELECT data FROM app_sessions WHERE message_id=?", (int(mid),)).fetchone()
        return json.loads(r[0]) if r else None

def session_set(mid:int, uid:int, data:dict):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR REPLACE INTO app_sessions(message_id,user_id,data) VALUES(?,?,?)",
                    (int(mid), int(uid), json.dumps(data))); con.commit()

def session_del(mid:int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM app_sessions WHERE message_id=?", (int(mid),)); con.commit()

class ApplicationFormView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="1. Preferred Name", style=discord.ButtonStyle.secondary, custom_id="app:name")
    async def name_btn(self, i: discord.Interaction, _: discord.ui.Button):
        msg_id = i.message.id
        class M(discord.ui.Modal, title="Preferred Name"):
            def __init__(self, mid: int):
                super().__init__(timeout=None)
                self.mid = mid
                self.v = discord.ui.TextInput(label="First name only", style=discord.TextStyle.short, required=True)
                self.add_item(self.v)
            async def on_submit(ms, mi: discord.Interaction):
                d = session_get(ms.mid) or {}
                d["name"] = str(ms.v.value)
                session_set(ms.mid, mi.user.id, d)
                await mi.response.send_message("Saved.", ephemeral=(mi.guild_id is not None))
        await i.response.send_modal(M(msg_id))

    @discord.ui.button(label="2. Pronouns", style=discord.ButtonStyle.secondary, custom_id="app:pronouns")
    async def pronouns_btn(self, i: discord.Interaction, _: discord.ui.Button):
        msg_id = i.message.id
        class M(discord.ui.Modal, title="Pronouns"):
            def __init__(self, mid: int):
                super().__init__(timeout=None)
                self.mid = mid
                self.v = discord.ui.TextInput(label="she/her, he/him, etc.", style=discord.TextStyle.short, required=True)
                self.add_item(self.v)
            async def on_submit(ms, mi: discord.Interaction):
                d = session_get(ms.mid) or {}
                d["pronouns"] = str(ms.v.value)
                session_set(ms.mid, mi.user.id, d)
                await mi.response.send_message("Saved.", ephemeral=(mi.guild_id is not None))
        await i.response.send_modal(M(msg_id))

    @discord.ui.button(label="3. Referral Source", style=discord.ButtonStyle.secondary, custom_id="app:refer")
    async def refer_btn(self, i: discord.Interaction, _: discord.ui.Button):
        msg_id = i.message.id
        class M(discord.ui.Modal, title="Referral Source"):
            def __init__(self, mid: int):
                super().__init__(timeout=None)
                self.mid = mid
                self.v = discord.ui.TextInput(label="Where did you hear about us?", style=discord.TextStyle.paragraph, required=True)
                self.add_item(self.v)
            async def on_submit(ms, mi: discord.Interaction):
                d = session_get(ms.mid) or {}
                d["refer"] = str(ms.v.value)
                session_set(ms.mid, mi.user.id, d)
                await mi.response.send_message("Saved.", ephemeral=(mi.guild_id is not None))
        await i.response.send_modal(M(msg_id))

    @discord.ui.select(placeholder="Select your branch",
                       options=[discord.SelectOption(label=x) for x in ["Army","Navy","Marines","Air Force","Coast Guard","Space Force","Family"]],
                       custom_id="app:branch")
    async def branch_sel(self, i: discord.Interaction, sel: discord.ui.Select):
        d = session_get(i.message.id) or {}
        d["branch_choice"] = sel.values[0]
        session_set(i.message.id, i.user.id, d)
        await i.response.defer()

    @discord.ui.select(placeholder="Select your status",
                       options=[discord.SelectOption(label=x) for x in ["Current","Former","DEP/Future Warrior"]],
                       custom_id="app:status")
    async def status_sel(self, i: discord.Interaction, sel: discord.ui.Select):
        d = session_get(i.message.id) or {}
        d["status_choice"] = sel.values[0]
        session_set(i.message.id, i.user.id, d)
        await i.response.defer()

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.success, custom_id="app:submit")
    async def submit_btn(self, i: discord.Interaction, _: discord.ui.Button):
        try:
            data = session_get(i.message.id) or {}
            missing = [k for k in ("name","pronouns","refer","branch_choice","status_choice") if not data.get(k)]
            if missing:
                await i.response.send_message("Missing: " + ", ".join(m.replace("_"," ") for m in missing), ephemeral=False)
                return

            gid = int(data.get("guild_id") or HOME_GUILD_ID or 0)
            guild = i.client.get_guild(gid) if gid else None
            if not guild:
                await i.response.send_message("Setup error: guild not found.", ephemeral=False)
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
            embed = discord.Embed(title="New Application", color=discord.Color.blue())
            embed.add_field(name="Preferred Name", value=data["name"], inline=False)
            embed.add_field(name="Pronouns", value=data["pronouns"], inline=False)
            embed.add_field(name="Branch", value=data["branch_choice"], inline=False)
            embed.add_field(name="Status", value=data["status_choice"], inline=False)
            embed.add_field(name="Referral Source", value=data["refer"], inline=False)
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

            for c in self.children:
                c.disabled = True
            await i.message.edit(view=self)
            await i.response.send_message("Submitted.", ephemeral=False)

        except Exception as e:
                                                                
            if not i.response.is_done():
                await i.response.send_message("Unexpected error submitting. Try again.", ephemeral=False)
            else:
                await i.followup.send("Unexpected error submitting. Try again.", ephemeral=False)
            raise


class ApplicationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.primary, custom_id="app:open")
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button):
        if has_submitted(interaction.user.id):
            await interaction.response.send_message("You already submitted.", ephemeral=True)
            return
        if not is_captcha_verified(interaction.user.id):
            await interaction.response.send_message(
                "You must complete human verification before applying. "
                "Check your DMs for the verification message, or ask staff for help.",
                ephemeral=True,
            )
            return
        try:
            dm = await interaction.user.create_dm()
            msg = await dm.send("Let's begin your application!", view=ApplicationFormView())
                             
            session_set(msg.id, interaction.user.id, {"guild_id": interaction.guild.id})
            await interaction.response.send_message("Check your DMs.", ephemeral=True)
        except discord.Forbidden:
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

    async def _decide(self, interaction: discord.Interaction, approved: bool):
        original_message_id = interaction.message.id if interaction.message else self.review_msg_id
        original_guild = interaction.guild

        class Reason(discord.ui.Modal, title=("Approval Reason" if approved else "Denial Reason")):
            reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph)
            async def on_submit(ms, mi: discord.Interaction):
                try:
                    guild = mi.guild or original_guild
                    if guild is None:
                        await send_application_error(mi, "Guild not found for this application."); return
                    data = self._load_application_data(original_message_id)
                    member = guild.get_member(self.applicant_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(self.applicant_id)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            member = None
                    if not member:
                        await send_application_error(mi, "User not found."); return
                    reviewer = str(mi.user)
                    bot_ref = mi.client
                    if not isinstance(bot_ref, commands.Bot):
                        raise RuntimeError("Bot reference unavailable for application decision.")
                    await process_application_decision(
                        bot_ref,
                        applicant_id=member.id,
                        approved=approved,
                        reviewer_name=reviewer,
                        reason=ms.reason.value,
                        guild=guild,
                        member=member,
                        review_message_id=original_message_id,
                        application_data=data,
                    )

                    for c in self.children: c.disabled = True
                    try:
                        await mi.message.edit(view=self)
                    except discord.HTTPException as exc:
                        if getattr(exc, "code", None) != 50005:
                            raise
                        
                    await mi.response.send_message(
                        f"Application {'approved' if approved else 'denied'} for {member.mention}.", ephemeral=True)
                except Exception as exc:
                    log_application_error("application_decision_modal", exc)
                    await send_application_error(mi, "An error occurred handling this decision. See application_errors.log for details.")
        await interaction.response.send_modal(Reason())

    async def _ticket(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(self.applicant_id)
        if not member:
            await interaction.response.send_message("User not found.", ephemeral=True); return
        category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Ticket category missing.", ephemeral=True); return
        staff = discord.utils.get(interaction.guild.roles, name="Staff")
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True),
        }
        if staff: overwrites[staff] = discord.PermissionOverwrite(view_channel=True)
        ch = await interaction.guild.create_text_channel(
            name=f"ticket-{member.name}", category=category, overwrites=overwrites,
            topic=f"Application of {member.display_name}")
        ticket_msg = await ch.send(f"{member.mention}, a staff member will assist you shortly.", view=TicketCloseView())
        await interaction.response.send_message(f"Ticket created: {ch.mention}", ephemeral=True)
        record_ticket_message(ch.id, ticket_msg.id)

class TicketCloseView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close_button")
    async def close_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        class Close(discord.ui.Modal, title="Close Ticket"):
            reason = discord.ui.TextInput(label="Reason for closing", style=discord.TextStyle.paragraph, required=True)
            async def on_submit(ms, mi: discord.Interaction):
                messages = [m async for m in interaction.channel.history(limit=1000, oldest_first=True)]
                rows = [{"Author": f"{m.author} ({m.author.id})", "Content": m.content,
                         "Time": m.created_at.isoformat()} for m in messages if not m.author.bot]
                buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=["Author","Content","Time"])
                w.writeheader(); w.writerows(rows); buf.seek(0)
                file = discord.File(io.BytesIO(buf.read().encode()),
                                    filename=f"{str(mi.user).replace('#','_')}_ticket_log.csv")
                log = interaction.guild.get_channel(TICKET_LOG_CHANNEL_ID)
                if log: await log.send(f"Ticket closed by {mi.user.mention}\nReason: {ms.reason.value}", file=file)
                await mi.response.send_message("Ticket closed. This channel will now self destruct", ephemeral=True)
                await asyncio.sleep(2); await interaction.channel.delete()
        await interaction.response.send_modal(Close())

async def refresh_ticket_views(bot: commands.Bot):
    """Ensure every tracked ticket message keeps an active close button view."""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT message_id, channel_id FROM tickets").fetchall()
    if not rows:
        return
    bot_id = bot.user.id if bot.user else None
    for stored_message_id, channel_id in rows:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
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
                    if candidate.components or TICKET_GREETING_PHRASE in content:
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

           
class Applications(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()

    async def cog_load(self):
        self.bot.add_view(ApplicationView())
        self.bot.add_view(ApplicationFormView())
        self.bot.add_view(TicketCloseView())

    @app_commands.command(name="app_repost", description="Repost the application button")
    @app_commands.describe(channel="Target channel")
    async def app_repost(self, interaction: Interaction, channel: discord.TextChannel | None = None):
        ch = channel or interaction.channel
        await ch.send("Let's begin your application!", view=ApplicationView())
        await interaction.response.send_message("Posted.", ephemeral=True)

    @app_commands.command(name="refreshview", description="Re-attach review buttons to a staff message")
    @app_commands.describe(message_id="Staff-review message ID")
    async def refreshview(self, interaction: Interaction, message_id: str):
        try: mid = int(message_id)
        except ValueError:
            await interaction.response.send_message("Invalid message ID.", ephemeral=True); return
        ch = interaction.client.get_channel(STAFF_REVIEW_CHANNEL_ID)
        if not ch:
            await interaction.response.send_message("Channel not found.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        with sqlite3.connect(DB_PATH) as con:
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

    @app_commands.command(name="list_pending", description="List pending application message IDs")
    async def list_pending(self, interaction: Interaction):
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute("SELECT message_id,user_id FROM pending_applications ORDER BY message_id DESC").fetchall()
        if not rows:
            await interaction.response.send_message("No pending applications.", ephemeral=True); return
        await interaction.response.send_message("\n".join(f"{m} — {u}" for m,u in rows[:50]), ephemeral=True)

    @app_commands.command(name="remove_pending", description="Remove a pending application by message ID")
    @app_commands.describe(message_id="Staff-review message ID")
    async def remove_pending(self, interaction: Interaction, message_id: str):
        try: mid = int(message_id)
        except ValueError:
            await interaction.response.send_message("Invalid message ID.", ephemeral=True); return
        delete_pending(message_id=mid)
        await interaction.response.send_message("Removed.", ephemeral=True)

    async def send_followup_reminders(self):
        if not APPLICATION_FOLLOWUP_CATEGORY_ID:
            return 0, 0
        category = self.bot.get_channel(APPLICATION_FOLLOWUP_CATEGORY_ID)
        if category is None:
            try:
                fetched = await self.bot.fetch_channel(APPLICATION_FOLLOWUP_CATEGORY_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return 0, 0
            category = fetched
        if not isinstance(category, discord.CategoryChannel):
            return 0, 0

        now = discord.utils.utcnow()
        guild = category.guild
        me = None
        if guild:
            if self.bot.user:
                me = guild.get_member(self.bot.user.id)
            if me is None:
                me = guild.me

        processed = 0
        reminders_sent = 0

        for channel in category.channels:
            if not isinstance(channel, discord.TextChannel):
                continue
            processed += 1
            if me:
                perms = channel.permissions_for(me)
                if not perms.send_messages:
                    continue
            last_message = None
            if channel.last_message_id:
                try:
                    last_message = await channel.fetch_message(channel.last_message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    last_message = None
            if last_message is None:
                try:
                    async for msg in channel.history(limit=1):
                        last_message = msg
                        break
                except (discord.Forbidden, discord.HTTPException):
                    continue
            if last_message is None:
                continue
            if (
                self.bot.user
                and last_message.author.id == self.bot.user.id
                and last_message.content.strip() == FOLLOWUP_MESSAGE_TEXT
            ):
                continue
            if now - last_message.created_at >= FOLLOWUP_DELAY:
                try:
                    await channel.send(FOLLOWUP_MESSAGE_TEXT)
                    reminders_sent += 1
                except (discord.Forbidden, discord.HTTPException):
                    continue
                await asyncio.sleep(1.0)
        return processed, reminders_sent

    @app_commands.command(name="ticketremind", description="Send follow-up reminders to inactive application tickets")
    async def ticketremind(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        processed, reminders = await self.send_followup_reminders()
        if processed == 0:
            await interaction.followup.send("No application channels were checked. Verify the category ID.", ephemeral=True)
            return
        if reminders == 0:
            await interaction.followup.send(f"Checked {processed} channels; nothing needed a reminder.", ephemeral=True)
            return
        await interaction.followup.send(f"Checked {processed} channels and sent {reminders} reminder(s).", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Applications(bot))
