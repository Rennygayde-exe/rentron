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

class Pruning(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.auto_prune_loop.start()

    def cog_unload(self):
        self.auto_prune_loop.cancel()

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
        return datetime.utcnow().timestamp() + interval_sec

    async def safe_delete(self, msg: discord.Message):
        try:
            await msg.delete()
            await asyncio.sleep(0.12)
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

        for m in single_candidates:
            await self.safe_delete(m)

        if deleted:
            fn = f"prune_log_{int(datetime.utcnow().timestamp())}.csv"
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
            now_ts = datetime.utcnow().timestamp()
            if now_ts - last_ts < interval_sec:
                return
            channel = await self.resolve_text_channel(config.get("channel_id"))
            log_channel = await self.resolve_text_channel(config.get("log_channel_id"))
            if not channel:
                return
            cutoff = datetime.utcnow() - timedelta(seconds=interval_sec)
            await self.prune_channel(channel, cutoff, log_channel)
            self.save_last(now_ts)
        except Exception as e:
            print(f"auto_prune_loop error: {e}")

    @auto_prune_loop.before_loop
    async def before_auto_prune_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="prune_attachments", description="Manually prune attachments")
    async def prune_attachments(self, interaction: discord.Interaction, days: int, channel: discord.TextChannel, images_only: bool = False):
        await interaction.response.defer(ephemeral=True)
        cutoff = datetime.utcnow() - timedelta(days=days)
        log_channel = await self.resolve_text_channel(self.load_config().get("log_channel_id"))
        await self.prune_channel(channel, cutoff, log_channel, interaction.channel)
        await interaction.followup.send(f"Manual prune executed for {channel.mention}", ephemeral=True)

    @app_commands.command(name="prune", description="Run the configured prune if the schedule allows it")
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
        now_ts = datetime.utcnow().timestamp()
        last_ts = self.load_last()
        if last_ts and now_ts - last_ts < interval_sec:
            next_ts = self.next_timestamp(last_ts, interval_sec)
            next_dt = datetime.utcfromtimestamp(next_ts)
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
        cutoff = datetime.utcnow() - timedelta(seconds=interval_sec)
        deleted = await self.prune_channel(channel, cutoff, log_channel, extra_channel)
        self.save_last(now_ts)
        next_ts = self.next_timestamp(now_ts, interval_sec)
        next_dt = datetime.utcfromtimestamp(next_ts)
        await interaction.followup.send(
            f"Prune complete in {channel.mention}. Deleted {deleted} message(s). Next run {discord.utils.format_dt(next_dt, style='R')}.",
            ephemeral=True,
        )

    @app_commands.command(name="set_prune_config", description="Set auto prune schedule")
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
        cutoff = datetime.utcnow() - timedelta(seconds=interval_sec or 0)
        await self.prune_channel(channel, cutoff, log_channel, interaction.channel)
        self.save_last(datetime.utcnow().timestamp())
        await interaction.followup.send(f"Auto prune every {interval} {unit} in {channel.mention}, logs in {log_channel.mention}", ephemeral=True)

    @app_commands.command(name="forcerun", description="Force an immediate auto-prune run with current config")
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
        cutoff = datetime.utcnow() - timedelta(seconds=interval_sec)
        await self.prune_channel(channel, cutoff, log_channel, interaction.channel)
        self.save_last(datetime.utcnow().timestamp())
        await interaction.followup.send(f"Forced prune executed for {channel.mention}", ephemeral=True)

    @app_commands.command(name="next_prune", description="Show next scheduled prune time")
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
        next_time = datetime.utcfromtimestamp(next_ts)
        await interaction.response.send_message(
            f"Next prune scheduled for {discord.utils.format_dt(next_time, style='F')} ({discord.utils.format_dt(next_time, style='R')})",
            ephemeral=True,
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(Pruning(bot))
