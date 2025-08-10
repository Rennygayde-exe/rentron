import os
import discord
from discord import app_commands
from discord.ext import commands

ALLOWED_USER_ID = 669626735385640993

class AdminReload(commands.Cog):
    def __init__(self, bot): self.bot = bot
    def _ok(self, i): return i.user.id == ALLOWED_USER_ID or i.user.guild_permissions.administrator

    @app_commands.command(name="reload", description="Reload an extension from commands/")
    @app_commands.describe(name="e.g. music, application, moderation")
    async def reload(self, interaction: discord.Interaction, name: str):
        if not self._ok(interaction):
            await interaction.response.send_message("Permission denied.", ephemeral=True); return
        mod = f"commands.{name}" if not name.startswith("commands.") else name
        try:
            await self.bot.reload_extension(mod)
        except commands.ExtensionNotLoaded:
            await self.bot.load_extension(mod)
        except commands.ExtensionNotFound:
            await interaction.response.send_message(f"Not found: {mod}", ephemeral=True); return
        except commands.ExtensionFailed as e:
            await interaction.response.send_message(f"Failed: {e}", ephemeral=True); return
        await interaction.response.send_message(f"Reloaded: {mod}", ephemeral=True)

    @app_commands.command(name="reload_all", description="Reload all extensions in commands/")
    async def reload_all(self, interaction: discord.Interaction):
        if not self._ok(interaction):
            await interaction.response.send_message("Permission denied.", ephemeral=True); return
        failed = []
        for fn in os.listdir("commands"):
            if not fn.endswith(".py"): continue
            mod = f"commands.{fn[:-3]}"
            try:
                await self.bot.reload_extension(mod)
            except commands.ExtensionNotLoaded:
                try: await self.bot.load_extension(mod)
                except Exception: failed.append(mod)
            except Exception: failed.append(mod)
        msg = "OK" if not failed else "Failed: " + ", ".join(failed)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="sync", description="Sync slash commands")
    async def sync(self, interaction: discord.Interaction):
        if not self._ok(interaction):
            await interaction.response.send_message("Permission denied.", ephemeral=True); return
        await self.bot.tree.sync()
        await interaction.response.send_message("Synced.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminReload(bot))
