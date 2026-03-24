import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from dateutil import parser as date_parser
from discord import app_commands
from discord.ext import commands

DB_PATH = Path("data/member_history.db")
MEE6_BOT_ID = 159985870458322944


def _ensure_db() -> None:
    """Create the member history table and indexes if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS member_intervals(
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at REAL NOT NULL,
                left_at REAL,
                PRIMARY KEY (guild_id, user_id, joined_at)
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_member_intervals_guild_join ON member_intervals(guild_id, joined_at)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_member_intervals_guild_left ON member_intervals(guild_id, left_at)"
        )
        con.commit()


def _as_utc(dt: Optional[datetime]) -> datetime:
    """Coerce datetimes to aware UTC values."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_datetime(raw: str) -> Optional[datetime]:
    """Parse user input into a timezone-aware UTC datetime."""
    try:
        parsed = date_parser.parse(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _gather_message_text(message: discord.Message) -> str:
    parts = []
    if message.content:
        parts.append(message.content)
    for embed in message.embeds:
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            if field.name:
                parts.append(field.name)
            if field.value:
                parts.append(field.value)
    return "\n".join(parts)


def _extract_user_id(text: str, message: discord.Message) -> Optional[int]:
    match = re.search(r"<@!?(\d+)>", text)
    if match:
        return int(match.group(1))
    if message.mentions:
        return message.mentions[0].id
    return None


def _classify_event(text: str) -> Optional[str]:
    lowered = text.lower()
    leave_hits = ("left", "goodbye", "farewell", "has left", "member left")
    join_hits = ("welcome", "joined", "just joined", "member joined")
    if any(token in lowered for token in leave_hits):
        return "leave"
    if any(token in lowered for token in join_hits):
        return "join"
    return None


def _is_mee6(message: discord.Message) -> bool:
    author = message.author
    if not author:
        return False
    if author.id == MEE6_BOT_ID:
        return True
    return author.name.lower() == "mee6"


class MemberMetrics(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._baseline_synced = False
        _ensure_db()

     
                         
     
    def _record_join(self, guild_id: int, user_id: int, joined_ts: float) -> None:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
                                                                        
            cur.execute(
                """
                UPDATE member_intervals
                SET left_at = ?
                WHERE guild_id = ? AND user_id = ? AND left_at IS NULL AND joined_at < ?
                """,
                (joined_ts, guild_id, user_id, joined_ts),
            )
            cur.execute(
                """
                INSERT OR IGNORE INTO member_intervals(guild_id, user_id, joined_at)
                VALUES(?, ?, ?)
                """,
                (guild_id, user_id, joined_ts),
            )
            con.commit()

    def _record_leave(self, guild_id: int, user_id: int, joined_ts: float, left_ts: float) -> None:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            row = cur.execute(
                """
                SELECT joined_at FROM member_intervals
                WHERE guild_id = ? AND user_id = ? AND left_at IS NULL
                ORDER BY joined_at DESC
                LIMIT 1
                """,
                (guild_id, user_id),
            ).fetchone()
            anchor_join = row[0] if row else joined_ts
            if not row:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO member_intervals(guild_id, user_id, joined_at)
                    VALUES(?, ?, ?)
                    """,
                    (guild_id, user_id, anchor_join),
                )
            cur.execute(
                """
                UPDATE member_intervals
                SET left_at = ?
                WHERE guild_id = ? AND user_id = ? AND joined_at = ?
                """,
                (left_ts, guild_id, user_id, anchor_join),
            )
            con.commit()

    async def _sync_current_members(self) -> None:
        """Ensure every currently-present member has an open interval."""
        if self._baseline_synced:
            return
        self._baseline_synced = True
        for guild in self.bot.guilds:
            rows: list[tuple[int, int, float]] = []
            for member in guild.members:
                joined = _as_utc(member.joined_at).timestamp()
                rows.append((guild.id, member.id, joined))
            with sqlite3.connect(DB_PATH) as con:
                cur = con.cursor()
                cur.executemany(
                    """
                    INSERT OR IGNORE INTO member_intervals(guild_id, user_id, joined_at)
                    VALUES(?, ?, ?)
                    """,
                    rows,
                )
                con.commit()

     
                     
     
    @commands.Cog.listener()
    async def on_ready(self):
        await self._sync_current_members()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        joined_ts = _as_utc(member.joined_at).timestamp()
        self._record_join(member.guild.id, member.id, joined_ts)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        joined_ts = _as_utc(member.joined_at).timestamp()
        left_ts = datetime.now(timezone.utc).timestamp()
        self._record_leave(member.guild.id, member.id, joined_ts, left_ts)

     
                
     
    @app_commands.command(
        name="unique_users",
        description="Count unique members who were in the server between two dates.",
    )
    @app_commands.describe(
        start="Start date/time (e.g. 2024-01-01 or 2024-01-01T12:00Z)",
        end="End date/time (e.g. 2024-01-31 or 2024-01-31T18:00-05:00)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def unique_users(self, interaction: discord.Interaction, start: str, end: str):
        if not interaction.guild:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        start_dt = _parse_datetime(start)
        end_dt = _parse_datetime(end)
        if not start_dt or not end_dt:
            await interaction.response.send_message(
                "Could not understand one of those dates. Try ISO format like `2025-01-10 15:30` or `2025-01-10T15:30Z`.",
                ephemeral=True,
            )
            return

                                                   
        if start_dt > end_dt:
            start_dt, end_dt = end_dt, start_dt

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._sync_current_members()

        start_ts = start_dt.timestamp()
        end_ts = end_dt.timestamp()

        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            count_row = cur.execute(
                """
                SELECT COUNT(DISTINCT user_id)
                FROM member_intervals
                WHERE guild_id = ?
                  AND joined_at <= ?
                  AND (left_at IS NULL OR left_at >= ?)
                """,
                (interaction.guild.id, end_ts, start_ts),
            ).fetchone()
            earliest_row = cur.execute(
                "SELECT MIN(joined_at) FROM member_intervals WHERE guild_id = ?",
                (interaction.guild.id,),
            ).fetchone()

        total_unique = int(count_row[0]) if count_row and count_row[0] is not None else 0
        earliest_tracked = None
        if earliest_row and earliest_row[0] is not None:
            earliest_tracked = datetime.fromtimestamp(float(earliest_row[0]), tz=timezone.utc)

        lines = [
            f"Unique members between {discord.utils.format_dt(start_dt, style='F')} and {discord.utils.format_dt(end_dt, style='F')}: **{total_unique}**"
        ]
        if earliest_tracked:
            lines.append(
                f"(Earliest tracked join for this guild: {discord.utils.format_dt(earliest_tracked, style='F')})"
            )
        else:
            lines.append("No member history tracked yet for this guild.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(
        name="backfill_mee6_logs",
        description="Backfill member history from MEE6 join/leave logs in a channel.",
    )
    @app_commands.describe(
        channel="Log channel that contains MEE6 join/leave messages.",
        start="Start date/time (e.g. 2024-01-01)",
        end="End date/time (e.g. 2024-12-31)",
        apply="Write results to the member history database (default false).",
        assume_join_at_start="If a leave has no prior join, treat them as present since the start date.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def backfill_mee6_logs(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        start: str,
        end: str,
        apply: bool = False,
        assume_join_at_start: bool = True,
    ):
        if not interaction.guild:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        start_dt = _parse_datetime(start)
        end_dt = _parse_datetime(end)
        if not start_dt or not end_dt:
            await interaction.response.send_message(
                "Could not understand one of those dates. Try ISO format like `2025-01-10 15:30` or `2025-01-10T15:30Z`.",
                ephemeral=True,
            )
            return

        if start_dt > end_dt:
            start_dt, end_dt = end_dt, start_dt

        await interaction.response.defer(ephemeral=True, thinking=True)

        join_count = 0
        leave_count = 0
        skipped_non_mee6 = 0
        skipped_no_user = 0
        skipped_unclassified = 0
        sample_lines = []
        start_ts = start_dt.timestamp()

        async for message in channel.history(after=start_dt, before=end_dt, oldest_first=True, limit=None):
            if not _is_mee6(message):
                skipped_non_mee6 += 1
                continue
            text = _gather_message_text(message)
            if not text:
                skipped_unclassified += 1
                continue
            event = _classify_event(text)
            if not event:
                skipped_unclassified += 1
                continue
            user_id = _extract_user_id(text, message)
            if not user_id:
                skipped_no_user += 1
                continue
            created_ts = _as_utc(message.created_at).timestamp()

            if event == "join":
                join_count += 1
                if apply:
                    self._record_join(interaction.guild.id, user_id, created_ts)
            else:
                leave_count += 1
                if apply:
                    anchor_join = start_ts if assume_join_at_start else created_ts
                    self._record_leave(interaction.guild.id, user_id, anchor_join, created_ts)

            if len(sample_lines) < 5:
                stamp = discord.utils.format_dt(_as_utc(message.created_at), style="F")
                sample_lines.append(f"{event} user_id={user_id} at {stamp}")

        mode_label = "applied" if apply else "dry run"
        lines = [
            f"Backfill {mode_label} for {channel.mention} between {discord.utils.format_dt(start_dt, style='F')} and {discord.utils.format_dt(end_dt, style='F')}.",
            f"Join events: {join_count} | Leave events: {leave_count}",
            f"Skipped non-MEE6: {skipped_non_mee6} | Skipped no user: {skipped_no_user} | Skipped unclassified: {skipped_unclassified}",
        ]
        if sample_lines:
            lines.append("Samples:\n" + "\n".join(sample_lines))
        if not apply:
            lines.append("Re-run with `apply: true` once the samples look right.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MemberMetrics(bot))
