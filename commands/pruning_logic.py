import discord
from discord import app_commands, Interaction, File
import asyncio
import csv
import json
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json


SCHEDULE_CONFIG_FILE = Path("prune_schedule.json")
LAST_PRUNE_FILE = Path("last_prune.txt")

def get_last_prune_time():
    try:
        with LAST_PRUNE_FILE.open() as f:
            ts = f.read().strip()
            return datetime.fromisoformat(ts)
    except Exception as e:
        print(f"[get_last_prune_time] Failed to read timestamp: {e}")
        return None

def set_last_prune_time(dt: datetime):
    with LAST_PRUNE_FILE.open("w") as f:
        f.write(dt.isoformat())

def load_prune_schedule():
    if SCHEDULE_CONFIG_FILE.exists():
        with SCHEDULE_CONFIG_FILE.open() as f:
            return json.load(f)
    return {
        "interval_hours": 72,
        "channel_id": None
    }

def load_prune_schedule():
    if SCHEDULE_CONFIG_FILE.exists():
        with SCHEDULE_CONFIG_FILE.open() as f:
            return json.load(f)
    return {
        "interval_hours": 72,
        "channel_id": None
    }

def save_prune_schedule(config: dict):
    with SCHEDULE_CONFIG_FILE.open("w") as f:
        json.dump(config, f, indent=4)

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

    last_update = datetime.now()
    for i, message in enumerate(messages):
        now = datetime.now()
        if (now - last_update).total_seconds() >= 3 or i == total - 1:
            percent = int(((i + 1) / total) * 100)
            await progress_msg.edit(content=f"Progress: {make_bar(percent)}")
            last_update = now

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
        if isinstance(interaction, DummyInteraction):
            return len(deleted), csv_file
        elif log_channel:
            await log_channel.send(f"{len(deleted)} messages deleted from {channel.mention} by {interaction.user.mention}", file=csv_file)


    await interaction.followup.send(f"Deleted {len(deleted)} messages from {channel.mention}.", ephemeral=True)

    return len(deleted), None  # For dummy use

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

@app_commands.command(name="set_prune_config", description="Set channel and interval for scheduled pruning.")
@app_commands.describe(
    channel="Channel to prune",
    amount="Amount of time between prunes",
    unit="Unit of time (minutes, hours, days, weeks)"
)
async def set_prune_config(interaction: Interaction, channel: discord.TextChannel, amount: int, unit: str):
    if not any(role.name == "Staff" for role in interaction.user.roles):
        await interaction.response.send_message("You don't have permission to change the prune config.", ephemeral=True)
        return

    if unit.lower() not in ["minutes", "hours", "days", "weeks"]:
        await interaction.response.send_message("Invalid unit. Choose minutes, hours, days, or weeks.", ephemeral=True)
        return

    config = {
        "channel_id": channel.id,
        "interval": amount,
        "unit": unit.lower()
    }
    save_prune_schedule(config)

    await interaction.response.send_message(
        f"Auto-prune config updated:\n"
        f"**Channel:** {channel.mention}\n"
        f"**Interval:** every {amount} {unit}",
        ephemeral=True
    )

@app_commands.command(name="next_prune", description="Check when the next scheduled prune is.")
async def next_prune(interaction: discord.Interaction):
    config = load_prune_schedule()
    interval = config.get("interval", 72)
    unit = config.get("unit", "hours")

    time_kwargs = {unit: interval} if unit != "years" else {"days": interval * 365}

    last_prune = get_last_prune_time()
    if last_prune is None:
        await interaction.response.send_message("No prune has occurred yet.", ephemeral=True)
        return

    try:
        next_run = last_prune + timedelta(**time_kwargs)
    except Exception as e:
        await interaction.response.send_message(f"Error calculating next run: {e}", ephemeral=True)
        return

    unix_ts = int(next_run.timestamp())
    formatted = next_run.strftime("%A, %B %d, %Y at %I:%M %p")

    await interaction.response.send_message(
        f"Next prune scheduled for <t:{unix_ts}:F> ({formatted}).",
        ephemeral=True
    )



def setup(tree: app_commands.CommandTree):
    tree.add_command(prune_cmd)
    tree.add_command(set_prune_config)
    tree.add_command(next_prune)
