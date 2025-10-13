import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
import os
import csv
import random
from datetime import datetime, timedelta, timezone


CONFIG_FILE = "prune_schedule.json"
LAST_FILE = "last_prune.txt"

DEBUG_MODE = os.getenv("PRUNE_DEBUG") == "1"
LOOP_INTERVAL = 60 if DEBUG_MODE else 1800

class Pruning(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.auto_prune_loop.start()

    def cog_unload(self):
        self.auto_prune_loop.cancel()

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
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

    async def prune_channel(self, channel: discord.TextChannel, cutoff: datetime, log_channel: discord.TextChannel, extra_channel: discord.TextChannel = None):
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

        # Bulk delete in chunks
        for i in range(0, len(bulk_candidates), 100):
            chunk = bulk_candidates[i:i+100]
            try:
                await channel.delete_messages(chunk)
            except Exception:
                for m in chunk:
                    await self.safe_delete(m)
            await asyncio.sleep(0.5)

        # Single deletes
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

    @tasks.loop(seconds=LOOP_INTERVAL)
    async def auto_prune_loop(self):
        try:
            print(f"auto_prune_loop tick (interval={LOOP_INTERVAL}s, debug={DEBUG_MODE})")
            await asyncio.sleep(random.uniform(0,5))  # jitter
            config = self.load_config()
            if not config or "channel_id" not in config or "interval" not in config or "unit" not in config:
                return
            last_ts = self.load_last()
            now_ts = datetime.utcnow().timestamp()
            unit = config["unit"]
            mult = {"minutes":60,"hours":3600,"days":86400}.get(unit,3600)
            interval_sec = config["interval"] * mult
            if now_ts - last_ts < interval_sec:
                return
            channel = self.bot.get_channel(config["channel_id"])
            log_channel = self.bot.get_channel(config.get("log_channel_id"))
            if not channel:
                return
            cutoff = datetime.utcnow() - timedelta(seconds=interval_sec)
            await self.prune_channel(channel, cutoff, log_channel)
            self.save_last(now_ts)
        except Exception as e:
            print(f"auto_prune_loop error: {e}")

    @app_commands.command(name="prune_attachments", description="Manually prune attachments")
    async def prune_attachments(self, interaction: discord.Interaction, days: int, channel: discord.TextChannel, images_only: bool = False):
        await interaction.response.defer(ephemeral=True)
        cutoff = datetime.utcnow() - timedelta(days=days)
        log_channel = self.bot.get_channel(self.load_config().get("log_channel_id"))
        await self.prune_channel(channel, cutoff, log_channel, interaction.channel)
        await interaction.followup.send(f"Manual prune executed for {channel.mention}", ephemeral=True)

    @app_commands.command(name="set_prune_config", description="Set auto prune schedule")
    async def set_prune_config(self, interaction: discord.Interaction, interval: int, unit: str, channel: discord.TextChannel, log_channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        config = {"interval": interval, "unit": unit, "channel_id": channel.id, "log_channel_id": log_channel.id}
        self.save_config(config)
        self.save_last(0)
        cutoff = datetime.utcnow() - timedelta(seconds=interval * {"minutes":60,"hours":3600,"days":86400}.get(unit,3600))
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
        channel = self.bot.get_channel(config["channel_id"])
        log_channel = self.bot.get_channel(config.get("log_channel_id"))
        if not channel:
            await interaction.followup.send("Configured channel not found", ephemeral=True)
            return
        unit = config["unit"]
        mult = {"minutes":60,"hours":3600,"days":86400}.get(unit,3600)
        interval_sec = config["interval"] * mult
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
        unit = config["unit"]
        mult = {"minutes":60,"hours":3600,"days":86400}.get(unit,3600)
        interval_sec = config["interval"] * mult
        next_time = datetime.utcfromtimestamp(last_ts + interval_sec)
        await interaction.response.send_message(f"Next prune at {next_time} UTC", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Pruning(bot))
