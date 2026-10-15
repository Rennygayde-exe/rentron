import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
import os
import csv
import random
from datetime import datetime, timedelta, timezone
from typing import Literal


CONFIG_FILE = "prune_schedule.json"
LAST_FILE = "last_prune.txt"

DEBUG_MODE = os.getenv("PRUNE_DEBUG") == "1"
LOOP_INTERVAL = 60 if DEBUG_MODE else 1800
UNIT_SECONDS: dict[str, int] = {"minutes": 60, "hours": 3600, "days": 86400}

CONTENT_PRUNE_CHANNEL_ID = int(os.getenv("CONTENT_PRUNE_CHANNEL_ID", "1458933078029238383"))
CONTENT_PRUNE_LOG_CHANNEL_ID = int(os.getenv("CONTENT_PRUNE_LOG_CHANNEL_ID", "1326497954324025364"))
CONTENT_PRUNE_MAX_AGE_DAYS = int(os.getenv("CONTENT_PRUNE_MAX_AGE_DAYS", "14"))
CONTENT_PRUNE_CHECK_INTERVAL_DAYS = int(os.getenv("CONTENT_PRUNE_CHECK_INTERVAL_DAYS", "2"))
CONTENT_PRUNE_LAST_FILE = "content_prune_last.txt"

class Pruning(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.auto_prune_loop.start()
        self.content_prune_loop.start()

    def cog_unload(self):
        self.auto_prune_loop.cancel()
        self.content_prune_loop.cancel()

    def load_content_prune_last(self) -> float:
        if os.path.exists(CONTENT_PRUNE_LAST_FILE):
            with open(CONTENT_PRUNE_LAST_FILE, "r") as f:
                return float(f.read().strip() or 0)
        return 0

    def save_content_prune_last(self, ts: float):
        with open(CONTENT_PRUNE_LAST_FILE, "w") as f:
            f.write(str(ts))

    def load_config(self) -> dict:
        if not os.path.exists(CONFIG_FILE):
            return {}
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}

    def save_config(self, config):
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f)

    def load_last(self):
        if os.path.exists(LAST_FILE):
            with open(LAST_FILE, "r") as f:
                return float(f.read().strip() or 0)
        return 0

    def save_last(self, ts: float):
        with open(LAST_FILE, "w") as f:
            f.write(str(ts))

    def interval_seconds(self, config: dict) -> int | None:
        try:
            interval = int(config.get("interval", 0))
        except (TypeError, ValueError):
            return None
        if interval <= 0:
            return None
        unit = config.get("unit")
        multiplier = UNIT_SECONDS.get(unit)
        if multiplier is None:
            return None
        return interval * multiplier

    async def resolve_text_channel(self, channel_id: int | None) -> discord.TextChannel | None:
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await self.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    def next_timestamp(self, last_ts: float, interval_sec: int) -> float:
        if last_ts > 0:
            return last_ts + interval_sec
        return datetime.now(timezone.utc).timestamp() + interval_sec

    async def safe_delete(self, msg: discord.Message):
        try:
            await msg.delete()
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException as e:
            retry_after = getattr(e, "retry_after", None)
            if retry_after:
                await asyncio.sleep(retry_after + 0.25)
                try:
                    await msg.delete()
                    return True
                except Exception:
                    return False
            return False

    async def delete_many_throttled(self, messages: list[discord.Message], concurrency: int = 5) -> None:
        """Delete messages with several requests in flight at once, relying on
        discord.py's own rate-limit bucket handling for pacing (rather than an
        extra fixed sleep per message on top of it)."""
        sem = asyncio.Semaphore(concurrency)

        async def _bounded(msg: discord.Message):
            async with sem:
                await self.safe_delete(msg)

        await asyncio.gather(*(_bounded(m) for m in messages))

    async def prune_channel(
        self,
        channel: discord.TextChannel,
        cutoff: datetime,
        log_channel: discord.TextChannel | None,
        extra_channel: discord.TextChannel | None = None,
    ) -> int:
        deleted = []
        bulk_candidates = []
        single_candidates = []

        async for msg in channel.history(limit=None, before=cutoff):
            if msg.attachments:
                for att in msg.attachments:
                    deleted.append([msg.id, msg.author.id, msg.created_at.isoformat(), att.url, channel.id])
                age = datetime.now(timezone.utc) - msg.created_at
                if age.days < 14:
                    bulk_candidates.append(msg)
                else:
                    single_candidates.append(msg)

        for i in range(0, len(bulk_candidates), 100):
            chunk = bulk_candidates[i:i+100]
            try:
                await channel.delete_messages(chunk)
            except Exception:
                for m in chunk:
                    await self.safe_delete(m)
            await asyncio.sleep(0.5)

        await self.delete_many_throttled(single_candidates)

        if deleted:
            fn = f"prune_log_{int(datetime.now(timezone.utc).timestamp())}.csv"
            with open(fn, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["id","author","created","url","channel"])
                writer.writerows(deleted)
            if log_channel:
                await log_channel.send(file=discord.File(fn))
            if extra_channel:
                await extra_channel.send(content=f"Deleted {len(deleted)} messages with attachments in {channel.mention}", file=discord.File(fn))
            os.remove(fn)
        elif extra_channel:
            await extra_channel.send(content=f"No messages with attachments found to prune in {channel.mention}")
        return len(deleted)

    async def content_prune_channel(
        self,
        channel: discord.TextChannel,
        cutoff: datetime,
        log_channel: discord.TextChannel | None,
        extra_channel: discord.TextChannel | None = None,
    ) -> int:
        """Delete ALL messages older than cutoff in channel, regardless of attachments."""
        deleted = []
        bulk_candidates = []
        single_candidates = []

        async for msg in channel.history(limit=None, before=cutoff):
            deleted.append([
                msg.id,
                msg.author.id,
                msg.created_at.isoformat(),
                (msg.content or "").replace("\n", " ")[:200],
                channel.id,
            ])
            age = datetime.now(timezone.utc) - msg.created_at
            if age.days < 14:
                bulk_candidates.append(msg)
            else:
                single_candidates.append(msg)

        for i in range(0, len(bulk_candidates), 100):
            chunk = bulk_candidates[i:i+100]
            try:
                await channel.delete_messages(chunk)
            except Exception:
                for m in chunk:
                    await self.safe_delete(m)
            await asyncio.sleep(0.5)

        await self.delete_many_throttled(single_candidates)

        if deleted:
            fn = f"content_prune_log_{int(datetime.now(timezone.utc).timestamp())}.csv"
            with open(fn, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["id", "author", "created", "content", "channel"])
                writer.writerows(deleted)
            summary = f"Content prune: deleted {len(deleted)} message(s) older than {CONTENT_PRUNE_MAX_AGE_DAYS}d in {channel.mention}"
            if log_channel:
                await log_channel.send(content=summary, file=discord.File(fn))
            if extra_channel:
                await extra_channel.send(content=summary, file=discord.File(fn))
            os.remove(fn)
        elif extra_channel:
            await extra_channel.send(content=f"No messages older than {CONTENT_PRUNE_MAX_AGE_DAYS}d found in {channel.mention}")
        return len(deleted)

    @tasks.loop(seconds=LOOP_INTERVAL)
    async def content_prune_loop(self):
        try:
            await asyncio.sleep(random.uniform(0, 5))
            interval_sec = CONTENT_PRUNE_CHECK_INTERVAL_DAYS * 86400
            last_ts = self.load_content_prune_last()
            now_ts = datetime.now(timezone.utc).timestamp()
            if now_ts - last_ts < interval_sec:
                return
            channel = await self.resolve_text_channel(CONTENT_PRUNE_CHANNEL_ID)
            if not channel:
                return
            log_channel = await self.resolve_text_channel(CONTENT_PRUNE_LOG_CHANNEL_ID)
            cutoff = datetime.now(timezone.utc) - timedelta(days=CONTENT_PRUNE_MAX_AGE_DAYS)
            await self.content_prune_channel(channel, cutoff, log_channel)
            self.save_content_prune_last(now_ts)
        except Exception as e:
            print(f"content_prune_loop error: {e}")

    @content_prune_loop.before_loop
    async def before_content_prune_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=LOOP_INTERVAL)
    async def auto_prune_loop(self):
        try:
            print(f"auto_prune_loop tick (interval={LOOP_INTERVAL}s, debug={DEBUG_MODE})")
            await asyncio.sleep(random.uniform(0,5))
            config = self.load_config()
            if not config or "channel_id" not in config:
                return
            interval_sec = self.interval_seconds(config)
            if not interval_sec:
                return
            last_ts = self.load_last()
            now_ts = datetime.now(timezone.utc).timestamp()
            if now_ts - last_ts < interval_sec:
                return
            channel = await self.resolve_text_channel(config.get("channel_id"))
            log_channel = await self.resolve_text_channel(config.get("log_channel_id"))
            if not channel:
                return
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval_sec)
            await self.prune_channel(channel, cutoff, log_channel)
            self.save_last(now_ts)
        except Exception as e:
            print(f"auto_prune_loop error: {e}")

    @auto_prune_loop.before_loop
    async def before_auto_prune_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="prune_attachments", description="Manually prune attachments")
    @app_commands.checks.has_permissions(administrator=True)
    async def prune_attachments(self, interaction: discord.Interaction, days: int, channel: discord.TextChannel, images_only: bool = False):
        await interaction.response.defer(ephemeral=True)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        log_channel = await self.resolve_text_channel(self.load_config().get("log_channel_id"))
        await self.prune_channel(channel, cutoff, log_channel, interaction.channel)
        await interaction.followup.send(f"Manual prune executed for {channel.mention}", ephemeral=True)

    @app_commands.command(name="prune", description="Run the configured prune if the schedule allows it")
    @app_commands.checks.has_permissions(administrator=True)
    async def prune(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        config = self.load_config()
        if not config or "channel_id" not in config:
            await interaction.followup.send("No prune config set", ephemeral=True)
            return
        interval_sec = self.interval_seconds(config)
        if not interval_sec:
            await interaction.followup.send("Invalid prune config. Please run /set_prune_config again.", ephemeral=True)
            return
        now_ts = datetime.now(timezone.utc).timestamp()
        last_ts = self.load_last()
        if last_ts and now_ts - last_ts < interval_sec:
            next_ts = self.next_timestamp(last_ts, interval_sec)
            next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)
            await interaction.followup.send(
                f"Too early to prune. Next run scheduled for {discord.utils.format_dt(next_dt, style='F')} ({discord.utils.format_dt(next_dt, style='R')}).",
                ephemeral=True,
            )
            return
        channel = await self.resolve_text_channel(config.get("channel_id"))
        if not channel:
            await interaction.followup.send("Configured channel no longer exists. Please update /set_prune_config.", ephemeral=True)
            return
        log_channel = await self.resolve_text_channel(config.get("log_channel_id"))
        extra_channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval_sec)
        deleted = await self.prune_channel(channel, cutoff, log_channel, extra_channel)
        self.save_last(now_ts)
        next_ts = self.next_timestamp(now_ts, interval_sec)
        next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)
        await interaction.followup.send(
            f"Prune complete in {channel.mention}. Deleted {deleted} message(s). Next run {discord.utils.format_dt(next_dt, style='R')}.",
            ephemeral=True,
        )

    @app_commands.command(name="set_prune_config", description="Set auto prune schedule")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_prune_config(
        self,
        interaction: discord.Interaction,
        interval: int,
        unit: Literal["minutes", "hours", "days"],
        channel: discord.TextChannel,
        log_channel: discord.TextChannel,
    ):
        await interaction.response.defer(ephemeral=True)
        config = {"interval": interval, "unit": unit, "channel_id": channel.id, "log_channel_id": log_channel.id}
        self.save_config(config)
        self.save_last(0)
        interval_sec = self.interval_seconds(config)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval_sec or 0)
        await self.prune_channel(channel, cutoff, log_channel, interaction.channel)
        self.save_last(datetime.now(timezone.utc).timestamp())
        await interaction.followup.send(f"Auto prune every {interval} {unit} in {channel.mention}, logs in {log_channel.mention}", ephemeral=True)

    @app_commands.command(name="forcerun", description="Force an immediate auto-prune run with current config")
    @app_commands.checks.has_permissions(administrator=True)
    async def forcerun(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        config = self.load_config()
        if not config:
            await interaction.followup.send("No valid prune config set", ephemeral=True)
            return
        channel = await self.resolve_text_channel(config.get("channel_id"))
        log_channel = await self.resolve_text_channel(config.get("log_channel_id"))
        if not channel:
            await interaction.followup.send("Configured channel not found", ephemeral=True)
            return
        interval_sec = self.interval_seconds(config)
        if not interval_sec:
            await interaction.followup.send("Invalid prune config. Please run /set_prune_config.", ephemeral=True)
            return
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval_sec)
        await self.prune_channel(channel, cutoff, log_channel, interaction.channel)
        self.save_last(datetime.now(timezone.utc).timestamp())
        await interaction.followup.send(f"Forced prune executed for {channel.mention}", ephemeral=True)

    @app_commands.command(name="next_prune", description="Show next scheduled prune time")
    @app_commands.checks.has_permissions(administrator=True)
    async def next_prune(self, interaction: discord.Interaction):
        config = self.load_config()
        if not config:
            await interaction.response.send_message("No prune config set", ephemeral=True)
            return
        last_ts = self.load_last()
        interval_sec = self.interval_seconds(config)
        if not interval_sec:
            await interaction.response.send_message("Stored config invalid. Please re-run /set_prune_config.", ephemeral=True)
            return
        next_ts = self.next_timestamp(last_ts, interval_sec)
        next_time = datetime.fromtimestamp(next_ts, tz=timezone.utc)
        await interaction.response.send_message(
            f"Next prune scheduled for {discord.utils.format_dt(next_time, style='F')} ({discord.utils.format_dt(next_time, style='R')})",
            ephemeral=True,
        )

    @app_commands.command(name="content_prune_forcerun", description="Force an immediate content prune run (deletes all messages older than the configured age)")
    @app_commands.checks.has_permissions(administrator=True)
    async def content_prune_forcerun(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel = await self.resolve_text_channel(CONTENT_PRUNE_CHANNEL_ID)
        if not channel:
            await interaction.followup.send("Configured content-prune channel not found", ephemeral=True)
            return
        log_channel = await self.resolve_text_channel(CONTENT_PRUNE_LOG_CHANNEL_ID)
        cutoff = datetime.now(timezone.utc) - timedelta(days=CONTENT_PRUNE_MAX_AGE_DAYS)
        deleted = await self.content_prune_channel(channel, cutoff, log_channel, interaction.channel)
        self.save_content_prune_last(datetime.now(timezone.utc).timestamp())
        await interaction.followup.send(f"Forced content prune executed for {channel.mention}. Deleted {deleted} message(s).", ephemeral=True)

    @app_commands.command(name="content_prune_next", description="Show next scheduled content prune time")
    @app_commands.checks.has_permissions(administrator=True)
    async def content_prune_next(self, interaction: discord.Interaction):
        last_ts = self.load_content_prune_last()
        interval_sec = CONTENT_PRUNE_CHECK_INTERVAL_DAYS * 86400
        next_ts = self.next_timestamp(last_ts, interval_sec)
        next_time = datetime.fromtimestamp(next_ts, tz=timezone.utc)
        await interaction.response.send_message(
            f"Next content prune scheduled for {discord.utils.format_dt(next_time, style='F')} ({discord.utils.format_dt(next_time, style='R')})",
            ephemeral=True,
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(Pruning(bot))
