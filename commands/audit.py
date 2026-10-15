import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import re
import datetime

SNAPSHOT_DIR = "snapshots"
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

SNAPSHOT_LOG_CHANNEL_ID = int(os.getenv("SNAPSHOT_LOG_CHANNEL_ID", "1326497954324025364"))
SNAPSHOT_AUTO_INTERVAL_DAYS = int(os.getenv("SNAPSHOT_AUTO_INTERVAL_DAYS", "7"))
SNAPSHOT_AUTO_GUILD_ID = int(os.getenv("HOME_GUILD_ID", os.getenv("GUILD_ID", "0")))
SNAPSHOT_LAST_FILE = "snapshot_last.txt"
SNAPSHOT_CHECK_INTERVAL_SECONDS = 1800


def _safe_snapshot_filename(name: str) -> str | None:
    name = os.path.basename(name.strip())
    if name.endswith(".json"):
        name = name[:-5]
    if not name or not _SAFE_NAME_RE.match(name):
        return None
    return f"{name}.json"


def _build_snapshot(guild: discord.Guild) -> dict:
    snapshot = {
        "guild_name": guild.name,
        "guild_id": guild.id,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "roles": [],
        "channels": []
    }

    for role in guild.roles:
        snapshot["roles"].append({
            "id": role.id,
            "name": role.name,
            "permissions": role.permissions.value,
            "position": role.position,
            "color": role.color.value
        })

    for ch in guild.channels:
        overwrites = {}
        for target, perm in ch.overwrites.items():
            overwrites[str(target.id)] = {
                "allow": perm.pair()[0].value,
                "deny": perm.pair()[1].value
            }

        snapshot["channels"].append({
            "id": ch.id,
            "name": ch.name,
            "type": str(ch.type),
            "category": ch.category.name if ch.category else None,
            "position": ch.position,
            "overwrites": overwrites
        })

    return snapshot

class AuditSnapshot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        self.auto_snapshot_loop.start()

    def cog_unload(self):
        self.auto_snapshot_loop.cancel()

    def load_last(self) -> float:
        if os.path.exists(SNAPSHOT_LAST_FILE):
            with open(SNAPSHOT_LAST_FILE, "r") as f:
                return float(f.read().strip() or 0)
        return 0

    def save_last(self, ts: float):
        with open(SNAPSHOT_LAST_FILE, "w") as f:
            f.write(str(ts))

    async def resolve_log_channel(self) -> discord.TextChannel | None:
        if not SNAPSHOT_LOG_CHANNEL_ID:
            return None
        channel = self.bot.get_channel(SNAPSHOT_LOG_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await self.bot.fetch_channel(SNAPSHOT_LOG_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def _save_snapshot_file(self, guild: discord.Guild, filename: str) -> tuple[dict, str]:
        snapshot = _build_snapshot(guild)
        filepath = os.path.join(SNAPSHOT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)
        return snapshot, filepath

    @tasks.loop(seconds=SNAPSHOT_CHECK_INTERVAL_SECONDS)
    async def auto_snapshot_loop(self):
        try:
            interval_sec = SNAPSHOT_AUTO_INTERVAL_DAYS * 86400
            last_ts = self.load_last()
            now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
            if now_ts - last_ts < interval_sec:
                return
            if not SNAPSHOT_AUTO_GUILD_ID:
                return
            guild = self.bot.get_guild(SNAPSHOT_AUTO_GUILD_ID)
            if not guild:
                return
            ts_label = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"auto_{ts_label}.json"
            snapshot, filepath = await self._save_snapshot_file(guild, filename)
            log_channel = await self.resolve_log_channel()
            if log_channel:
                await log_channel.send(
                    content=f"Scheduled snapshot: `{filename}` ({len(snapshot['roles'])} roles, {len(snapshot['channels'])} channels)",
                    file=discord.File(filepath, filename=filename),
                )
            self.save_last(now_ts)
        except Exception as e:
            print(f"auto_snapshot_loop error: {e}")

    @auto_snapshot_loop.before_loop
    async def before_auto_snapshot_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="snapshot", description="Create a JSON snapshot of the server's config.")
    @app_commands.describe(name="Name for the snapshot file")
    @app_commands.checks.has_permissions(administrator=True)
    async def snapshot(self, interaction: discord.Interaction, name: str):
        guild = interaction.guild
        await interaction.response.defer(thinking=True)

        filename = _safe_snapshot_filename(name)
        if filename is None:
            return await interaction.followup.send(
                "Invalid snapshot name - use only letters, numbers, `_` and `-`."
            )
        snapshot, filepath = await self._save_snapshot_file(guild, filename)

        log_channel = await self.resolve_log_channel()
        if log_channel:
            try:
                await log_channel.send(
                    content=f"Manual snapshot: `{filename}` by {interaction.user.mention} ({len(snapshot['roles'])} roles, {len(snapshot['channels'])} channels)",
                    file=discord.File(filepath, filename=filename),
                )
            except Exception:
                pass

        embed = discord.Embed(
            title="Backup Snapshot Created!",
            description=f"Snapshot `{filename}` saved to `{SNAPSHOT_DIR}/`",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Roles", value=len(snapshot["roles"]))
        embed.add_field(name="Channels", value=len(snapshot["channels"]))
        await interaction.followup.send(embed=embed)

class AuditRestore(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="restore", description="Restore the server structure from a snapshot JSON.")
    @app_commands.describe(name="Name of the snapshot file to restore from")
    @app_commands.checks.has_permissions(administrator=True)
    async def restore(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(thinking=True)
        guild = interaction.guild

        filename = _safe_snapshot_filename(name)
        if filename is None:
            return await interaction.followup.send(
                "Invalid snapshot name - use only letters, numbers, `_` and `-`."
            )
        filepath = os.path.join(SNAPSHOT_DIR, filename)
        if not os.path.exists(filepath):
            return await interaction.followup.send(f"Snapshot `{filename}` not found in `{SNAPSHOT_DIR}/`")

        with open(filepath, "r", encoding="utf-8") as f:
            snapshot = json.load(f)

        restored_roles = 0
        restored_channels = 0

        existing_roles = {r.name: r for r in guild.roles}
        for rdata in snapshot["roles"]:
            if rdata["name"] in existing_roles:
                continue
            try:
                await guild.create_role(
                    name=rdata["name"],
                    permissions=discord.Permissions(rdata["permissions"]),
                    color=discord.Color(rdata["color"]),
                    reason=f"Restored from snapshot {filename}"
                )
                restored_roles += 1
                await asyncio.sleep(1)
            except Exception as e:
                print(f"Role restore failed: {e}")

        existing_channels = {c.name: c for c in guild.channels}
        category_map = {}

        for chdata in snapshot["channels"]:
            if chdata["type"] == "category" and chdata["name"] not in existing_channels:
                try:
                    category = await guild.create_category(chdata["name"], reason=f"Restored from {filename}")
                    category_map[chdata["name"]] = category
                    restored_channels += 1
                    await asyncio.sleep(1)
                except Exception as e:
                    print(f"Category restore failed: {e}")

        for chdata in snapshot["channels"]:
            if chdata["type"] in ("text", "voice") and chdata["name"] not in existing_channels:
                try:
                    category = category_map.get(chdata["category"])
                    if chdata["type"] == "text":
                        new_ch = await guild.create_text_channel(chdata["name"], category=category)
                    else:
                        new_ch = await guild.create_voice_channel(chdata["name"], category=category)

                    for target_id, perm in chdata.get("overwrites", {}).items():
                        target = guild.get_role(int(target_id)) or guild.get_member(int(target_id))
                        if target:
                            overwrite = discord.PermissionOverwrite.from_pair(
                                discord.Permissions(perm["allow"]),
                                discord.Permissions(perm["deny"])
                            )
                            await new_ch.set_permissions(target, overwrite=overwrite)

                    restored_channels += 1
                    await asyncio.sleep(1)
                except Exception as e:
                    print(f"Channel restore failed: {e}")

        embed = discord.Embed(
            title="🛠️ Server Restore Complete",
            description=f"Restored from `{filename}`",
            color=discord.Color.green()
        )
        embed.add_field(name="Roles Restored", value=str(restored_roles))
        embed.add_field(name="Channels Restored", value=str(restored_channels))
        embed.set_footer(text="Note: Messages and integrations are not restored.")
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(AuditSnapshot(bot))
    await bot.add_cog(AuditRestore(bot))
