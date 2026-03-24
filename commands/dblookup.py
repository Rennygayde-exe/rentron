import os
import re
import sqlite3
import asyncio
from pathlib import Path
import discord
from discord import app_commands, Interaction
from discord.ext import commands

DB_PATH = Path(os.getenv("TMHNET_DB_PATH") or Path(__file__).resolve().parents[1] / "data" / "tmhnet.db")
USER_ID_RE = re.compile(r"\d{17,20}")


def _extract_user_id(value: str) -> str | None:
    match = USER_ID_RE.search(value or "")
    return match.group(0) if match else None


def _fetch_user_row(user_id: str) -> tuple[str | None, str | None, str | None, str | None] | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT discordId, registerIp, name, phone FROM users WHERE discordId=?",
            (str(user_id),),
        ).fetchone()
    if not row:
        return None
    return row[0], row[1], row[2], row[3]


class DbLookup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="dblookup", description="Lookup a user in the TMHNET database.")
    @app_commands.describe(user="Discord @mention or user ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def dblookup(self, interaction: Interaction, user: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = _extract_user_id(user)
        if not user_id:
            await interaction.followup.send("Provide a valid Discord user ID or mention.", ephemeral=True)
            return
        if not DB_PATH.exists():
            await interaction.followup.send(f"Database not found: `{DB_PATH}`", ephemeral=True)
            return
        try:
            row = await asyncio.to_thread(_fetch_user_row, user_id)
        except sqlite3.Error:
            await interaction.followup.send("Database error while reading the users table.", ephemeral=True)
            return
        if not row:
            await interaction.followup.send("No matching user found in the database.", ephemeral=True)
            return
        discord_id, register_ip, name, phone = row
        register_ip = register_ip or "N/A"
        name = name or "N/A"
        phone = phone or "N/A"
        await interaction.followup.send(
            f"{discord_id} - {register_ip} - {name} - {phone}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DbLookup(bot))
