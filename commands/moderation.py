import discord
from discord.ext import commands
from discord import app_commands, Interaction, Attachment, File
import aiohttp
import json
import io
import zipfile
import re
import pandas as pd
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import psutil
import platform
from datetime import datetime, timedelta, timezone
import sys
import subprocess
import csv


start_time = time.time()

async def prune_attachments(
    interaction: discord.Interaction,
    channel: discord.abc.Messageable,
    amount: int,
    unit: str,
    type: str
):
    if not any(role.name == "Staff" for role in interaction.user.roles):
        await interaction.followup.send("You don't have permission to run this command.", ephemeral=True)
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    unit = unit.lower()
    type = type.lower()

    if unit not in ["seconds", "minutes", "hours", "days", "weeks", "years"]:
        await interaction.followup.send("Invalid time unit.", ephemeral=True)
        return
    if type not in ["all", "images"]:
        await interaction.followup.send("Invalid type. Use 'all' or 'images'.", ephemeral=True)
        return

    time_kwargs = {unit: amount} if unit != "years" else {"days": amount * 365}
    cutoff = datetime.now(timezone.utc) - timedelta(**time_kwargs)
    if interaction.response.is_done():
        await interaction.followup.send(
            f"Scanning {channel.mention} for {type} attachments older than {amount} {unit}...",
            ephemeral=True
        )

        deleted = []
        messages = [msg async for msg in channel.history(limit=2000)]
        total = len(messages)
        progress_msg = await interaction.followup.send("Progress: [░░░░░░░░░░] 0%", ephemeral=True)

        def make_bar(percent):
            bars = int(percent / 10)
            return f"[{'█' * bars}{'░' * (10 - bars)}] {percent}%"

        for i, message in enumerate(messages):
            percent = int(((i + 1) / total) * 100)
            if (i + 1) % 10 == 0 or i == total - 1:
                await progress_msg.edit(content=f"Progress: {make_bar(percent)}")

            if message.created_at < cutoff and message.attachments:
                if type == "images" and not any(att.filename.lower().endswith((
                    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")) for att in message.attachments):
                    continue
                try:
                    await message.delete()
                    deleted.append({
                        "Author": f"{message.author} ({message.author.id})",
                        "Date": message.created_at.isoformat(),
                        "Attachment": message.attachments[0].url,
                        "Message ID": message.id,
                        "Channel": str(channel)
                    })
                    await asyncio.sleep(0.75)
                except Exception as e:
                    print(f"Error deleting message: {e}")

        await progress_msg.edit(content=f"Deleted {len(deleted)} message(s).")

        if deleted:
            csv_buffer = io.StringIO()
            writer = csv.DictWriter(csv_buffer, fieldnames=["Author", "Date", "Attachment", "Message ID", "Channel"])
            writer.writeheader()
            writer.writerows(deleted)
            csv_buffer.seek(0)

            csv_file = discord.File(fp=io.BytesIO(csv_buffer.read().encode()), filename="pruned_attachments_log.csv")
            log_channel = discord.utils.get(interaction.guild.text_channels, name="backdoor-bot-stuff")
            if log_channel:
                await log_channel.send(f"{len(deleted)} messages deleted from {channel.mention} by {interaction.user.mention}", file=csv_file)

        await interaction.followup.send(f"Deleted {len(deleted)} messages from {channel.mention}.", ephemeral=True)

@app_commands.command(name="prune_attachments", description="Delete messages with attachments older than a set time.")
@app_commands.describe(
    channel="Target channel",
    amount="Time amount (number)",
    unit="Time unit (e.g. days, weeks)",
    type="Attachment type: all or images"
)
async def prune_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    amount: int,
    unit: str,
    type: str
):
    await prune_attachments(interaction, channel, amount, unit, type)


@app_commands.command(name="rams", description="Run a random prune in the selected channel.")
@app_commands.describe(channel="Target channel to randomly prune")
async def rams_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    if not any(role.name == "Staff" for role in interaction.user.roles):
        await interaction.followup.send("You don't have permission to run this command.", ephemeral=True)
        return

    days = random.randint(1, 14)
    attachment_type = random.choice(["all", "images"])
    await interaction.followup.send(
        f"Hold on Chewie, this might get a little hairy!\nPruning {channel.mention} for **{attachment_type}** attachment types older than **{days} days**...",
        ephemeral=True
    )

    await prune_attachments(interaction, channel, days, "days", attachment_type)

    from discord import app_commands, Interaction, Attachment
import json
import aiohttp

from discord import app_commands, Interaction, Attachment
import aiohttp
import json

from discord import app_commands, Interaction, Attachment
import aiohttp
import json
import asyncio

@app_commands.command(name="massshadowgenerator", description="Malachor-V Mass Shadow Generator.")
@app_commands.describe(file="Attach a .json file with ban entries")
@app_commands.checks.has_permissions(ban_members=True)
async def massshadowgenerator(interaction: Interaction, file: Attachment):
    if not file.filename.endswith(".json"):
        await interaction.response.send_message("Please upload a valid `.json` file.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    message = await interaction.followup.send("Initializing mass shadow generator... as if we didn't learn from Revan's mistake.")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(file.url) as resp:
                if resp.status != 200:
                    await message.edit(content="Failed to download JSON.")
                    return
                content = await resp.text()

        try:
            data = json.loads(content)
            ban_list = data if isinstance(data, list) else data.get("bans", [])
        except json.JSONDecodeError as e:
            await message.edit(content=f"Invalid JSON format: {e}")
            return

        total = len(ban_list)
        banned = 0
        failed = 0
        update_every = max(1, total // 25)
        for index, entry in enumerate(ban_list, start=1):
            user_id = entry.get("id")
            reason = entry.get("reason", "No reason provided")

            try:
                user = await interaction.client.fetch_user(user_id)
                await interaction.guild.ban(user, reason=reason)
                banned += 1
            except Exception as e:
                failed += 1
                print(f"Failed to ban {user_id}: {e}")

            if index % update_every == 0 or index == total:
                bar = progress_bar(index, total)
                await message.edit(content=f"{bar}\nProgress: {index}/{total}")

        await message.edit(content=f"Mass Shadow Generator complete.\nBanned: {banned}\nFailed: {failed}")

    except Exception as e:
        await message.edit(content=f"Error: {e}")


def progress_bar(current: int, total: int, width: int = 30) -> str:
    filled = int(width * current / total)
    empty = width - filled
    return "[" + ("#" * filled) + ("-" * empty) + "]"

@app_commands.command(name="sync", description="Force slash command sync.")
@app_commands.checks.has_permissions(administrator=True)
async def sync_commands(interaction: Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.client.tree.sync(guild=interaction.guild)
    await interaction.followup.send("Synced commands.")


@app_commands.command(name="parse_zip", description="Parse a zip of .txt files into a banlist JSON and Excel.")
@app_commands.describe(file="Upload a zip file containing .txt files with user info.")
async def parse_zip(interaction: Interaction, file: Attachment):
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

        # Convert to JSON
        json_bytes = io.BytesIO(json.dumps(banlist, indent=4).encode())
        json_bytes.seek(0)

        # Convert to Excel
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
async def botstats(interaction: Interaction):
    now = time.time()
    uptime_seconds = int(now - start_time)
    uptime_str = str(timedelta(seconds=uptime_seconds))

    process = psutil.Process()
    mem = process.memory_info().rss / 1024 / 1024
    system = platform.system()
    python_ver = platform.python_version()

    from bot import log_buffer
    log_buffer.seek(0)
    log_lines = log_buffer.read().splitlines()[-25:]
    log_text = "\n".join(log_lines) or "No log entries available."

    log_file = discord.File(io.BytesIO(log_text.encode()), filename="console_log.txt")

    embed = discord.Embed(
        title="Rentron Bot Stats",
        color=discord.Color.blue()
    )
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Memory Usage", value=f"{mem:.2f} MB", inline=True)
    embed.add_field(name="System", value=system, inline=True)
    embed.add_field(name="Python", value=python_ver, inline=True)

    await interaction.response.send_message(embed=embed, file=log_file)

@app_commands.command(name="restart_bot", description="Restarts Rentron.")
async def restart_bot(interaction: Interaction):
    if not any(role.name in ("Admin", "S6 Professional", "Staff") for role in interaction.user.roles):
        await interaction.response.send_message("Fuck off kiddo.", ephemeral=True)
        return

    await interaction.response.send_message("NOOOOOOOOOOOOOOOOOOOOOOOO", ephemeral=True)
    await interaction.client.close()
    os.execv(sys.executable, ['python'] + sys.argv)

@app_commands.command(name="ssh", description="Run a shell command")
@app_commands.describe(command="The shell command to run")
async def ssh(interaction: Interaction, command: str):
    allowed_user_id = 669626735385640993

    if interaction.user.id != allowed_user_id:
        await interaction.response.send_message("WHO THE FUCK DO YOU THINK YOU ARE? IM TELLING MY MOM.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    try:
        result = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30
        )
        output = result.stdout.decode("utf-8")

        if not output.strip():
            output = "no output was logged."

        await interaction.followup.send(f"```\n{output[:1900]}\n```")

    except Exception as e:
        await interaction.followup.send(f"Error:\n```{str(e)[:1900]}```")

def setup(tree: app_commands.CommandTree):
    tree.add_command(prune_cmd)
    tree.add_command(rams_cmd)
    tree.add_command(massshadowgenerator)
    tree.add_command(sync_commands)
    tree.add_command(parse_zip)
    tree.add_command(botstats)
    tree.add_command(restart_bot)
    tree.add_command(ssh)