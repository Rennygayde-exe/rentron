import io, csv, json, asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks

UNIT_SECONDS = {
    "minutes": 60,
    "hours":   3600,
    "days":    86400,
    "weeks":   604800,
}
IMG_EXTS = (".png",".jpg",".jpeg",".gif",".bmp",".webp",".heic",".heif",".avif")

class Cleanup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.schedule_file = Path("prune_schedule.json")
        self.last_file = Path("last_prune.txt")

    def _get_last(self) -> datetime | None:
        if self.last_file.exists():
            try:
                return datetime.fromtimestamp(int(self.last_file.read_text().strip()), tz=timezone.utc)
            except:
                return None
        return None

    def _set_last(self, dt: datetime):
        self.last_file.write_text(str(int(dt.timestamp())))

    def _load_cfg(self) -> dict:
        if self.schedule_file.exists():
            try:
                return json.loads(self.schedule_file.read_text(encoding="utf-8"))
            except:
                pass
        return {"interval": 72, "unit": "hours", "channel_id": None, "log_channel_id": None}

    def _save_cfg(self, cfg: dict):
        self.schedule_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))

    async def _prune_attachments(
        self,
        interaction: Interaction,
        channel: discord.TextChannel,
        amount: int,
        unit: Literal["minutes", "hours", "days", "weeks"],
        attach_type: Literal["all", "images"],
        log_channel: discord.TextChannel | None,
    ) -> list[dict]:
        if not interaction.guild:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return []
        if not interaction.user.guild_permissions.manage_messages and not any(r.name == "Staff" for r in getattr(interaction.user, "roles", [])):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return []

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)


        perms = getattr(interaction.user, "guild_permissions", None)
        has_manage = bool(getattr(perms, "manage_messages", False))
        if not has_manage and not any(r.name == "Staff" for r in getattr(interaction.user, "roles", [])):
            await interaction.followup.send("No permission.", ephemeral=True)
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(**{unit: amount})

        deleted_rows: list[dict] = []
        deleted_count = failed = checked = 0
        progress = await interaction.followup.send("Scanning… 0 checked / 0 deleted", ephemeral=True)

        async for msg in channel.history(limit=None, before=cutoff, oldest_first=False):
            checked += 1
            if not msg.attachments:
                if checked % 50 == 0:
                    await progress.edit(content=f"Scanning… {checked} checked / {deleted_count} deleted")
                continue

            if attach_type == "images":
                if not any(
                    (a.content_type or "").startswith("image/") or (a.filename or "").lower().endswith(IMG_EXTS)
                    for a in msg.attachments
                ):
                    if checked % 50 == 0:
                        await progress.edit(content=f"Scanning… {checked} checked / {deleted_count} deleted")
                    continue

            att_url = msg.attachments[0].url if msg.attachments else ""
            try:
                await msg.delete()
                deleted_rows.append({
                    "id":        str(msg.id),
                    "author":    f"{msg.author} ({msg.author.id})",
                    "created":   msg.created_at.isoformat(),
                    "attachment": att_url,
                    "channel":   (getattr(msg.channel, "name", None) or str(msg.channel.id)),
                })
                deleted_count += 1
                await asyncio.sleep(0.7)
            except Exception:
                failed += 1

            if checked % 50 == 0:
                await progress.edit(content=f"Scanning… {checked} checked / {deleted_count} deleted")

        await progress.edit(content=f"Done—deleted {deleted_count} item(s). Failed: {failed}")

        if deleted_rows:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=["id", "author", "created", "attachment", "channel"])
            w.writeheader()
            w.writerows(deleted_rows)
            buf.seek(0)
            file = discord.File(io.BytesIO(buf.read().encode()), filename=f"pruned_{channel.id}.csv")
            summary = f"Pruned {len(deleted_rows)} message(s) in {channel.mention} older than {amount} {unit}."
            if log_channel:
                try:
                    await log_channel.send(summary, file=file)
                except Exception:
                    await interaction.followup.send("Failed to post results to the log channel.", ephemeral=True)
            else:
                await interaction.followup.send(summary, file=file, ephemeral=True)
        else:
            await interaction.followup.send("No messages matched.", ephemeral=True)

        self._set_last(datetime.now(timezone.utc))
        return deleted_rows


    # Background auto-prune loop (every 30 min)
    @tasks.loop(minutes=30)
    async def auto_prune_loop(self):
        await self.bot.wait_until_ready()
        cfg = self._load_cfg()
        chan_id = cfg.get("channel_id")
        if not chan_id:
            return
        channel = self.bot.get_channel(chan_id)
        if not isinstance(channel, discord.TextChannel):
            return
        log_channel = self.bot.get_channel(cfg.get("log_channel_id")) if cfg.get("log_channel_id") else None

        amt = int(cfg.get("interval", 72))
        unit = cfg.get("unit", "hours")
        if unit not in UNIT_SECONDS:
            return

        now = datetime.now(timezone.utc)
        last = self._get_last()
        threshold = amt * UNIT_SECONDS[unit]
        if last and (now - last).total_seconds() < threshold:
            return

        class _DummyResp:
            def is_done(self): return True
            async def defer(self, **_): pass
        class _DummyMsg:
            async def edit(self, **_): pass
        class _DummyFollowup:
            async def send(self, *_, **__): return _DummyMsg()
        class _Dummy:
            def __init__(self, g):
                self.user = type("U", (), {"roles":[type("R", (), {"name":"Staff"})()]})()
                self.guild = g
                self.response = _DummyResp()
                self.followup = _DummyFollowup()

        try:
            dummy = _Dummy(channel.guild)
            await self._prune_attachments(dummy, channel, amt, unit, "all", log_channel)
            self._set_last(now)
        except Exception:
            pass

    @app_commands.command(name="prune_attachments", description="Delete old attachments in a channel.")
    @app_commands.describe(
        channel="Channel to scan",
        amount="Age amount",
        unit="Age unit",
        attach_type="Attachment type",
        log_channel="Channel to post results (optional)",
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    async def prune_attachments_cmd(
        self,
        interaction: Interaction,
        channel: discord.TextChannel,
        amount: app_commands.Range[int, 1, 3650],
        unit: Literal["minutes", "hours", "days", "weeks"],
        attach_type: Literal["all", "images"],
        log_channel: discord.TextChannel | None = None,
    ):
        await self._prune_attachments(interaction, channel, amount, unit, attach_type, log_channel)

    @app_commands.command(name="set_prune_config", description="Configure auto-prune.")
    @app_commands.describe(
        channel="Where to prune",
        amount="Interval amount",
        unit="Unit",
        log_channel="Where to post results (optional)"
    )
    async def set_prune_config(
        self,
        interaction: Interaction,
        channel: discord.TextChannel,
        amount: app_commands.Range[int,1,3650],
        unit: Literal["minutes","hours","days","weeks"],
        log_channel: discord.TextChannel | None = None
    ):
        if not interaction.user.guild_permissions.manage_messages and not any(r.name == "Staff" for r in getattr(interaction.user, "roles", [])):
            await interaction.response.send_message("No permission.", ephemeral=True); return
        cfg = {"channel_id": channel.id, "interval": int(amount), "unit": unit, "log_channel_id": (log_channel.id if log_channel else None)}
        self._save_cfg(cfg)
        await interaction.response.send_message(
            f"Auto-prune set: every {amount} {unit} in {channel.mention}" + (f", results -> {log_channel.mention}" if log_channel else ""),
            ephemeral=True
        )

    @app_commands.command(name="next_prune", description="Show next scheduled prune.")
    async def next_prune(self, interaction: Interaction):
        cfg = self._load_cfg()
        last = self._get_last()
        now  = datetime.now(timezone.utc)
        secs = cfg["interval"] * UNIT_SECONDS[cfg["unit"]]
        nxt = (last + timedelta(seconds=secs)) if last else (now + timedelta(seconds=secs))
        chan = interaction.guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
        await interaction.response.send_message(
            f"Next prune: <t:{int(nxt.timestamp())}:F> • Target: {chan.mention if chan else '`unset`'}",
            ephemeral=True
        )

    async def cog_load(self):
        self.auto_prune_loop.start()

    async def cog_unload(self):
        self.auto_prune_loop.cancel()

async def setup(bot: commands.Bot):
    await bot.add_cog(Cleanup(bot))