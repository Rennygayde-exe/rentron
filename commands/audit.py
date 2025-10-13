import discord
from discord.ext import commands
from discord import app_commands
import json, os, datetime

SNAPSHOT_DIR = "snapshots"

class AuditSnapshot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)

    @app_commands.command(name="snapshot", description="Create a JSON snapshot of the server’s config.")
    @app_commands.describe(name="Name for the snapshot file")
    @app_commands.checks.has_permissions(administrator=True)
    async def snapshot(self, interaction: discord.Interaction, name: str):
        guild = interaction.guild
        await interaction.response.defer(thinking=True)

        snapshot = {
            "guild_name": guild.name,
            "guild_id": guild.id,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
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

        filename = f"{name}.json" if not name.endswith(".json") else name
        filepath = os.path.join(SNAPSHOT_DIR, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)

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
    async def restore(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(thinking=True)
        guild = interaction.guild

        filename = f"{name}.json" if not name.endswith(".json") else name
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

        # Create categories
        for chdata in snapshot["channels"]:
            if chdata["type"] == "category" and chdata["name"] not in existing_channels:
                try:
                    category = await guild.create_category(chdata["name"], reason=f"Restored from {filename}")
                    category_map[chdata["name"]] = category
                    restored_channels += 1
                    await asyncio.sleep(1)
                except Exception as e:
                    print(f"Category restore failed: {e}")

        # Create text/voice channels
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
