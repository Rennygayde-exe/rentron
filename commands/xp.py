import os
import sqlite3
from datetime import datetime
from pathlib import Path

import discord
from discord import Interaction, app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parents[1]
XP_DB_PATH = str(Path(os.getenv("XP_DB_PATH") or (BASE_DIR / "xp.db")))
XP_NOTIFICATION_CHANNEL_ID = int(os.getenv("XP_NOTIFICATION_CHANNEL_ID", "0"))
XP_PER_MESSAGE = int(os.getenv("XP_PER_MESSAGE", "5"))
XP_LEVEL_STEP = int(os.getenv("XP_LEVEL_STEP", "100"))
XP_EXCLUDED_ROLE_NAME = os.getenv("XP_EXCLUDED_ROLE_NAME", "Staff").strip()


def init_xp_db() -> None:
    with sqlite3.connect(XP_DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS xp_progress(
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        con.commit()


def calculate_level(xp: int) -> int:
    if xp < 0:
        return 1
    return (xp // XP_LEVEL_STEP) + 1


def add_xp(guild_id: int, user_id: int, amount: int) -> tuple[int, int, bool]:
    if guild_id <= 0 or user_id <= 0 or amount <= 0:
        return 0, 1, False
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(XP_DB_PATH) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT xp, level FROM xp_progress WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        row = cur.fetchone()
        current_xp = row[0] if row else 0
        previous_level = row[1] if row else 1
        new_xp = current_xp + amount
        new_level = calculate_level(new_xp)
        cur.execute(
            """
            INSERT INTO xp_progress(guild_id,user_id,xp,level,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(guild_id,user_id) DO UPDATE SET
                xp=excluded.xp,
                level=excluded.level,
                updated_at=excluded.updated_at
            """,
            (guild_id, user_id, new_xp, new_level, now),
        )
        con.commit()
    return new_xp, new_level, new_level > previous_level


def fetch_leaderboard(guild_id: int, limit: int = 20) -> list[tuple[int, int, int]]:
    with sqlite3.connect(XP_DB_PATH) as con:
        rows = con.execute(
            """
            SELECT user_id, xp, level
            FROM xp_progress
            WHERE guild_id=?
            ORDER BY xp DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
    return [(int(uid), int(xp), int(level)) for uid, xp, level in rows]


class XPCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_xp_db()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            not message.guild
            or message.author.bot
            or XP_PER_MESSAGE <= 0
            or not message.content.strip()
        ):
            return
        if XP_EXCLUDED_ROLE_NAME:
            author_roles = getattr(message.author, "roles", ()) or ()
            if any(getattr(role, "name", None) == XP_EXCLUDED_ROLE_NAME for role in author_roles):
                return

        xp, level, leveled = add_xp(
            guild_id=message.guild.id,
            user_id=message.author.id,
            amount=XP_PER_MESSAGE,
        )
        if leveled and XP_NOTIFICATION_CHANNEL_ID:
            channel = message.guild.get_channel(XP_NOTIFICATION_CHANNEL_ID)
            if channel is None:
                try:
                    fetched = await self.bot.fetch_channel(XP_NOTIFICATION_CHANNEL_ID)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    fetched = None
                channel = fetched if isinstance(fetched, discord.TextChannel) else None
            if channel and isinstance(channel, discord.TextChannel):
                if channel.guild.id == message.guild.id:
                    try:
                        await channel.send(
                            f"{message.author.mention} reached level {level}! (XP: {xp})"
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass

    @app_commands.command(
        name="leaderboard", description="Show the top members by XP in this server."
    )
    async def leaderboard(self, interaction: Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        rows = fetch_leaderboard(interaction.guild.id)
        if not rows:
            await interaction.response.send_message(
                "No XP data yet. Start chatting to earn some!", ephemeral=True
            )
            return
        lines = []
        for idx, (user_id, xp, level) in enumerate(rows, start=1):
            member = interaction.guild.get_member(user_id)
            display = member.display_name if member else f"User {user_id}"
            lines.append(f"{idx}. {display} — {xp} XP (Level {level})")
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(
        name="clearleaderboard", description="Reset the XP leaderboard for this server."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def clearleaderboard(self, interaction: Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        with sqlite3.connect(XP_DB_PATH) as con:
            con.execute(
                "DELETE FROM xp_progress WHERE guild_id=?", (interaction.guild.id,)
            )
            con.commit()
        await interaction.response.send_message(
            "XP leaderboard cleared for this server.", ephemeral=True
        )

    @clearleaderboard.error
    async def clearleaderboard_error(
        self, interaction: Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need administrator permissions to do that.", ephemeral=True
            )
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(XPCog(bot))
