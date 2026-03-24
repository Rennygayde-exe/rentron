import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import Interaction, app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parents[1]
XP_DB_PATH = str(Path(os.getenv("XP_DB_PATH") or (BASE_DIR / "xp.db")))
XP_NOTIFICATION_CHANNEL_ID = int(os.getenv("XP_NOTIFICATION_CHANNEL_ID", "0"))
XP_PER_MESSAGE_MIN = int(os.getenv("XP_PER_MESSAGE_MIN", "5"))
XP_PER_MESSAGE_MAX = int(os.getenv("XP_PER_MESSAGE_MAX", "15"))
XP_LEVEL_STEP = int(os.getenv("XP_LEVEL_STEP", "500"))
XP_EXCLUDED_ROLE_NAME = os.getenv("XP_EXCLUDED_ROLE_NAME", "Staff").strip()
XP_EARNING_COOLDOWN_SECONDS = int(os.getenv("XP_EARNING_COOLDOWN_SECONDS", "60"))


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
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS xp_optouts(
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        con.commit()


def is_opted_out(guild_id: int, user_id: int) -> bool:
    with sqlite3.connect(XP_DB_PATH) as con:
        row = con.execute(
            "SELECT 1 FROM xp_optouts WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
    return row is not None


def set_opt_out(guild_id: int, user_id: int) -> None:
    with sqlite3.connect(XP_DB_PATH) as con:
        con.execute(
            "INSERT OR IGNORE INTO xp_optouts(guild_id, user_id) VALUES(?,?)",
            (guild_id, user_id),
        )
        con.commit()


def clear_opt_out(guild_id: int, user_id: int) -> None:
    with sqlite3.connect(XP_DB_PATH) as con:
        con.execute(
            "DELETE FROM xp_optouts WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        con.commit()


def calculate_level(xp: int) -> int:
    if xp < 0:
        return 1
    return (xp // XP_LEVEL_STEP) + 1


def _parse_timestamp(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        dt = datetime.fromisoformat(raw_value)
    except ValueError:
        try:
            dt = datetime.strptime(raw_value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def add_xp(guild_id: int, user_id: int, amount: int) -> tuple[int, int, bool]:
    if guild_id <= 0 or user_id <= 0 or amount <= 0:
        return 0, 1, False
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    with sqlite3.connect(XP_DB_PATH) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT xp, level, updated_at FROM xp_progress WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        row = cur.fetchone()
        current_xp = row[0] if row else 0
        previous_level = calculate_level(current_xp)
        last_updated = _parse_timestamp(row[2] if row and len(row) > 2 else None)
        if last_updated and XP_EARNING_COOLDOWN_SECONDS > 0:
            elapsed = now - last_updated
            if elapsed < timedelta(seconds=XP_EARNING_COOLDOWN_SECONDS):
                return current_xp, previous_level, False

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
            (guild_id, user_id, new_xp, new_level, now_iso),
        )
        con.commit()
    return new_xp, new_level, new_level > previous_level


def fetch_leaderboard(guild_id: int, limit: int = 20) -> list[tuple[int, int, int]]:
    with sqlite3.connect(XP_DB_PATH) as con:
        rows = con.execute(
            """
            SELECT user_id, xp
            FROM xp_progress
            WHERE guild_id=?
            ORDER BY xp DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
    parsed_rows = []
    for uid, xp in rows:
        xp_int = int(xp)
        parsed_rows.append((int(uid), xp_int, calculate_level(xp_int)))
    return parsed_rows


class XPCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_xp_db()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            not message.guild
            or message.author.bot
            or not message.content.strip()
        ):
            return
        xp_min = min(XP_PER_MESSAGE_MIN, XP_PER_MESSAGE_MAX)
        xp_max = max(XP_PER_MESSAGE_MIN, XP_PER_MESSAGE_MAX)
        if xp_max <= 0:
            return
        if xp_min <= 0:
            xp_min = 1
        if XP_EXCLUDED_ROLE_NAME:
            author_roles = getattr(message.author, "roles", ()) or ()
            if any((getattr(role, "name", None) or "").lower() == XP_EXCLUDED_ROLE_NAME.lower() for role in author_roles):
                return

        if is_opted_out(message.guild.id, message.author.id):
            return

        xp_amount = random.randint(xp_min, xp_max)
        xp, level, leveled = add_xp(
            guild_id=message.guild.id,
            user_id=message.author.id,
            amount=xp_amount,
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
        name="xp_optout", description="Opt out of the XP system. You will no longer earn XP or appear on the leaderboard."
    )
    async def xp_optout(self, interaction: Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if is_opted_out(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message(
                "You are already opted out of the XP system.", ephemeral=True
            )
            return
        set_opt_out(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(
            "You have opted out of the XP system. You will no longer earn XP. "
            "Use `/xp_optin` at any time to re-enable it.",
            ephemeral=True,
        )

    @app_commands.command(
        name="xp_optin", description="Re-enable XP earning after opting out."
    )
    async def xp_optin(self, interaction: Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not is_opted_out(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message(
                "You are already opted in to the XP system.", ephemeral=True
            )
            return
        clear_opt_out(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(
            "You have opted back in to the XP system. You will start earning XP again.",
            ephemeral=True,
        )

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
