import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)

WHITELIST_DB_PATH = str(Path(os.getenv("WHITELIST_DB_PATH") or (BASE_DIR / "data" / "whitelist.db")))
TOP_MONEY_FILE = Path(os.getenv(
    "MC_TOP_MONEY_FILE",
    "",
))
TOP_MONEY_ROLE_ID = int(os.getenv("TOP_MONEY_ROLE_ID", "1529886419672240168"))
GUILD_ID = int(os.getenv("HOME_GUILD_ID", os.getenv("GUILD_ID", "0")))
CHECK_INTERVAL_SECONDS = 90


@contextmanager
def _connect(path):
    con = sqlite3.connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


def _read_top_money_uuids() -> list[str]:
    if not TOP_MONEY_FILE.exists():
        return []
    lines = TOP_MONEY_FILE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()][:10]


def _discord_ids_for_uuids(uuids: list[str]) -> set[int]:
    if not uuids:
        return set()
    placeholders = ",".join("?" for _ in uuids)
    with _connect(WHITELIST_DB_PATH) as con:
        rows = con.execute(
            f"SELECT discord_id FROM whitelist_links WHERE mc_uuid IN ({placeholders})",
            uuids,
        ).fetchall()
    return {row[0] for row in rows}


class MCMoneyRole(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sync_loop.start()

    def cog_unload(self) -> None:
        self.sync_loop.cancel()

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def sync_loop(self) -> None:
        if not GUILD_ID or not TOP_MONEY_ROLE_ID:
            return
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        role = guild.get_role(TOP_MONEY_ROLE_ID)
        if role is None:
            logger.warning("MCMoneyRole: role id %s not found in guild %s", TOP_MONEY_ROLE_ID, GUILD_ID)
            return

        try:
            top_uuids = _read_top_money_uuids()
            should_have = _discord_ids_for_uuids(top_uuids)
        except Exception:
            logger.exception("MCMoneyRole: failed to read top money data")
            return

        for member in list(role.members):
            if member.id not in should_have:
                try:
                    await member.remove_roles(role, reason="No longer in Minecraft top 10 money")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("MCMoneyRole: failed to remove role from %s", member.id)

        for discord_id in should_have:
            member = guild.get_member(discord_id)
            if member is None:
                try:
                    member = await guild.fetch_member(discord_id)
                except (discord.NotFound, discord.HTTPException):
                    continue
            if role not in member.roles:
                try:
                    await member.add_roles(role, reason="In Minecraft top 10 money")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("MCMoneyRole: failed to add role to %s", member.id)

    @sync_loop.before_loop
    async def before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(MCMoneyRole(bot))
