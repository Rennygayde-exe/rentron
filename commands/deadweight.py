from __future__ import annotations

import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from utils import responses as response_utils
from utils import usage_log

ROLE_MENU_FILE = Path("data/role_menus.json")
ROLE_REACT_FILE = Path("data/react_roles.json")

BLAST_RADIUS_HINTS = {
    "commands.moderation": "server-wide moderation",
    "commands.pruning_logic": "server-wide maintenance",
    "commands.audit": "server configuration",
    "commands.application": "server workflow",
    "commands.role_menu": "role configuration",
    "commands.scheduler": "scheduled messages",
    "commands.music": "voice channel scope",
    "commands.drill": "staff-only simulation",
    "commands.deadweight": "staff-only analysis",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _shorten(text: str, limit: int = 140) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def _command_intent(cmd) -> str:
    if isinstance(cmd, app_commands.Command):
        return cmd.description or (cmd.callback.__doc__ or "no description").strip()
    return (cmd.help or cmd.callback.__doc__ or "no description").strip()


def _command_module(cmd) -> str:
    return getattr(cmd.callback, "__module__", "unknown")


def _blast_radius(module: str) -> str:
    return BLAST_RADIUS_HINTS.get(module, "channel-level")


def _format_command_label(name: str, command_type: str) -> str:
    prefix = "/" if command_type == "app" else "!"
    return f"{prefix}{name}"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _flatten_app_commands(items: list[app_commands.Command | app_commands.Group]) -> list[app_commands.Command]:
    flattened: list[app_commands.Command] = []
    for item in items:
        if isinstance(item, app_commands.Group):
            flattened.extend(_flatten_app_commands(list(item.commands)))
        else:
            flattened.append(item)
    return flattened


def _usage_window_counts(guild_id: int, since_iso: str) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    with sqlite3.connect(usage_log.DB_PATH) as con:
        rows = con.execute(
            """
            SELECT command_name, command_type, COUNT(*)
            FROM command_usage
            WHERE guild_id=? AND used_at >= ?
            GROUP BY command_name, command_type
            """,
            (guild_id, since_iso),
        ).fetchall()
    for name, cmd_type, count in rows:
        counts[(str(name), str(cmd_type))] = int(count)
    return counts


def _usage_last_seen(guild_id: int) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    last_used: dict[tuple[str, str], str] = {}
    last_role: dict[tuple[str, str], str] = {}
    with sqlite3.connect(usage_log.DB_PATH) as con:
        rows = con.execute(
            """
            SELECT command_name, command_type, role_name, used_at
            FROM command_usage
            WHERE guild_id=?
            ORDER BY used_at DESC
            """,
            (guild_id,),
        ).fetchall()
    for name, cmd_type, role_name, used_at in rows:
        key = (str(name), str(cmd_type))
        if key in last_used:
            continue
        last_used[key] = str(used_at)
        last_role[key] = str(role_name) if role_name else "n/a"
    return last_used, last_role


def _response_window_counts(guild_id: int, since_iso: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    with sqlite3.connect(usage_log.DB_PATH) as con:
        rows = con.execute(
            """
            SELECT response_id, COUNT(*)
            FROM response_usage
            WHERE guild_id=? AND used_at >= ?
            GROUP BY response_id
            """,
            (guild_id, since_iso),
        ).fetchall()
    for response_id, count in rows:
        counts[str(response_id)] = int(count)
    return counts


def _response_last_seen(guild_id: int) -> tuple[dict[str, str], dict[str, str]]:
    last_used: dict[str, str] = {}
    last_role: dict[str, str] = {}
    with sqlite3.connect(usage_log.DB_PATH) as con:
        rows = con.execute(
            """
            SELECT response_id, role_name, used_at
            FROM response_usage
            WHERE guild_id=?
            ORDER BY used_at DESC
            """,
            (guild_id,),
        ).fetchall()
    for response_id, role_name, used_at in rows:
        key = str(response_id)
        if key in last_used:
            continue
        last_used[key] = str(used_at)
        last_role[key] = str(role_name) if role_name else "n/a"
    return last_used, last_role


def _find_stale_role_refs(guild: discord.Guild) -> list[dict]:
    stale: list[dict] = []
    role_menus = _load_json(ROLE_MENU_FILE)
    react_roles = _load_json(ROLE_REACT_FILE)

    for menu_id, entry in role_menus.items():
        if int(entry.get("guild_id", 0)) != guild.id:
            continue
        for item in entry.get("entries", []) or []:
            role_id = item.get("role_id")
            if not role_id or guild.get_role(int(role_id)):
                continue
            stale.append(
                {
                    "source": "role_menus.json",
                    "container_id": menu_id,
                    "role_id": int(role_id),
                    "label": item.get("label") or "unknown",
                }
            )

    for menu_id, entry in react_roles.items():
        if int(entry.get("guild_id", 0)) != guild.id:
            continue
        for item in entry.get("entries", []) or []:
            role_id = item.get("role_id")
            if not role_id or guild.get_role(int(role_id)):
                continue
            stale.append(
                {
                    "source": "react_roles.json",
                    "container_id": menu_id,
                    "role_id": int(role_id),
                    "label": item.get("emoji") or "unknown",
                }
            )

    return stale


class UsageTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        usage_log.ensure_db()

    async def _warn_sunset(
        self,
        interaction: Interaction | None,
        ctx: commands.Context | None,
        target_type: str,
        target_name: str,
        role_id: Optional[int],
        role_name: Optional[str],
    ) -> None:
        sunset = usage_log.get_active_sunset(target_type, target_name)
        if not sunset:
            return
        usage_log.log_sunset_usage(int(sunset["id"]), role_id, role_name)

        if target_type not in {"command", "cog"}:
            return

        expires = datetime.fromisoformat(sunset["expires_at"])
        remaining = expires - _utc_now()
        remaining_days = max(0, remaining.days)
        msg = (
            f"Notice: `{target_name}` is scheduled for removal in {remaining_days} day(s) "
            f"(expires {expires.date().isoformat()})."
        )
        if sunset.get("reason"):
            msg += f" Reason: {sunset['reason']}"

        if interaction:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        elif ctx:
            await ctx.send(msg)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        if not ctx.guild or not ctx.command:
            return
        role_id, role_name = _role_info(ctx.author)
        usage_log.log_command_usage(ctx.command.qualified_name, "prefix", ctx.guild.id, role_id, role_name)
        await self._warn_sunset(ctx=ctx, interaction=None, target_type="command", target_name=ctx.command.qualified_name, role_id=role_id, role_name=role_name)

        if ctx.command.cog:
            await self._warn_sunset(
                ctx=ctx,
                interaction=None,
                target_type="cog",
                target_name=ctx.command.cog.qualified_name,
                role_id=role_id,
                role_name=role_name,
            )

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: Interaction, command: app_commands.Command) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        role_id, role_name = _role_info(interaction.user)
        usage_log.log_command_usage(command.qualified_name, "app", interaction.guild.id, role_id, role_name)
        await self._warn_sunset(
            interaction=interaction,
            ctx=None,
            target_type="command",
            target_name=command.qualified_name,
            role_id=role_id,
            role_name=role_name,
        )

        binding = getattr(command, "binding", None)
        if binding:
            await self._warn_sunset(
                interaction=interaction,
                ctx=None,
                target_type="cog",
                target_name=binding.qualified_name,
                role_id=role_id,
                role_name=role_name,
            )


class DeadweightCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        usage_log.ensure_db()

    deadweight = app_commands.Group(name="deadweight", description="Entropy management tooling")

    @deadweight.command(name="scan", description="Scan for unused or stale commands and content.")
    @app_commands.describe(days="Lookback window in days (default 90)")
    async def deadweight_scan(self, interaction: Interaction, days: app_commands.Range[int, 14, 365] = 90) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return
        if not _is_staff(interaction.user):
            await interaction.response.send_message("This command is for staff only.", ephemeral=True)
            return

        since = usage_log.get_usage_window_start(days)
        since_iso = since.isoformat()

        app_cmds = _flatten_app_commands(list(self.bot.tree.get_commands()))
        prefix_cmds = list(self.bot.commands)

        command_entries: dict[tuple[str, str], dict] = {}
        for cmd in app_cmds:
            key = (cmd.qualified_name, "app")
            command_entries[key] = {
                "name": cmd.qualified_name,
                "type": "app",
                "intent": _shorten(_command_intent(cmd)),
                "module": _command_module(cmd),
                "blast": _blast_radius(_command_module(cmd)),
            }
        for cmd in prefix_cmds:
            key = (cmd.qualified_name, "prefix")
            command_entries[key] = {
                "name": cmd.qualified_name,
                "type": "prefix",
                "intent": _shorten(_command_intent(cmd)),
                "module": _command_module(cmd),
                "blast": _blast_radius(_command_module(cmd)),
            }

        usage_log.ensure_db()
        window_counts = _usage_window_counts(interaction.guild.id, since_iso)
        last_used, last_role = _usage_last_seen(interaction.guild.id)

        never_used = []
        used_once = []
        for key, entry in sorted(command_entries.items()):
            count = window_counts.get(key, 0)
            last_seen = last_used.get(key, "never")
            role_name = last_role.get(key, "n/a")
            if last_seen == "never":
                never_used.append((entry, last_seen, role_name))
            elif count == 1:
                used_once.append((entry, last_seen, role_name))

        if not response_utils.RESPONSES:
            response_utils.load_responses()
        response_entries = {str(r.get("id", "")): r for r in response_utils.RESPONSES if r.get("id")}

        response_window_counts = _response_window_counts(interaction.guild.id, since_iso)
        response_last_used, response_last_role = _response_last_seen(interaction.guild.id)

        stale_responses = []
        for response_id, entry in response_entries.items():
            last_seen = response_last_used.get(response_id, "never")
            role_name = response_last_role.get(response_id, "n/a")
            if last_seen == "never":
                stale_responses.append((response_id, entry, last_seen, role_name))
                continue
            last_dt = datetime.fromisoformat(last_seen)
            if last_dt < since:
                stale_responses.append((response_id, entry, last_seen, role_name))

        # Cogs with no activity
        command_to_cog: dict[str, str] = {}
        for cmd in app_cmds:
            binding = getattr(cmd, "binding", None)
            if binding:
                command_to_cog[cmd.qualified_name] = binding.qualified_name
        for cmd in prefix_cmds:
            if cmd.cog:
                command_to_cog[cmd.qualified_name] = cmd.cog.qualified_name

        cog_activity: dict[str, int] = {}
        for (name, _type), count in window_counts.items():
            cog_name = command_to_cog.get(name)
            if not cog_name:
                continue
            cog_activity[cog_name] = cog_activity.get(cog_name, 0) + count

        inactive_cogs = []
        for cog_name in self.bot.cogs.keys():
            if cog_name not in cog_activity:
                inactive_cogs.append(cog_name)

        stale_roles = _find_stale_role_refs(interaction.guild)

        lines = [
            f"Deadweight scan - last {days} days (since {since.date().isoformat()})",
            "",
            "Commands never used:",
        ]
        lines.extend(
            [
                f"- {_format_command_label(item['name'], item['type'])}; last: {last}; role: {role}; intent: {item['intent']}; deps: {item['module']}; blast: {item['blast']}"
                for item, last, role in (never_used or [])
            ]
            or ["- none"]
        )

        lines.append("")
        lines.append("Commands used only once (window):")
        lines.extend(
            [
                f"- {_format_command_label(item['name'], item['type'])}; last: {last}; role: {role}; intent: {item['intent']}; deps: {item['module']}; blast: {item['blast']}"
                for item, last, role in (used_once or [])
            ]
            or ["- none"]
        )

        lines.append("")
        lines.append("Responses not triggered within window:")
        if stale_responses:
            for response_id, entry, last_seen, role_name in stale_responses:
                intent = _shorten(entry.get("response", "") or "no response text")
                triggers = ", ".join(entry.get("triggers", [])[:3])
                lines.append(
                    f"- {response_id}; last: {last_seen}; role: {role_name}; intent: {intent}; deps: responses.json; blast: channel; triggers: {triggers}"
                )
        else:
            lines.append("- none")

        lines.append("")
        lines.append("Loaded cogs with no activity (window):")
        lines.extend([f"- {name}" for name in (inactive_cogs or [])] or ["- none"])

        lines.append("")
        lines.append("Role bindings pointing to missing roles:")
        if stale_roles:
            for item in stale_roles:
                lines.append(
                    f"- {item['source']}; container: {item['container_id']}; role_id: {item['role_id']}; label: {item['label']}"
                )
        else:
            lines.append("- none")

        report_text = "\n".join(lines)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if len(report_text) > 1800:
            file = discord.File(
                fp=io.BytesIO(report_text.encode("utf-8")),
                filename=f"deadweight_scan_{interaction.guild.id}.txt",
            )
            await interaction.followup.send("Scan complete. Report attached.", file=file, ephemeral=True)
        else:
            await interaction.followup.send(f"```\n{report_text}\n```", ephemeral=True)

    @deadweight.command(name="sunset", description="Mark a command or feature for soft deprecation.")
    @app_commands.describe(
        target_type="Type of item to sunset",
        target_name="Qualified command/cog/response name",
        days="Sunset window in days",
        reason="Optional reason for the sunset",
    )
    @app_commands.choices(
        target_type=[
            app_commands.Choice(name="command", value="command"),
            app_commands.Choice(name="cog", value="cog"),
            app_commands.Choice(name="response", value="response"),
        ]
    )
    async def deadweight_sunset(
        self,
        interaction: Interaction,
        target_type: app_commands.Choice[str],
        target_name: str,
        days: app_commands.Range[int, 7, 180] = 30,
        reason: str = "",
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return
        if not _is_staff(interaction.user):
            await interaction.response.send_message("This command is for staff only.", ephemeral=True)
            return

        target = target_name.strip()
        if not target:
            await interaction.response.send_message("Provide a valid target name.", ephemeral=True)
            return

        if target_type.value == "command":
            all_commands = {cmd.qualified_name for cmd in _flatten_app_commands(list(self.bot.tree.get_commands()))}
            all_commands.update({cmd.qualified_name for cmd in self.bot.commands})
            if target not in all_commands:
                await interaction.response.send_message("Command not found.", ephemeral=True)
                return
        elif target_type.value == "cog":
            if target not in self.bot.cogs:
                await interaction.response.send_message("Cog not found.", ephemeral=True)
                return
        else:
            if not response_utils.RESPONSES:
                response_utils.load_responses()
            if target not in {str(r.get("id")) for r in response_utils.RESPONSES if r.get("id")}:
                await interaction.response.send_message("Response ID not found.", ephemeral=True)
                return

        usage_log.upsert_sunset(target_type.value, target, days, reason or None)
        await interaction.response.send_message(
            f"Sunset set for {target_type.value} `{target}` ({days} days).",
            ephemeral=True,
        )

    @deadweight.command(name="report", description="Summarize deadweight candidates and sunset outcomes.")
    async def deadweight_report(self, interaction: Interaction, days: app_commands.Range[int, 14, 365] = 90) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return
        if not _is_staff(interaction.user):
            await interaction.response.send_message("This command is for staff only.", ephemeral=True)
            return

        since = usage_log.get_usage_window_start(days)
        since_iso = since.isoformat()

        app_cmds = _flatten_app_commands(list(self.bot.tree.get_commands()))
        prefix_cmds = list(self.bot.commands)

        all_commands = {(cmd.qualified_name, "app") for cmd in app_cmds}
        all_commands.update({(cmd.qualified_name, "prefix") for cmd in prefix_cmds})

        window_counts = _usage_window_counts(interaction.guild.id, since_iso)
        last_used, _last_role = _usage_last_seen(interaction.guild.id)

        never_used = [key for key in all_commands if key not in last_used]
        used_once = [key for key in all_commands if window_counts.get(key, 0) == 1]

        if not response_utils.RESPONSES:
            response_utils.load_responses()
        response_entries = {str(r.get("id")) for r in response_utils.RESPONSES if r.get("id")}
        response_last_used, _ = _response_last_seen(interaction.guild.id)
        stale_responses = [
            rid for rid in response_entries
            if rid not in response_last_used or datetime.fromisoformat(response_last_used[rid]) < since
        ]

        inactive_cogs = []
        command_to_cog: dict[str, str] = {}
        for cmd in app_cmds:
            binding = getattr(cmd, "binding", None)
            if binding:
                command_to_cog[cmd.qualified_name] = binding.qualified_name
        for cmd in prefix_cmds:
            if cmd.cog:
                command_to_cog[cmd.qualified_name] = cmd.cog.qualified_name
        cog_activity: dict[str, int] = {}
        for (name, _type), count in window_counts.items():
            cog_name = command_to_cog.get(name)
            if not cog_name:
                continue
            cog_activity[cog_name] = cog_activity.get(cog_name, 0) + count
        for cog_name in self.bot.cogs.keys():
            if cog_name not in cog_activity:
                inactive_cogs.append(cog_name)

        stale_roles = _find_stale_role_refs(interaction.guild)

        usage_log.expire_old_sunsets()
        with sqlite3.connect(usage_log.DB_PATH) as con:
            sunsets = con.execute(
                """
                SELECT id, target_type, target_name, created_at, expires_at, reason
                FROM sunsets
                WHERE active=1
                ORDER BY created_at DESC
                """
            ).fetchall()
            sunset_hits = con.execute(
                """
                SELECT sunset_id, COUNT(*)
                FROM sunset_usage
                GROUP BY sunset_id
                """
            ).fetchall()

        hits_map = {int(row[0]): int(row[1]) for row in sunset_hits}
        rescued = [
            row for row in sunsets if hits_map.get(int(row[0]), 0) > 0
        ]

        total_candidates = (
            len(never_used) + len(used_once) + len(stale_responses) + len(inactive_cogs) + len(stale_roles)
        )

        lines = [
            f"Deadweight report - last {days} days",
            f"Total candidates for removal: {total_candidates}",
            f"Command candidates: {len(never_used)} never used, {len(used_once)} used once",
            f"Responses stale: {len(stale_responses)}",
            f"Inactive cogs: {len(inactive_cogs)}",
            f"Stale role bindings: {len(stale_roles)}",
            "",
            "Features rescued during sunset:",
        ]

        if rescued:
            for row in rescued:
                lines.append(
                    f"- {row[1]} `{row[2]}`; hits: {hits_map.get(int(row[0]), 0)}; expires: {row[4][:10]}"
                )
        else:
            lines.append("- none")

        lines.extend(
            [
                "",
                "Reclaimed cognitive surface area (estimate):",
                f"- {len(never_used) + len(used_once)} commands",
                f"- {len(stale_responses)} responses",
                f"- {len(inactive_cogs)} cogs",
                "",
                "Documentation impact:",
                "- update help/docs for any removals",
                "- remove stale role menu references if applicable",
            ]
        )

        report_text = "\n".join(lines)
        await interaction.response.send_message(f"```\n{report_text}\n```", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UsageTracker(bot))
    await bot.add_cog(DeadweightCog(bot))
