from __future__ import annotations

import json
import io
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import Interaction, app_commands
from discord.ext import commands, tasks

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "drills.db"
CHECK_INTERVAL_SECONDS = 10
DATA_RETENTION_DAYS = 30
DEFAULT_DURATION_MINUTES = 30

DEFAULT_CONSTRAINTS = [
    "enforcement disabled",
    "limited visibility",
    "delayed information",
    "simulated permissions only",
]

DEFAULT_SUCCESS_CRITERIA = [
    "containment",
    "documentation",
    "internal communication",
    "escalation clarity",
]

SCENARIO_CHOICES = [
    app_commands.Choice(name="raid", value="raid"),
    app_commands.Choice(name="compromised_staff", value="compromised_staff"),
    app_commands.Choice(name="doxx_threat", value="doxx_threat"),
    app_commands.Choice(name="mass_report_abuse", value="mass_report_abuse"),
    app_commands.Choice(name="platform_brigade", value="platform_brigade"),
]

_USER_MENTION_RE = re.compile(r"<@!?\d+>")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS drills(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                thread_id INTEGER,
                scenario_type TEXT NOT NULL,
                constraints TEXT NOT NULL,
                success_criteria TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                ended_at TEXT,
                snapshot_json TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS drill_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drill_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                content TEXT NOT NULL,
                author_role_id INTEGER,
                author_role_name TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(drill_id) REFERENCES drills(id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS drill_injections(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drill_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                send_at TEXT NOT NULL,
                delivered_at TEXT,
                FOREIGN KEY(drill_id) REFERENCES drills(id)
            )
            """
        )
        con.commit()


def purge_old_drills() -> None:
    cutoff = (_utc_now() - timedelta(days=DATA_RETENTION_DAYS)).isoformat()
    with sqlite3.connect(DB_PATH) as con:
        old_ids = [
            int(row[0])
            for row in con.execute(
                "SELECT id FROM drills WHERE status='ended' AND ended_at IS NOT NULL AND ended_at < ?",
                (cutoff,),
            ).fetchall()
        ]
        if not old_ids:
            return
        con.executemany("DELETE FROM drill_events WHERE drill_id=?", [(drill_id,) for drill_id in old_ids])
        con.executemany("DELETE FROM drill_injections WHERE drill_id=?", [(drill_id,) for drill_id in old_ids])
        con.executemany("DELETE FROM drills WHERE id=?", [(drill_id,) for drill_id in old_ids])
        con.commit()


def _split_list(raw: str, fallback: list[str]) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return list(fallback)
    parts = re.split(r"[;\n]", text)
    cleaned = [p.strip(" -\t") for p in parts if p.strip(" -\t")]
    return cleaned or list(fallback)


def _format_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _format_delta(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _is_staff(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return bool(
        perms.administrator
        or perms.manage_guild
        or perms.manage_messages
        or perms.kick_members
        or perms.ban_members
    )


def _role_info(member: discord.Member) -> tuple[Optional[int], Optional[str]]:
    roles = [role for role in getattr(member, "roles", []) if not getattr(role, "is_default", lambda: False)()]
    if not roles:
        return None, None
    top_role = max(roles, key=lambda r: r.position)
    return top_role.id, top_role.name


def _redact_user_mentions(text: str) -> str:
    return _USER_MENTION_RE.sub("@user", text or "")


def _snapshot_guild(guild: discord.Guild) -> dict:
    roles = []
    for role in guild.roles:
        roles.append(
            {
                "id": role.id,
                "name": role.name,
                "position": role.position,
                "permissions": role.permissions.value,
                "managed": role.managed,
                "mentionable": role.mentionable,
                "hoist": role.hoist,
            }
        )

    channels = []
    for channel in guild.channels:
        overwrites = []
        for target, overwrite in channel.overwrites.items():
            allow, deny = overwrite.pair()
            overwrites.append(
                {
                    "target_type": "role" if isinstance(target, discord.Role) else "member",
                    "target_id": target.id,
                    "allow": allow.value,
                    "deny": deny.value,
                }
            )
        channels.append(
            {
                "id": channel.id,
                "name": channel.name,
                "type": str(channel.type),
                "category_id": getattr(channel.category, "id", None),
                "position": channel.position,
                "overwrites": overwrites,
            }
        )

    mod_roles = []
    for role in guild.roles:
        perms = role.permissions
        if perms.administrator or perms.manage_guild or perms.manage_messages or perms.kick_members or perms.ban_members:
            mod_roles.append(role.id)

    mod_count = sum(
        1
        for member in guild.members
        if member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
        or member.guild_permissions.manage_messages
        or member.guild_permissions.kick_members
        or member.guild_permissions.ban_members
    )

    return {
        "captured_at": _utc_now().isoformat(),
        "roles": roles,
        "channels": channels,
        "mod_roster": {"role_ids": mod_roles, "member_count": mod_count},
    }


def _fetch_active_drill(guild_id: int) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            """
            SELECT id, guild_id, channel_id, thread_id, scenario_type, constraints, success_criteria,
                   status, started_at, ends_at, ended_at, snapshot_json
            FROM drills
            WHERE guild_id=? AND status='active'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (guild_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "guild_id": int(row[1]),
        "channel_id": int(row[2]),
        "thread_id": int(row[3]) if row[3] is not None else None,
        "scenario_type": str(row[4]),
        "constraints": str(row[5]),
        "success_criteria": str(row[6]),
        "status": str(row[7]),
        "started_at": str(row[8]),
        "ends_at": str(row[9]),
        "ended_at": str(row[10]) if row[10] else None,
        "snapshot_json": str(row[11]),
    }


def _fetch_drill_by_id(guild_id: int, drill_id: Optional[int]) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as con:
        if drill_id is None:
            row = con.execute(
                """
                SELECT id, guild_id, channel_id, thread_id, scenario_type, constraints, success_criteria,
                       status, started_at, ends_at, ended_at, snapshot_json
                FROM drills
                WHERE guild_id=?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (guild_id,),
            ).fetchone()
        else:
            row = con.execute(
                """
                SELECT id, guild_id, channel_id, thread_id, scenario_type, constraints, success_criteria,
                       status, started_at, ends_at, ended_at, snapshot_json
                FROM drills
                WHERE guild_id=? AND id=?
                """,
                (guild_id, drill_id),
            ).fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "guild_id": int(row[1]),
        "channel_id": int(row[2]),
        "thread_id": int(row[3]) if row[3] is not None else None,
        "scenario_type": str(row[4]),
        "constraints": str(row[5]),
        "success_criteria": str(row[6]),
        "status": str(row[7]),
        "started_at": str(row[8]),
        "ends_at": str(row[9]),
        "ended_at": str(row[10]) if row[10] else None,
        "snapshot_json": str(row[11]),
    }


def _add_event(
    drill_id: int,
    event_type: str,
    content: str,
    role_id: Optional[int],
    role_name: Optional[str],
    when: Optional[datetime] = None,
) -> None:
    stamp = (when or _utc_now()).isoformat()
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO drill_events(drill_id, event_type, content, author_role_id, author_role_name, created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (drill_id, event_type, content, role_id, role_name, stamp),
        )
        con.commit()


class DrillCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        ensure_db()
        purge_old_drills()
        self.injection_loop.start()

    def cog_unload(self) -> None:
        if self.injection_loop.is_running():
            self.injection_loop.cancel()

    drill = app_commands.Group(name="drill", description="Incident rehearsal drills")

    async def _resolve_drill_channel(
        self, interaction: Interaction
    ) -> tuple[Optional[discord.abc.Messageable], Optional[str], Optional[int]]:
        if not interaction.guild:
            return None, "Use this command inside a server.", None
        base_channel = interaction.channel
        if isinstance(base_channel, discord.Thread):
            return base_channel, None, base_channel.id
        if not isinstance(base_channel, discord.TextChannel):
            return None, "Run drills from a text channel or staff thread.", None

        everyone_role = interaction.guild.default_role
        if not base_channel.permissions_for(everyone_role).view_channel:
            return base_channel, None, None

        bot_member = interaction.guild.me or interaction.guild.get_member(self.bot.user.id)
        if not bot_member:
            return None, "Bot member not available.", None
        if not base_channel.permissions_for(bot_member).create_private_threads:
            return None, "Use a staff-only channel or grant the bot permission to create private threads.", None

        thread = await base_channel.create_thread(
            name=f"drill-{interaction.user.display_name}-{int(_utc_now().timestamp())}",
            type=discord.ChannelType.private_thread,
            invitable=True,
        )
        await thread.add_user(interaction.user)
        return thread, "Private staff thread created. Invite other staff as needed.", thread.id

    @drill.command(name="start", description="Start a time-boxed incident rehearsal drill.")
    @app_commands.describe(
        scenario_type="Scenario type for this drill",
        duration_minutes="Drill duration in minutes (default 30)",
        constraints="Optional constraints, separated by semicolons or new lines",
        success_criteria="Optional success criteria, separated by semicolons or new lines",
    )
    @app_commands.choices(scenario_type=SCENARIO_CHOICES)
    async def drill_start(
        self,
        interaction: Interaction,
        scenario_type: app_commands.Choice[str],
        duration_minutes: app_commands.Range[int, 10, 240] = DEFAULT_DURATION_MINUTES,
        constraints: str = "",
        success_criteria: str = "",
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return
        if not _is_staff(interaction.user):
            await interaction.response.send_message("This command is for staff only.", ephemeral=True)
            return

        active = _fetch_active_drill(interaction.guild.id)
        if active:
            await interaction.response.send_message(
                f"A drill is already active (ID {active['id']}). End it before starting another.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        target_channel, error, thread_id = await self._resolve_drill_channel(interaction)
        if error or target_channel is None:
            await interaction.followup.send(error or "Unable to start drill.", ephemeral=True)
            return

        constraint_list = _split_list(constraints, DEFAULT_CONSTRAINTS)
        success_list = _split_list(success_criteria, DEFAULT_SUCCESS_CRITERIA)
        started_at = _utc_now()
        ends_at = started_at + timedelta(minutes=duration_minutes)
        snapshot = _snapshot_guild(interaction.guild)

        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute(
                """
                INSERT INTO drills(
                    guild_id, channel_id, thread_id, scenario_type, constraints, success_criteria,
                    status, started_at, ends_at, snapshot_json
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    interaction.guild.id,
                    target_channel.id,
                    thread_id,
                    scenario_type.value,
                    json.dumps(constraint_list),
                    json.dumps(success_list),
                    "active",
                    started_at.isoformat(),
                    ends_at.isoformat(),
                    json.dumps(snapshot),
                ),
            )
            con.commit()
            drill_id = int(cur.lastrowid)

        role_id, role_name = _role_info(interaction.user)
        _add_event(drill_id, "system", "Drill started.", role_id, role_name, started_at)

        start_message = (
            f"**Drill started (ID {drill_id}) - {scenario_type.value}**\n"
            "Rehearsal only. The bot is a referee and a clock.\n\n"
            "**Constraints**\n"
            f"{_format_list(constraint_list)}\n\n"
            "**Success criteria**\n"
            f"{_format_list(success_list)}\n\n"
            f"**Timer** ends at {discord.utils.format_dt(ends_at, style='F')} "
            f"({discord.utils.format_dt(ends_at, style='R')})."
        )
        try:
            await target_channel.send(start_message)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Drill started, but the bot could not post the kickoff message in that channel.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Drill started in {target_channel.mention}. Data expires after {DATA_RETENTION_DAYS} days.",
            ephemeral=True,
        )

    @drill.command(name="inject", description="Inject new information into the active drill.")
    @app_commands.describe(
        message="Signal to inject",
        delay_minutes="Delay before injecting (0 = now)",
        random_window_minutes="Optional random delay added on top of delay",
    )
    async def drill_inject(
        self,
        interaction: Interaction,
        message: str,
        delay_minutes: app_commands.Range[int, 0, 180] = 0,
        random_window_minutes: app_commands.Range[int, 0, 60] = 0,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return
        if not _is_staff(interaction.user):
            await interaction.response.send_message("This command is for staff only.", ephemeral=True)
            return

        drill = _fetch_active_drill(interaction.guild.id)
        if not drill:
            await interaction.response.send_message("No active drill found. Run /drill start first.", ephemeral=True)
            return

        target_channel = self.bot.get_channel(drill["thread_id"] or drill["channel_id"])
        if target_channel is None:
            await interaction.response.send_message("Drill channel not found.", ephemeral=True)
            return

        delay = delay_minutes
        if random_window_minutes:
            delay += random.randint(0, random_window_minutes)
        send_at = _utc_now() + timedelta(minutes=delay)

        if delay <= 0:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await target_channel.send(f"**Inject:** {message}")
            except (discord.Forbidden, discord.HTTPException):
                await interaction.followup.send("Inject scheduled but delivery failed.", ephemeral=True)
                return
            _add_event(drill["id"], "inject", message, None, None, _utc_now())
            await interaction.followup.send("Inject delivered.", ephemeral=True)
            return

        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                """
                INSERT INTO drill_injections(drill_id, content, send_at)
                VALUES(?,?,?)
                """,
                (drill["id"], message, send_at.isoformat()),
            )
            con.commit()
        await interaction.response.send_message(
            f"Inject scheduled for {discord.utils.format_dt(send_at, style='F')} "
            f"({discord.utils.format_dt(send_at, style='R')}).",
            ephemeral=True,
        )

    @drill.command(name="action", description="Log a staff action without touching real systems.")
    @app_commands.describe(action="Action taken or decision made")
    async def drill_action(self, interaction: Interaction, action: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return
        if not _is_staff(interaction.user):
            await interaction.response.send_message("This command is for staff only.", ephemeral=True)
            return

        drill = _fetch_active_drill(interaction.guild.id)
        if not drill:
            await interaction.response.send_message("No active drill found. Run /drill start first.", ephemeral=True)
            return

        target_channel = self.bot.get_channel(drill["thread_id"] or drill["channel_id"])
        if target_channel is None:
            await interaction.response.send_message("Drill channel not found.", ephemeral=True)
            return

        role_id, role_name = _role_info(interaction.user)
        sanitized = _redact_user_mentions(action.strip())
        _add_event(drill["id"], "action", sanitized, role_id, role_name, _utc_now())
        try:
            await target_channel.send(f"**Action logged** ({role_name or 'staff'}): {sanitized}")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message("Action logged, but I could not post in the drill channel.", ephemeral=True)
            return
        await interaction.response.send_message("Action logged.", ephemeral=True)

    @drill.command(name="end", description="End the active drill and freeze the log.")
    async def drill_end(self, interaction: Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return
        if not _is_staff(interaction.user):
            await interaction.response.send_message("This command is for staff only.", ephemeral=True)
            return

        drill = _fetch_active_drill(interaction.guild.id)
        if not drill:
            await interaction.response.send_message("No active drill found.", ephemeral=True)
            return

        ended_at = _utc_now()
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "UPDATE drills SET status='ended', ended_at=? WHERE id=?",
                (ended_at.isoformat(), drill["id"]),
            )
            con.execute(
                "UPDATE drill_injections SET delivered_at=? WHERE drill_id=? AND delivered_at IS NULL",
                (ended_at.isoformat(), drill["id"]),
            )
            con.commit()

        role_id, role_name = _role_info(interaction.user)
        _add_event(drill["id"], "system", "Drill ended.", role_id, role_name, ended_at)

        target_channel = self.bot.get_channel(drill["thread_id"] or drill["channel_id"])
        if target_channel is not None:
            try:
                await target_channel.send("**Drill ended.** Log frozen. Use `/drill report` for the debrief.")
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.response.send_message("Drill ended.", ephemeral=True)

    @drill.command(name="report", description="Generate a structured debrief report for a drill.")
    @app_commands.describe(drill_id="Optional drill ID to report on (defaults to latest)")
    async def drill_report(self, interaction: Interaction, drill_id: Optional[int] = None) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return
        if not _is_staff(interaction.user):
            await interaction.response.send_message("This command is for staff only.", ephemeral=True)
            return

        drill = _fetch_drill_by_id(interaction.guild.id, drill_id)
        if not drill:
            await interaction.response.send_message("No drill found for this server.", ephemeral=True)
            return

        with sqlite3.connect(DB_PATH) as con:
            events = con.execute(
                """
                SELECT event_type, content, author_role_name, created_at
                FROM drill_events
                WHERE drill_id=?
                ORDER BY created_at ASC
                """,
                (drill["id"],),
            ).fetchall()

        started_at = datetime.fromisoformat(drill["started_at"])
        ends_at = datetime.fromisoformat(drill["ends_at"])
        ended_at = datetime.fromisoformat(drill["ended_at"]) if drill["ended_at"] else None
        constraints = json.loads(drill["constraints"])
        success_criteria = json.loads(drill["success_criteria"])

        inject_events = [
            (datetime.fromisoformat(row[3]), str(row[1]))
            for row in events
            if str(row[0]) == "inject"
        ]

        action_events = [
            (datetime.fromisoformat(row[3]), str(row[1]), str(row[2]) if row[2] else "staff")
            for row in events
            if str(row[0]) == "action"
        ]

        timeline_lines = []
        for event_type, content, role_name, created_at in events:
            ts = datetime.fromisoformat(created_at)
            delta = _format_delta(ts - started_at)
            label = event_type.upper()
            suffix = f" ({role_name})" if role_name else ""
            timeline_lines.append(f"{delta} | {label}{suffix}: {content}")

        info_lines = []
        for action_time, action_text, action_role in action_events:
            known = [f"{_format_delta(t - started_at)} {msg}" for t, msg in inject_events if t <= action_time]
            if len(known) > 5:
                known = known[-5:]
                known.append(f"... +{len(inject_events) - 5} earlier inject(s)")
            info_block = "; ".join(known) if known else "none"
            info_lines.append(
                f"{_format_delta(action_time - started_at)} | ACTION ({action_role}): {action_text} | info: {info_block}"
            )

        noise_reducers = []
        noise_amplifiers = []
        reduce_keys = ("lock", "slowmode", "slow mode", "mute", "timeout", "contain", "pause", "quarantine")
        amplify_keys = ("unlock", "open chat", "open channel", "announce", "broadcast", "ping everyone", "invite")
        for _, action_text, _ in action_events:
            lowered = action_text.lower()
            if any(k in lowered for k in reduce_keys):
                noise_reducers.append(action_text)
            if any(k in lowered for k in amplify_keys):
                noise_amplifiers.append(action_text)

        delay_lines = []
        if inject_events and action_events:
            first_inject_time = inject_events[0][0]
            first_action_time = action_events[0][0]
            delta_first = first_action_time - first_inject_time
            delay_lines.append(f"First action after first inject: {_format_delta(delta_first)}")
        if len(action_events) > 1:
            gaps = [
                (action_events[i][0] - action_events[i - 1][0])
                for i in range(1, len(action_events))
            ]
            if gaps:
                biggest_gap = max(gaps)
                delay_lines.append(f"Largest action-to-action gap: {_format_delta(biggest_gap)}")

        counterfactuals = []
        if inject_events and action_events:
            delta_first = action_events[0][0] - inject_events[0][0]
            if delta_first > timedelta(minutes=2):
                counterfactuals.append(
                    f"If initial containment started {_format_delta(delta_first)} earlier, what changes downstream?"
                )
        if len(action_events) > 1:
            gaps = [
                (action_events[i][0] - action_events[i - 1][0])
                for i in range(1, len(action_events))
            ]
            if gaps and max(gaps) > timedelta(minutes=5):
                counterfactuals.append(
                    "If a coordination check-in happened earlier, would it reduce drift or ambiguity?"
                )

        report_lines = [
            f"Drill report - ID {drill['id']} ({drill['scenario_type']})",
            f"Status: {drill['status']}",
            f"Started: {started_at.isoformat()}",
            f"Planned end: {ends_at.isoformat()}",
            f"Actual end: {ended_at.isoformat() if ended_at else 'in progress'}",
            "",
            "Constraints:",
            *_format_list(constraints).splitlines(),
            "",
            "Success criteria:",
            *_format_list(success_criteria).splitlines(),
            "",
            "Timeline:",
            *timeline_lines,
            "",
            "Info available at each action:",
            *info_lines,
            "",
            "Coordination delays:",
            *(delay_lines or ["none observed"]),
            "",
            "Actions likely reducing noise (heuristic):",
            *(noise_reducers or ["none detected"]),
            "",
            "Actions likely amplifying noise (heuristic):",
            *(noise_amplifiers or ["none detected"]),
            "",
            "Counterfactual prompts:",
            *(counterfactuals or ["none generated"]),
            "",
            f"Drill data expires after {DATA_RETENTION_DAYS} days.",
        ]

        report_text = "\n".join(report_lines)
        await interaction.response.defer(ephemeral=True, thinking=True)

        target_channel = self.bot.get_channel(drill["thread_id"] or drill["channel_id"])
        if len(report_text) > 1800:
            file = discord.File(
                fp=io.BytesIO(report_text.encode("utf-8")),
                filename=f"drill_report_{drill['id']}.txt",
            )
            if target_channel:
                await target_channel.send(file=file, content="Drill report attached.")
                await interaction.followup.send("Report generated and attached in the drill channel.", ephemeral=True)
            else:
                await interaction.followup.send("Report generated.", file=file, ephemeral=True)
        else:
            if target_channel:
                await target_channel.send(f"```\n{report_text}\n```")
                await interaction.followup.send("Report generated and posted in the drill channel.", ephemeral=True)
            else:
                await interaction.followup.send(f"```\n{report_text}\n```", ephemeral=True)

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def injection_loop(self) -> None:
        now_iso = _utc_now().isoformat()
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute(
                """
                SELECT id, drill_id, content, send_at
                FROM drill_injections
                WHERE delivered_at IS NULL AND send_at <= ?
                ORDER BY send_at ASC
                LIMIT 10
                """,
                (now_iso,),
            ).fetchall()

        for injection_id, drill_id, content, send_at in rows:
            with sqlite3.connect(DB_PATH) as con:
                drill_row = con.execute(
                    "SELECT channel_id, thread_id, status FROM drills WHERE id=?",
                    (int(drill_id),),
                ).fetchone()
            if not drill_row or drill_row[2] != "active":
                with sqlite3.connect(DB_PATH) as con:
                    con.execute(
                        "UPDATE drill_injections SET delivered_at=? WHERE id=?",
                        (_utc_now().isoformat(), int(injection_id)),
                    )
                    con.commit()
                continue

            channel_id = int(drill_row[1]) if drill_row[1] else int(drill_row[0])
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                with sqlite3.connect(DB_PATH) as con:
                    con.execute(
                        "UPDATE drill_injections SET delivered_at=? WHERE id=?",
                        (_utc_now().isoformat(), int(injection_id)),
                    )
                    con.commit()
                continue

            try:
                await channel.send(f"**Inject:** {content}")
            except (discord.Forbidden, discord.HTTPException):
                with sqlite3.connect(DB_PATH) as con:
                    con.execute(
                        "UPDATE drill_injections SET delivered_at=? WHERE id=?",
                        (_utc_now().isoformat(), int(injection_id)),
                    )
                    con.commit()
                _add_event(int(drill_id), "system", "Inject failed to deliver.", None, None, _utc_now())
                continue
            with sqlite3.connect(DB_PATH) as con:
                con.execute(
                    "UPDATE drill_injections SET delivered_at=? WHERE id=?",
                    (_utc_now().isoformat(), int(injection_id)),
                )
                con.commit()
            _add_event(int(drill_id), "inject", str(content), None, None, _utc_now())

    @injection_loop.before_loop
    async def before_injection_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DrillCog(bot))
