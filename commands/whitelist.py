import logging
import os
import re
import sqlite3
import uuid as uuidlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord
from discord import Interaction, app_commands
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)

WHITELIST_DB_PATH = str(Path(os.getenv("WHITELIST_DB_PATH") or (BASE_DIR / "data" / "whitelist.db")))

MC_WHITELIST_HOST = os.getenv("MC_WHITELIST_HOST", "127.0.0.1")
MC_WHITELIST_PORT = int(os.getenv("MC_WHITELIST_PORT", "28017"))
MC_WHITELIST_TOKEN = os.getenv("MC_WHITELIST_TOKEN", "")
LOG_CHANNEL_ID = int(os.getenv("BLACKBIRDLOGS_ID", "0"))

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


@contextmanager
def _connect(path):
    con = sqlite3.connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


def init_whitelist_db() -> None:
    Path(WHITELIST_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect(WHITELIST_DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS whitelist_links(
                discord_id INTEGER PRIMARY KEY,
                mc_uuid TEXT NOT NULL UNIQUE,
                mc_username TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        con.commit()


def _existing_link_for_discord(discord_id: int):
    with _connect(WHITELIST_DB_PATH) as con:
        return con.execute(
            "SELECT mc_username FROM whitelist_links WHERE discord_id = ?",
            (discord_id,),
        ).fetchone()


def _existing_link_for_uuid(mc_uuid: str):
    with _connect(WHITELIST_DB_PATH) as con:
        return con.execute(
            "SELECT discord_id FROM whitelist_links WHERE mc_uuid = ?",
            (mc_uuid,),
        ).fetchone()


def _store_link(discord_id: int, mc_uuid: str, mc_username: str) -> None:
    with _connect(WHITELIST_DB_PATH) as con:
        con.execute(
            "INSERT INTO whitelist_links(discord_id, mc_uuid, mc_username, created_at) VALUES (?, ?, ?, ?)",
            (discord_id, mc_uuid, mc_username, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()


class Whitelist(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_whitelist_db()

    async def _resolve_mojang_uuid(self, session: aiohttp.ClientSession, username: str):
        url = f"https://api.mojang.com/users/profiles/minecraft/{username}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            data = await resp.json()
            raw_id = data["id"]
            dashed = f"{raw_id[0:8]}-{raw_id[8:12]}-{raw_id[12:16]}-{raw_id[16:20]}-{raw_id[20:32]}"
            return dashed, data["name"]

    async def _resolve_bedrock_uuid(self, session: aiohttp.ClientSession, gamertag: str):
        clean = gamertag.strip()
        if clean.startswith("."):
            clean = clean[1:]
        if not clean:
            return None
        url = f"https://api.geysermc.org/v2/xbox/xuid/{clean}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            xuid = data.get("xuid")
            if xuid is None:
                return None
            mc_uuid = str(uuidlib.UUID(int=xuid))
            floodgate_name = ("." + clean.replace(" ", "_"))[:16]
            return mc_uuid, floodgate_name

    @app_commands.command(name="whitelist", description="Whitelist your Minecraft account on the server")
    @app_commands.describe(
        username="Your exact Minecraft username (Java) or Xbox gamertag (Bedrock)",
        platform="Are you on Java Edition or Bedrock Edition (mobile/console/Win10)?",
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="Java Edition", value="java"),
        app_commands.Choice(name="Bedrock Edition", value="bedrock"),
    ])
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
    async def whitelist(self, interaction: Interaction, username: str, platform: str = "java"):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not MC_WHITELIST_TOKEN:
            await interaction.followup.send(
                "Somethings wrong with the token, contact Ren.", ephemeral=True
            )
            logger.error("MC_WHITELIST_TOKEN is not set; refusing /whitelist request.")
            return

        if platform == "java" and not USERNAME_RE.match(username):
            await interaction.followup.send(
                "Uhh that doesnt look like a valid Minecraft username (3-16 letters, numbers, or underscores).",
                ephemeral=True,
            )
            return
        if platform == "bedrock" and not username.strip():
            await interaction.followup.send(
                "That doesn't look like a valid Xbox gamertag.",
                ephemeral=True,
            )
            return

        existing = _existing_link_for_discord(interaction.user.id)
        if existing:
            await interaction.followup.send(
                f"You've already whitelisted `{existing[0]}`. Contact staff if you need to change this.",
                ephemeral=True,
            )
            return

        try:
            async with aiohttp.ClientSession() as session:
                if platform == "bedrock":
                    resolved = await self._resolve_bedrock_uuid(session, username)
                    not_found_msg = (
                        f"No Xbox account found with gamertag `{username}`. Double check the spelling."
                    )
                else:
                    resolved = await self._resolve_mojang_uuid(session, username)
                    not_found_msg = (
                        f"No Minecraft account found with username `{username}`. Double check the spelling."
                    )
                if resolved is None:
                    await interaction.followup.send(not_found_msg, ephemeral=True)
                    return
                mc_uuid, canonical_username = resolved

                linked_to = _existing_link_for_uuid(mc_uuid)
                if linked_to:
                    await interaction.followup.send(
                        "That Minecraft account is already linked to another Discord member. "
                        "Contact staff if this is a mistake.",
                        ephemeral=True,
                    )
                    return

                bridge_url = f"http://{MC_WHITELIST_HOST}:{MC_WHITELIST_PORT}/whitelist"
                headers = {"Authorization": f"Bearer {MC_WHITELIST_TOKEN}"}
                payload = {"uuid": mc_uuid, "username": canonical_username}
                async with session.post(
                    bridge_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error("Whitelist bridge returned %s: %s", resp.status, body[:500])
                        await interaction.followup.send(
                            "Couldn't reach the Minecraft server's whitelist bridge right now. "
                            "Please try again later or contact staff.",
                            ephemeral=True,
                        )
                        return
        except aiohttp.ClientError as e:
            logger.exception("Whitelist request failed for %s (%s): %s", username, interaction.user.id, e)
            await interaction.followup.send(
                "Something went wrong reaching the whitelist service. Please try again later.",
                ephemeral=True,
            )
            return

        _store_link(interaction.user.id, mc_uuid, canonical_username)

        await interaction.followup.send(
            f"`{canonical_username}` has been added to the whitelist! You can now join the server.",
            ephemeral=True,
        )

        log_channel = self.bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
        if log_channel:
            try:
                await log_channel.send(
                    f"\U0001f3ae {interaction.user.mention} whitelisted Minecraft account "
                    f"`{canonical_username}` (`{mc_uuid}`)"
                )
            except Exception:
                pass

    @whitelist.error
    async def whitelist_error(self, interaction: Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"Slow down a bit - try again in {error.retry_after:.0f}s.", ephemeral=True
            )
            return
        logger.exception("Unhandled error in /whitelist: %s", error)
        if interaction.response.is_done():
            await interaction.followup.send("Something went wrong. Please try again later.", ephemeral=True)
        else:
            await interaction.response.send_message("Something went wrong. Please try again later.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Whitelist(bot))
