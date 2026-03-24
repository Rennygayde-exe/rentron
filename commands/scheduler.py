import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from dateutil import parser as dateparser
from discord import Interaction, TextChannel, app_commands
from discord.ext import commands, tasks
from discord.ui import Modal, Select, TextInput, View, button

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "scheduled_messages.db"
CHECK_INTERVAL_SECONDS = 30
DEFAULT_TZ_NAME = os.getenv("SCHEDULER_DEFAULT_TZ", "").strip()
LOCAL_TZINFO = datetime.now().astimezone().tzinfo or timezone.utc
_YEAR_OPTIONS_SPAN = 3  # current year + next 2
_TIME_OPTIONS = [f"{h:02d}:00" for h in range(24)]
_DAY_SPECIAL_VALUE = "25-31"
_TIME_24H_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def ensure_db() -> None:
    """Create the scheduled messages table if it does not exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                send_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        con.commit()


def _parse_timezone(tz_name: str) -> tuple[Optional[timezone], str]:
    requested = (tz_name or "").strip()
    if requested:
        try:
            return ZoneInfo(requested), requested
        except Exception:
            return None, requested

    if DEFAULT_TZ_NAME:
        try:
            return ZoneInfo(DEFAULT_TZ_NAME), DEFAULT_TZ_NAME
        except Exception:
            pass

    if LOCAL_TZINFO:
        label = getattr(LOCAL_TZINFO, "key", None) or str(LOCAL_TZINFO)
        return LOCAL_TZINFO, label

    return timezone.utc, "UTC"


def _parse_time_only(raw: str, tzinfo: ZoneInfo) -> Optional[datetime]:
    """Handle inputs like '22:17', '2217', or '10pm' by pinning them to today (or tomorrow if already passed)."""
    text = (raw or "").strip().lower()
    now_local = datetime.now(tzinfo)

    # 2217, 930
    if re.fullmatch(r"\d{3,4}", text):
        if len(text) == 3:
            hour = int(text[0])
            minute = int(text[1:3])
        else:
            hour = int(text[0:2])
            minute = int(text[2:4])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        return candidate

    # 10pm, 7 am
    m_ampm = re.fullmatch(r"(\d{1,2})\s*(am|pm)", text)
    if m_ampm:
        hour = int(m_ampm.group(1))
        meridiem = m_ampm.group(2)
        if hour == 12:
            hour = 0 if meridiem == "am" else 12
        elif meridiem == "pm":
            hour += 12
        if not (0 <= hour < 24):
            return None
        candidate = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        return candidate

    # 22:17 or 10:05pm
    m_clock = re.fullmatch(r"(\d{1,2}):(\d{2})\s*(am|pm)?", text)
    if m_clock:
        hour = int(m_clock.group(1))
        minute = int(m_clock.group(2))
        meridiem = m_clock.group(3)
        if meridiem:
            if hour == 12:
                hour = 0 if meridiem == "am" else 12
            elif meridiem == "pm":
                hour += 12
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        return candidate

    return None


def _parse_24h_time_only(raw: str, tzinfo: timezone) -> Optional[datetime]:
    """Parse a strict 24h HH:MM time and pin to today or tomorrow in the given timezone."""
    text = (raw or "").strip()
    match = _TIME_24H_RE.fullmatch(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    now_local = datetime.now(tzinfo)
    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate


def parse_schedule_datetime(raw_time: str, tz_name: str) -> tuple[Optional[datetime], str, Optional[str]]:
    """Convert user input into an aware UTC datetime."""
    tzinfo, tz_label = _parse_timezone(tz_name)
    if tzinfo is None:
        return None, tz_label, f"Unknown timezone '{tz_label}'. Use IANA names like 'UTC' or 'America/New_York'."

    # Try simple clock formats first so bare "2217" becomes 22:17 today.
    clock_dt = _parse_time_only(raw_time, tzinfo)
    if clock_dt:
        return clock_dt.astimezone(timezone.utc), tz_label, None

    try:
        parsed = dateparser.parse(raw_time)
    except (ValueError, OverflowError, TypeError):
        return None, tz_label, "Could not understand that time. Try '2025-01-30 14:30' or an ISO timestamp."
    if parsed is None:
        return None, tz_label, "Could not understand that time. Try '2025-01-30 14:30' or an ISO timestamp."
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tzinfo)
    return parsed.astimezone(timezone.utc), tz_label, None


class MessageScheduler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        ensure_db()
        self.dispatch_loop.start()

    def cog_unload(self) -> None:
        if self.dispatch_loop.is_running():
            self.dispatch_loop.cancel()

    def add_schedule(self, guild_id: int, channel_id: int, author_id: int, message: str, send_at: datetime) -> int:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute(
                """
                INSERT INTO scheduled_messages(guild_id, channel_id, author_id, message, send_at)
                VALUES(?,?,?,?,?)
                """,
                (guild_id, channel_id, author_id, message, send_at.isoformat()),
            )
            con.commit()
            return int(cur.lastrowid)

    @app_commands.command(name="schedulemsg", description="Schedule a message to be sent at a specific time.")
    @app_commands.describe(
        channel="Channel to post the message (defaults to current channel if omitted)",
    )
    async def schedule_message(
        self,
        interaction: Interaction,
        channel: TextChannel | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return
        target_channel: TextChannel | None = channel
        if target_channel is None and isinstance(interaction.channel, TextChannel):
            target_channel = interaction.channel
        if target_channel is None:
            await interaction.response.send_message("Please choose a text channel to schedule the message in.", ephemeral=True)
            return

        if not target_channel.permissions_for(interaction.user).send_messages:
            await interaction.response.send_message("You do not have permission to send messages in that channel.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Pick a date and time for your message in {target_channel.mention}, then press **Continue** to enter the message. "
            f"(Default timezone: {_parse_timezone('')[1]})",
            view=SchedulePickerView(self, target_channel, interaction.user.id),
            ephemeral=True,
        )

    @commands.command(name="remind")
    async def remind(self, ctx: commands.Context, role: discord.Role, time_24h: str, *, reminder: str = "") -> None:
        """Schedule a reminder to ping a role at a 24h time (HH:MM)."""
        if not ctx.guild:
            await ctx.send("Use this command inside a server.")
            return
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("Please use this command in a text channel.")
            return
        if not ctx.channel.permissions_for(ctx.author).send_messages:
            await ctx.send("You do not have permission to send messages in this channel.")
            return

        tzinfo, tz_label = _parse_timezone("")
        if tzinfo is None:
            tzinfo = timezone.utc
            tz_label = "UTC"

        target_local = _parse_24h_time_only(time_24h, tzinfo)
        if target_local is None:
            await ctx.send("Time must be in 24-hour `HH:MM` format (example: `14:30`).")
            return

        target_dt = target_local.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        if target_dt <= now + timedelta(seconds=30):
            await ctx.send(
                f"That time is too soon. Pick a time at least 30 seconds from now "
                f"({discord.utils.format_dt(target_dt, style='R')}) using timezone '{tz_label}'."
            )
            return

        reminder_text = reminder.strip() or "reminder."
        content = f"{role.mention} {reminder_text}"

        schedule_id = self.add_schedule(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            author_id=ctx.author.id,
            message=content,
            send_at=target_dt,
        )

        await ctx.send(
            f"Scheduled reminder #{schedule_id} for {ctx.channel.mention} at "
            f"{discord.utils.format_dt(target_dt, style='F')} ({discord.utils.format_dt(target_dt, style='R')}) "
            f"using timezone '{tz_label}' for role '{role.name}'."
        )

    def fetch_due(self) -> list[tuple[int, int, int, int, str, str]]:
        now_iso = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute(
                """
                SELECT id, guild_id, channel_id, author_id, message, send_at
                FROM scheduled_messages
                WHERE send_at <= ?
                ORDER BY send_at ASC
                LIMIT 10
                """,
                (now_iso,),
            ).fetchall()
        return [(int(r[0]), int(r[1]), int(r[2]), int(r[3]), str(r[4]), str(r[5])) for r in rows]

    def delete_row(self, schedule_id: int) -> None:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("DELETE FROM scheduled_messages WHERE id=?", (schedule_id,))
            con.commit()

    async def notify_failure(self, user_id: int, reason: str) -> None:
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                return
        try:
            await user.send(f"Your scheduled message could not be delivered: {reason}")
        except (discord.Forbidden, discord.HTTPException):
            return

    async def deliver(self, row: tuple[int, int, int, int, str, str]) -> None:
        schedule_id, _guild_id, channel_id, author_id, content, send_at = row
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if not isinstance(channel, discord.TextChannel):
            await self.notify_failure(author_id, "Target channel no longer exists.")
            self.delete_row(schedule_id)
            return

        try:
            await channel.send(content)
        except discord.Forbidden:
            await self.notify_failure(author_id, f"No permission to send in {channel.mention}.")
        except discord.HTTPException as exc:
            await self.notify_failure(author_id, f"Failed to send at {send_at}: {exc}")
        finally:
            self.delete_row(schedule_id)

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def dispatch_loop(self) -> None:
        due = self.fetch_due()
        for row in due:
            await self.deliver(row)

    @dispatch_loop.before_loop
    async def before_dispatch_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(MessageScheduler(bot))


class ScheduleMessageModal(Modal, title="Schedule message"):
    def __init__(
        self,
        scheduler: MessageScheduler,
        channel: TextChannel,
        year: int,
        month: int,
        day: str,
        time_value: str,
        tz_value: str,
        allow_custom_tz: bool = False,
        require_day_override: bool = False,
    ):
        super().__init__(timeout=300)
        self.scheduler = scheduler
        self.channel = channel
        self.year = year
        self.month = month
        self.day = day
        self.time_value = time_value
        self.tz_value = tz_value
        self.allow_custom_tz = allow_custom_tz
        self.require_day_override = require_day_override

        self.message_input = TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            max_length=2000,
            placeholder="What should the bot post?",
        )
        self.add_item(self.message_input)

        self.minute_input = TextInput(
            label="Minutes (00-59, optional)",
            required=False,
            max_length=2,
            placeholder="00",
        )
        self.add_item(self.minute_input)

        if require_day_override:
            self.day_override = TextInput(
                label="Day (25-31)",
                required=True,
                max_length=2,
                placeholder="25",
            )
            self.add_item(self.day_override)
        else:
            self.day_override = None

        if allow_custom_tz:
            self.tz_input = TextInput(
                label="Timezone",
                required=False,
                placeholder="UTC or America/Los_Angeles (default uses your selection)",
                max_length=64,
            )
            self.add_item(self.tz_input)
        else:
            self.tz_input = None

    async def on_submit(self, interaction: Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        if not self.channel.permissions_for(interaction.user).send_messages:
            await interaction.response.send_message("You no longer have permission to send messages in that channel.", ephemeral=True)
            return

        message = (self.message_input.value or "").strip()
        if not message:
            await interaction.response.send_message("Message cannot be empty.", ephemeral=True)
            return

        # Resolve day
        if self.require_day_override:
            day_raw = (self.day_override.value or "").strip() if self.day_override else ""
            try:
                day = int(day_raw)
            except ValueError:
                await interaction.response.send_message("Please enter a day between 25 and 31.", ephemeral=True)
                return
            if day < 25 or day > 31:
                await interaction.response.send_message("Day must be between 25 and 31.", ephemeral=True)
                return
        else:
            try:
                day = int(self.day)
            except ValueError:
                await interaction.response.send_message("Invalid day selection.", ephemeral=True)
                return

        # Resolve minutes
        minute_raw = (self.minute_input.value or "").strip()
        if minute_raw:
            try:
                minute = int(minute_raw)
            except ValueError:
                await interaction.response.send_message("Minutes must be a number between 0 and 59.", ephemeral=True)
                return
            if minute < 0 or minute > 59:
                await interaction.response.send_message("Minutes must be between 0 and 59.", ephemeral=True)
                return
        else:
            minute = 0

        tz_name = (self.tz_input.value or "").strip() if self.tz_input else self.tz_value
        hour = self.time_value.split(":")[0]
        raw_time = f"{self.year:04d}-{self.month:02d}-{day:02d} {hour}:{minute:02d}"

        target_dt, tz_label, error = parse_schedule_datetime(raw_time, tz_name)
        if error or target_dt is None:
            await interaction.response.send_message(error or "Could not parse the provided time.", ephemeral=True)
            return

        now = datetime.now(timezone.utc)
        if target_dt <= now + timedelta(seconds=30):
            await interaction.response.send_message(
                f"That resolves to {discord.utils.format_dt(target_dt, style='F')} "
                f"({discord.utils.format_dt(target_dt, style='R')}) using timezone '{tz_label}'. "
                "Please pick a time at least 30 seconds from now or adjust the timezone.",
                ephemeral=True,
            )
            return

        schedule_id = self.scheduler.add_schedule(
            guild_id=interaction.guild.id,
            channel_id=self.channel.id,
            author_id=interaction.user.id,
            message=message,
            send_at=target_dt,
        )

        await interaction.response.send_message(
            f"Scheduled message #{schedule_id} for {self.channel.mention} at "
            f"{discord.utils.format_dt(target_dt, style='F')} ({discord.utils.format_dt(target_dt, style='R')}).",
            ephemeral=True,
        )


class SchedulePickerView(View):
    def __init__(self, scheduler: MessageScheduler, channel: TextChannel, user_id: int):
        super().__init__(timeout=300)
        self.scheduler = scheduler
        self.channel = channel
        self.user_id = user_id
        now = datetime.now()
        current_year = now.year

        months = [
            discord.SelectOption(label="January", value="1"),
            discord.SelectOption(label="February", value="2"),
            discord.SelectOption(label="March", value="3"),
            discord.SelectOption(label="April", value="4"),
            discord.SelectOption(label="May", value="5"),
            discord.SelectOption(label="June", value="6"),
            discord.SelectOption(label="July", value="7"),
            discord.SelectOption(label="August", value="8"),
            discord.SelectOption(label="September", value="9"),
            discord.SelectOption(label="October", value="10"),
            discord.SelectOption(label="November", value="11"),
            discord.SelectOption(label="December", value="12"),
        ]
        years = [
            discord.SelectOption(label=str(current_year + i), value=str(current_year + i))
            for i in range(_YEAR_OPTIONS_SPAN)
        ]
        days = [
            discord.SelectOption(label=str(d), value=str(d)) for d in range(1, 25)
        ] + [
            discord.SelectOption(label="25-31 (enter exact day in modal)", value=_DAY_SPECIAL_VALUE)
        ]

        self.month_select = Select(placeholder="Month", options=months, row=0)
        self.day_select = Select(placeholder="Day", options=days, row=1)
        self.year_select = Select(placeholder="Year", options=years, row=2)
        self.time_select = Select(placeholder="Time (hour, 24h)", options=[discord.SelectOption(label=t, value=t) for t in _TIME_OPTIONS], row=3)

        self.month_select.callback = self._capture
        self.day_select.callback = self._capture
        self.year_select.callback = self._capture
        self.time_select.callback = self._capture

        self.add_item(self.month_select)
        self.add_item(self.day_select)
        self.add_item(self.year_select)
        self.add_item(self.time_select)

    async def interaction_check(self, interaction: Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _capture(self, interaction: Interaction):
        # Defer so the menu stays responsive while the user keeps selecting.
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

    @button(label="Continue", style=discord.ButtonStyle.primary, row=4)
    async def continue_button(self, interaction: Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the user who started this can continue.", ephemeral=True)
            return

        try:
            month = int(self.month_select.values[0])
            day_value = self.day_select.values[0]
            year = int(self.year_select.values[0])
            time_value = self.time_select.values[0]
        except (ValueError, IndexError):
            await interaction.response.send_message("Please pick month, day, year, and time.", ephemeral=True)
            return

        # Validate date/time quickly.
        try:
            hour = int(time_value.split(":")[0])
            minute = int(time_value.split(":")[1])
            datetime(year, month, 1, hour, minute)
        except Exception:
            await interaction.response.send_message("That date/time combination is invalid. Please adjust.", ephemeral=True)
            return

        require_day_override = day_value == _DAY_SPECIAL_VALUE
        allow_custom = True
        tz_value = ""

        await interaction.response.send_modal(
            ScheduleMessageModal(
                scheduler=self.scheduler,
                channel=self.channel,
                year=year,
                month=month,
                day=day_value,
                time_value=time_value,
                tz_value=tz_value,
                allow_custom_tz=allow_custom,
                require_day_override=require_day_override,
            )
        )
