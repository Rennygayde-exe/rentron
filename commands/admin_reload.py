import discord
from discord.ext import commands
from discord import app_commands

class AdminReload(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="sync", description="Sync slash commands with Discord")
    @app_commands.checks.has_permissions(administrator=True)
    async def sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            synced = await self.bot.tree.sync()
            await interaction.followup.send(
                f"Synced {len(synced)} commands globally.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                f"Sync failed: {e}",
                ephemeral=True
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminReload(bot))
