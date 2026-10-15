"""Anonymous feedback panels.

A staff member posts a panel into a channel; anyone can press its button and
send feedback through a modal. The submission is forwarded to a destination
channel with no attribution of any kind.

ANONYMITY MODEL - read before changing anything in here.

Submissions are anonymous *to everyone in Discord*, including staff. They are
NOT anonymous to whoever can read the server's filesystem: every submission is
written, with its author, to the audit log defined below. That is deliberate,
so abuse can still be dealt with. The panel wording says "staff will not see
who sent this" rather than "completely anonymous" for exactly this reason.

Four things keep the Discord side anonymous. Each was verified against this
codebase; none is incidental:

1. Submission never travels through an application command. bot.py's
   on_interaction only logs `InteractionType.application_command`, so button
   and modal-submit interactions are not written to data/command_log.json.
   Never add a slash command that submits feedback - it would be logged.

2. The destination message is sent with channel.send(), never with
   interaction.response/followup. Discord stamps every interaction response
   with `interaction_metadata`, which carries the invoking user server-side and
   is readable by any bot or API client. The ephemeral acknowledgement is the
   only interaction response we make, and being ephemeral it cannot be fetched
   by anyone else.

3. The audit logger below sets propagate=False. The root logger carries
   utils.bug_reporter.BugReporterHandler, which re-broadcasts ERROR message
   text into a Discord channel after five repeats; propagate=False means an
   audit line can never reach it. Never log identity or feedback text through
   the root logger, and never at ERROR level.

4. The forward is an embed with empty message content. commands/keyword_alerts
   has an on_message listener with no bot-author guard - its only guard is
   `if not m.guild or not m.content`. Empty content makes it return early
   instead of re-broadcasting the feedback into another channel.

Nothing about a submitter is ever written to the database. Hashing would not
help: the guild member set is small and enumerable, so any deterministic
identifier can be brute-forced back to a person in milliseconds.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import discord
from discord import Interaction, app_commands
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")
DB_PATH = str(Path(os.getenv("FEEDBACK_DB_PATH") or (BASE_DIR / "data" / "feedback_panels.db")))

AUDIT_LOG_PATH = Path(os.getenv("FEEDBACK_AUDIT_LOG") or (BASE_DIR / "feedback_audit.log"))
audit_log = logging.getLogger("rentron.feedback.audit")


def _init_audit_log() -> None:
    if audit_log.handlers:
        return
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(AUDIT_LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        audit_log.addHandler(handler)
        try:
            os.chmod(AUDIT_LOG_PATH, 0o600)
        except OSError:
            pass
    except Exception:
        audit_log.addHandler(logging.NullHandler())


_init_audit_log()

DEFAULT_TITLE = "Anonymous Feedback"
DEFAULT_PROMPT = (
    "Use the button below to send feedback to the staff team.\n\n"
    "**Staff will not see who sent this.** Your name, avatar and ID are not "
    "attached to your message and are not shown to anyone reviewing it.\n\n"
    "Please don't include details that identify you unless you want to be "
    "identified — writing style and specific incidents can give you away even "
    "without a name."
)
DEFAULT_BUTTON_LABEL = "Send anonymous feedback"

MIN_LENGTH_DEFAULT = 15
MAX_LENGTH = 2000

DELAY_MODES = {
    "none": (0, 0),
    "short": (60, 600),
    "long": (900, 5400),
}

RATE_WINDOW_SECONDS = 600
PANEL_WINDOW_CAP = 30
DEDUPE_TTL_SECONDS = 3600

_member_windows: dict[int, tuple[int, int]] = {}
_panel_windows: dict[int, tuple[int, int]] = {}
_recent_hashes: dict[str, float] = {}
_pending_tasks: set[asyncio.Task] = set()

MENTION_RE = re.compile(r"<@[!&]?(\d+)>")
EVERYONE_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)


@contextmanager
def _connect(path):
    con = sqlite3.connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


def ensure_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect(DB_PATH) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS feedback_panels(
                message_id        INTEGER PRIMARY KEY,
                guild_id          INTEGER NOT NULL,
                channel_id        INTEGER NOT NULL,
                target_channel_id INTEGER NOT NULL,
                config            TEXT    NOT NULL,
                created_at        TEXT    NOT NULL)"""
        )
        con.commit()


def save_panel(message_id: int, guild_id: int, channel_id: int, target_channel_id: int, config: dict) -> None:
    with _connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO feedback_panels"
            "(message_id, guild_id, channel_id, target_channel_id, config, created_at) "
            "VALUES(?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (int(message_id), int(guild_id), int(channel_id), int(target_channel_id), json.dumps(config)),
        )
        con.commit()


def get_panel(message_id: int) -> dict | None:
    with _connect(DB_PATH) as con:
        row = con.execute(
            "SELECT message_id, guild_id, channel_id, target_channel_id, config, created_at "
            "FROM feedback_panels WHERE message_id=?",
            (int(message_id),),
        ).fetchone()
    if not row:
        return None
    try:
        config = json.loads(row[4])
    except Exception:
        config = {}
    return {
        "message_id": row[0], "guild_id": row[1], "channel_id": row[2],
        "target_channel_id": row[3], "config": config, "created_at": row[5],
    }


def list_panels(guild_id: int) -> list[dict]:
    with _connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT message_id FROM feedback_panels WHERE guild_id=? ORDER BY message_id DESC",
            (int(guild_id),),
        ).fetchall()
    return [p for p in (get_panel(r[0]) for r in rows) if p]


def delete_panel(message_id: int) -> None:
    with _connect(DB_PATH) as con:
        con.execute("DELETE FROM feedback_panels WHERE message_id=?", (int(message_id),))
        con.commit()


def set_panel_enabled(message_id: int, enabled: bool) -> bool:
    panel = get_panel(message_id)
    if not panel:
        return False
    panel["config"]["enabled"] = bool(enabled)
    save_panel(panel["message_id"], panel["guild_id"], panel["channel_id"],
               panel["target_channel_id"], panel["config"])
    return True


def sanitise(text: str) -> str:
    """Neutralise pings. An unaccountable @everyone is the classic abuse here."""
    text = EVERYONE_RE.sub(lambda m: "@​" + m.group(1), text)
    text = MENTION_RE.sub(lambda m: f"[user {m.group(1)}]", text)
    return text.strip()


def _window_index(now: float) -> int:
    return int(now // RATE_WINDOW_SECONDS)


def _check_and_count(store: dict, key: int, cap: int, now: float) -> bool:
    """Sliding-ish window counter. Returns True if the caller is under the cap."""
    idx = _window_index(now)
    for stale in [k for k, (w, _) in store.items() if w < idx - 1]:
        store.pop(stale, None)
    window, count = store.get(key, (idx, 0))
    if window != idx:
        window, count = idx, 0
    if count >= cap:
        return False
    store[key] = (window, count + 1)
    return True


def _refund(store: dict, key: int) -> None:
    entry = store.get(key)
    if entry and entry[1] > 0:
        store[key] = (entry[0], entry[1] - 1)


def _is_duplicate(text: str, now: float) -> bool:
    for stale in [k for k, exp in _recent_hashes.items() if exp < now]:
        _recent_hashes.pop(stale, None)
    digest = hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:16]
    if digest in _recent_hashes:
        return True
    _recent_hashes[digest] = now + DEDUPE_TTL_SECONDS
    return False


async def deliver_feedback(bot: commands.Bot, target_channel_id: int, title: str, text: str) -> bool:
    """Post to the destination channel. Never via an interaction response."""
    channel = bot.get_channel(int(target_channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(target_channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    embed = discord.Embed(title=title, description=text[:4096], color=discord.Color.blurple())

    for attempt in range(3):
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.HTTPException as exc:
            retry_after = getattr(exc, "retry_after", None)
            if retry_after and attempt < 2:
                await asyncio.sleep(float(retry_after) + 0.5)
                continue
            if attempt < 2 and getattr(exc, "status", 0) >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            return False
        except discord.Forbidden:
            return False
    return False


def _schedule_delivery(bot: commands.Bot, panel: dict, text: str, delay: float) -> None:
    async def runner():
        try:
            if delay:
                await asyncio.sleep(delay)
            ok = await deliver_feedback(
                bot, panel["target_channel_id"],
                panel["config"].get("title") or DEFAULT_TITLE, text,
            )
            if not ok:
                audit_log.info("DELIVERY FAILED panel=%s target=%s", panel["message_id"], panel["target_channel_id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            audit_log.exception("DELIVERY ERROR panel=%s", panel["message_id"])

    task = asyncio.create_task(runner())
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


class FeedbackModal(discord.ui.Modal):
    def __init__(self, panel: dict):
        config = panel["config"]
        super().__init__(title=(config.get("title") or DEFAULT_TITLE)[:45], timeout=None)
        self.panel = panel
        self.body = discord.ui.TextInput(
            label="Your feedback",
            style=discord.TextStyle.paragraph,
            required=True,
            min_length=int(config.get("min_length") or MIN_LENGTH_DEFAULT),
            max_length=MAX_LENGTH,
            placeholder="Staff will not see who sent this.",
        )
        self.add_item(self.body)

    async def on_error(self, interaction: Interaction, error: Exception) -> None:
        audit_log.warning("MODAL ERROR (details suppressed to protect the submitter)")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Something went wrong sending that. Please try again.", ephemeral=True
                )
        except discord.HTTPException:
            pass

    async def on_submit(self, interaction: Interaction) -> None:
        try:
            raw = str(self.body.value or "")
            text = sanitise(raw)
            now = discord.utils.utcnow().timestamp()

            if not text:
                await interaction.response.send_message(
                    "That looked empty once links and pings were removed. Try again.", ephemeral=True
                )
                return

            if _is_duplicate(text, now):
                await interaction.response.send_message(
                    "That's identical to something submitted recently, so it wasn't sent again.",
                    ephemeral=True,
                )
                return

            if not _check_and_count(_panel_windows, int(self.panel["message_id"]), PANEL_WINDOW_CAP, now):
                await interaction.response.send_message(
                    "This feedback box is busy right now. Please try again in a few minutes.",
                    ephemeral=True,
                )
                return

            mode = self.panel["config"].get("delay_mode") or "none"
            low, high = DELAY_MODES.get(mode, DELAY_MODES["short"])
            delay = random.uniform(low, high) if high else 0.0

            await interaction.response.send_message(
                "Thank you — your feedback has been sent anonymously."
                + ("\nIt is delivered on a delay so it can't be linked back to you." if delay else ""),
                ephemeral=True,
            )

            audit_log.info(
                "SUBMISSION panel=%s guild=%s channel=%s user=%s id=%s chars=%s\n    %s",
                self.panel["message_id"], self.panel["guild_id"], self.panel["channel_id"],
                interaction.user, interaction.user.id, len(text),
                text.replace("\n", "\n    "),
            )

            _schedule_delivery(interaction.client, self.panel, text, delay)

        except Exception:
            _refund(_panel_windows, int(self.panel["message_id"]))
            audit_log.warning("SUBMIT FAILED (details suppressed to protect the submitter)")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Something went wrong sending that. Please try again.", ephemeral=True
                    )
            except discord.HTTPException:
                pass


class FeedbackPanelView(discord.ui.View):
    """Persistent, shared across every panel.

    One registered instance serves all panels, so it must hold no per-panel
    state - the panel is looked up from interaction.message.id on each click.
    """

    def __init__(self, label: str | None = None):
        super().__init__(timeout=None)
        if label:
            self.open_feedback.label = label[:80]

    @discord.ui.button(
        label=DEFAULT_BUTTON_LABEL,
        style=discord.ButtonStyle.primary,
        custom_id="feedback:open",
    )
    async def open_feedback(self, interaction: Interaction, _: discord.ui.Button):
        panel = get_panel(interaction.message.id) if interaction.message else None
        if panel is None:
            await interaction.response.send_message(
                "This feedback panel is no longer active.", ephemeral=True
            )
            return
        if not panel["config"].get("enabled", True):
            await interaction.response.send_message(
                "This feedback box is closed at the moment.", ephemeral=True
            )
            return

        now = discord.utils.utcnow().timestamp()
        cooldown = int(panel["config"].get("cooldown_seconds") or 0)
        if cooldown:
            per_window = max(1, RATE_WINDOW_SECONDS // cooldown)
            if not _check_and_count(_member_windows, interaction.user.id, per_window, now):
                await interaction.response.send_message(
                    "You've sent a few already — please wait a little while before sending more.",
                    ephemeral=True,
                )
                return

        await interaction.response.send_modal(FeedbackModal(panel))


def _disabled_view(label: str | None = None) -> FeedbackPanelView:
    view = FeedbackPanelView(label)
    for child in view.children:
        child.disabled = True
    return view


class FeedbackPanels(commands.Cog):
    panel = app_commands.Group(
        name="feedback_panel",
        description="Anonymous feedback boxes",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        ensure_db()

    async def cog_load(self):
        self.bot.add_view(FeedbackPanelView())

    @panel.command(name="create", description="Post an anonymous feedback box in this channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        destination="Channel where submissions are posted (must not be this channel)",
        title="Heading on the panel and on each submission",
        prompt="Body text explaining the panel",
        button_label="Text on the button",
        cooldown_minutes="Minimum gap between submissions from one person (default 10)",
        delay="How long to hold a submission before posting, to prevent timing correlation",
        min_length="Shortest accepted submission (default 15 characters)",
    )
    @app_commands.choices(delay=[
        app_commands.Choice(name="none — post immediately (default)", value="none"),
        app_commands.Choice(name="short — 1 to 10 minutes", value="short"),
        app_commands.Choice(name="long — 15 to 90 minutes", value="long"),
    ])
    async def panel_create(
        self,
        interaction: Interaction,
        destination: discord.TextChannel,
        title: str | None = None,
        prompt: str | None = None,
        button_label: str | None = None,
        cooldown_minutes: app_commands.Range[int, 0, 1440] = 10,
        delay: app_commands.Choice[str] | None = None,
        min_length: app_commands.Range[int, 1, 500] = MIN_LENGTH_DEFAULT,
    ):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("Run this in a server.", ephemeral=True)
            return

        if destination.id == interaction.channel.id:
            await interaction.followup.send(
                "The destination must be a different channel — otherwise everyone who can "
                "use the panel can also read the submissions.",
                ephemeral=True,
            )
            return

        me = interaction.guild.me
        if me and not destination.permissions_for(me).send_messages:
            await interaction.followup.send(
                f"I can't post in {destination.mention}. Give me Send Messages there first.",
                ephemeral=True,
            )
            return

        config = {
            "title": (title or DEFAULT_TITLE)[:200],
            "prompt": (prompt or DEFAULT_PROMPT)[:3000],
            "button_label": (button_label or DEFAULT_BUTTON_LABEL)[:80],
            "enabled": True,
            "cooldown_seconds": int(cooldown_minutes) * 60,
            "delay_mode": (delay.value if delay else "none"),
            "min_length": int(min_length),
        }

        embed = discord.Embed(
            title=config["title"], description=config["prompt"], color=discord.Color.blurple()
        )
        view = FeedbackPanelView(config["button_label"])
        message = await interaction.channel.send(embed=embed, view=view)
        save_panel(message.id, interaction.guild.id, interaction.channel.id, destination.id, config)

        readers = sum(1 for m in getattr(interaction.channel, "members", []) if not m.bot)
        warning = ""
        if destination.permissions_for(interaction.guild.default_role).read_messages:
            warning = f"\n⚠️ {destination.mention} is readable by @everyone — submissions will be public."

        await interaction.followup.send(
            f"Panel posted. Submissions go to {destination.mention}.\n"
            f"Panel message ID `{message.id}` (needed to close it).\n"
            f"Anonymity set: about **{readers}** people can use this panel — that is the most "
            f"anyone could be hidden among.\n"
            f"Submissions are logged with their author to `{AUDIT_LOG_PATH.name}` on the server, "
            f"readable only with filesystem access." + warning,
            ephemeral=True,
        )

    @panel.command(name="close", description="Close a feedback panel (reopenable)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(message_id="The panel's message ID", reopen="Reopen it instead of closing")
    async def panel_close(self, interaction: Interaction, message_id: str, reopen: bool = False):
        await interaction.response.defer(ephemeral=True)
        try:
            mid = int(message_id)
        except ValueError:
            await interaction.followup.send("That isn't a valid message ID.", ephemeral=True)
            return

        panel = get_panel(mid)
        if not panel:
            await interaction.followup.send("No panel with that message ID.", ephemeral=True)
            return

        set_panel_enabled(mid, reopen)
        label = panel["config"].get("button_label")
        channel = self.bot.get_channel(panel["channel_id"])
        if channel:
            try:
                message = await channel.fetch_message(mid)
                await message.edit(view=FeedbackPanelView(label) if reopen else _disabled_view(label))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await interaction.followup.send(
            f"Panel {'reopened' if reopen else 'closed'}. Nothing was deleted.", ephemeral=True
        )

    @panel.command(name="list", description="List the feedback panels in this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panel_list(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("Run this in a server.", ephemeral=True)
            return
        panels = list_panels(interaction.guild.id)
        if not panels:
            await interaction.followup.send("No feedback panels in this server.", ephemeral=True)
            return
        lines = []
        for p in panels:
            state = "open" if p["config"].get("enabled", True) else "closed"
            lines.append(
                f"`{p['message_id']}` — in <#{p['channel_id']}> → <#{p['target_channel_id']}> "
                f"({state}, delay: {p['config'].get('delay_mode', 'short')})"
            )
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(FeedbackPanels(bot))
