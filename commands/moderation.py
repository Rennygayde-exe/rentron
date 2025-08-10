import os
import io
import re
import sys
import json
import time
import zipfile
import platform
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta

import aiohttp
import pandas as pd
import psutil
import discord
from discord import app_commands, Interaction, Attachment, File
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TRACE_LOG_CHANNEL_ID = int(os.getenv("BLACKBIRDLOGS_ID", "0"))

def progress_bar(current: int, total: int, width: int = 30) -> str:
    filled = int(width * current / total) if total else 0
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.start_time = time.time()


    def normalize_ban_entries(payload):
        """
        Accepts many shapes and returns a list of dicts: {"id": str|None, "username": str|None, "reason": str}
        Accepted payloads:
        - ["123", "456"]
        - [{"id":"123","reason":"..."}]
        - {"bans": [...]}
        - {"123":"reason", "456":"reason"}
        """
        def is_id(x):
            s = str(x).strip()
            return s.isdigit() and 17 <= len(s) <= 20

        entries = []

        # unwrapper
        if isinstance(payload, dict) and "bans" in payload:
            payload = payload["bans"]

        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, (str, int)) and is_id(item):
                    entries.append({"id": str(item).strip(), "username": None, "reason": "No reason provided"})
                elif isinstance(item, dict):
                    uid = None
                    for k in ("id", "user_id", "uid", "snowflake"):
                        if k in item and is_id(item[k]):
                            uid = str(item[k]).strip()
                            break
                    uname = None
                    for k in ("username", "name", "display_name", "global_name"):
                        if k in item and isinstance(item[k], str) and item[k].strip():
                            uname = item[k].strip()
                            break
                    reason = item.get("reason") or "No reason provided"
                    entries.append({"id": uid, "username": uname, "reason": reason})
        elif isinstance(payload, dict):
            for k, v in payload.items():
                if is_id(k):
                    entries.append({"id": k, "username": None, "reason": str(v) if v else "No reason provided"})

        seen = set()
        out = []
        for e in entries:
            key = ("id", e["id"]) if e["id"] else ("user", e["username"])
            if key not in seen:
                seen.add(key)
                out.append(e)
        return out
    @app_commands.command(name="massshadowgenerator", description="Malachor-V Mass Shadow Generator.")
    @app_commands.describe(file="Attach a .json file with ban entries")
    @app_commands.checks.has_permissions(ban_members=True)
    async def massshadowgenerator(self, interaction: Interaction, file: Attachment):
        if not file.filename.endswith(".json"):
            await interaction.response.send_message("Please upload a valid `.json` file.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        message = await interaction.followup.send("Initializing mass shadow generator...")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(file.url) as resp:
                    if resp.status != 200:
                        await message.edit(content="Failed to download JSON.")
                        return
                    content = await resp.text()

            try:
                data = json.loads(content)
                raw = data["bans"] if isinstance(data, dict) and "bans" in data else data
                ban_entries = []
                for item in raw:
                    if isinstance(item, (str, int)) and str(item).isdigit():
                        ban_entries.append({"id": str(item), "username": None, "reason": "No reason provided"})
                    elif isinstance(item, dict):
                        ban_entries.append({
                            "id": str(item.get("id") or item.get("user_id") or item.get("uid") or item.get("snowflake") or "").strip() or None,
                            "username": (item.get("username") or item.get("name") or item.get("display_name") or item.get("global_name")),
                            "reason": item.get("reason") or "No reason provided"
                        })
            except json.JSONDecodeError as e:
                await message.edit(content=f"Invalid JSON format: {e}")
                return

            guild = interaction.guild

            try:
                async for _ in guild.fetch_members(limit=None):
                    pass
            except Exception:
                pass

            name_map = {}
            for m in guild.members:
                for k in (m.name, m.display_name, m.global_name or ""):
                    if k:
                        name_map.setdefault(k.lower(), m)

            total = len(ban_entries)
            banned = failed = skipped_dup = 0
            update_every = max(1, total // 25) if total else 1
            seen_ids: set[int] = set()

            for index, entry in enumerate(ban_entries, start=1):
                user_obj = None
                reason = (entry.get("reason") or "No reason provided")[:512]
                raw_id = entry.get("id")

                target_id: int | None = None
                if raw_id and str(raw_id).isdigit():
                    target_id = int(raw_id)

                if target_id:
                    if target_id in seen_ids:
                        skipped_dup += 1
                    else:
                        try:
                            await guild.ban(discord.Object(id=target_id), reason=reason)
                            seen_ids.add(target_id)
                            banned += 1
                        except Exception as e:
                            # fallback search if we also have a username
                            uname = (entry.get("username") or "").strip().lower()
                            if uname:
                                user_obj = name_map.get(uname)
                                if user_obj is None:
                                    candidates = [m for k, m in name_map.items() if uname in k]
                                    if len(candidates) == 1:
                                        user_obj = candidates[0]
                            if user_obj:
                                try:
                                    await guild.ban(user_obj, reason=reason)
                                    seen_ids.add(int(user_obj.id))
                                    banned += 1
                                except Exception as ee:
                                    failed += 1
                                    print(f"Failed to ban {target_id} via fallback: {ee}")
                            else:
                                failed += 1
                                print(f"Failed to ban {target_id}: {e}")
                else:
                    # No ID = try username-only fallback
                    uname = (entry.get("username") or "").strip().lower()
                    if not uname:
                        failed += 1
                    else:
                        user_obj = name_map.get(uname)
                        if user_obj is None:
                            candidates = [m for k, m in name_map.items() if uname in k]
                            if len(candidates) == 1:
                                user_obj = candidates[0]
                        if user_obj:
                            tid = int(user_obj.id)
                            if tid in seen_ids:
                                skipped_dup += 1
                            else:
                                try:
                                    await guild.ban(user_obj, reason=reason)
                                    seen_ids.add(tid)
                                    banned += 1
                                except Exception as e:
                                    failed += 1
                                    print(f"Failed to ban {tid}: {e}")
                        else:
                            failed += 1

                if index % update_every == 0 or index == total:
                    bar = progress_bar(index, total)
                    await message.edit(content=f"{bar}\nProgress: {index}/{total}")

            await message.edit(
                content=(
                    "Mass Shadow Generator complete.\n"
                    f"Banned: {banned}\nFailed: {failed}\nSkipped (duplicate): {skipped_dup}"
                )
            )
        except Exception as e:
            await message.edit(content=f"Error: {e}")
    @app_commands.command(name="parse_zip", description="Parse a zip of .txt files into a banlist JSON and Excel.")
    @app_commands.describe(file="Upload a zip file containing .txt files with user info.")
    async def parse_zip(self, interaction: Interaction, file: Attachment):
        if not file.filename.endswith(".zip"):
            await interaction.response.send_message("Please upload a `.zip` file.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(file.url) as resp:
                    if resp.status != 200:
                        return await interaction.followup.send("Failed to download zip.")
                    zip_data = await resp.read()

            banlist = []
            with TemporaryDirectory() as tmpdir:
                with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                    z.extractall(tmpdir)

                for path in Path(tmpdir).rglob("*.txt"):
                    content = path.read_text(encoding="utf-8", errors="ignore")
                    id_match = re.search(r"Account ID:\s*(\d+)", content)
                    user_match = re.search(r"Username:\s*(.+)", content)
                    if id_match and user_match:
                        banlist.append({
                            "id": id_match.group(1),
                            "username": user_match.group(1),
                            "reason": "Scraped from uploaded dump"
                        })

            if not banlist:
                return await interaction.followup.send("No valid entries found.")

            json_bytes = io.BytesIO(json.dumps(banlist, indent=4).encode())
            json_bytes.seek(0)
            excel_buffer = io.BytesIO()
            pd.DataFrame(banlist).to_excel(excel_buffer, index=False)
            excel_buffer.seek(0)

            await interaction.followup.send("Banlist generated successfully:", files=[
                File(json_bytes, filename="banlist.json"),
                File(excel_buffer, filename="banlist.xlsx")
            ])
        except Exception as e:
            await interaction.followup.send(f"Error: `{e}`", ephemeral=True)

    @app_commands.command(name="botstats", description="View bot uptime and stats.")
    async def botstats(self, interaction: Interaction):
        now = time.time()
        uptime_seconds = int(now - self.start_time)
        uptime_str = str(timedelta(seconds=uptime_seconds))

        process = psutil.Process()
        mem = process.memory_info().rss / 1024 / 1024
        system = platform.system()
        python_ver = platform.python_version()

        embed = discord.Embed(title="Rentron Bot Stats", color=discord.Color.blue())
        embed.add_field(name="Uptime", value=uptime_str, inline=True)
        embed.add_field(name="Memory Usage", value=f"{mem:.2f} MB", inline=True)
        embed.add_field(name="System", value=system, inline=True)
        embed.add_field(name="Python", value=python_ver, inline=True)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="restart_bot", description="Restarts Rentron.")
    async def restart_bot(self, interaction: Interaction):
        if not any(role.name in ("Admin", "S6 Professional", "Staff") for role in interaction.user.roles):
            await interaction.response.send_message("Permission denied.", ephemeral=True)
            return
        await interaction.response.send_message("Restarting.", ephemeral=True)
        await interaction.client.close()
        os.execv(sys.executable, ['python'] + sys.argv)

    @app_commands.command(name="massmove", description="Move everyone from one voice channel to another.")
    @app_commands.describe(from_channel="Source voice channel", to_channel="Destination voice channel", delay_ms="Delay per move (0-1000)")
    @app_commands.checks.has_permissions(move_members=True)
    async def massmove(
        self,
        interaction: Interaction,
        from_channel: discord.VoiceChannel,
        to_channel: discord.VoiceChannel,
        delay_ms: int = 100
    ):
        if from_channel.id == to_channel.id:
            await interaction.response.send_message("Source and destination must be different.", ephemeral=True)
            return
        members = list(from_channel.members)
        if not members:
            await interaction.response.send_message("No members to move.", ephemeral=True)
            return
        delay_ms = max(0, min(1000, delay_ms))
        await interaction.response.defer(ephemeral=True, thinking=True)

        moved = 0
        failed = 0
        for m in members:
            try:
                await m.move_to(to_channel, reason=f"Mass move by {interaction.user}")
                moved += 1
            except Exception as e:
                failed += 1
                print(f"Failed to move {m} ({m.id}): {e}")
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)

        await interaction.followup.send(f"Moved {moved} member(s). Failed: {failed}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
