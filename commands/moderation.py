import discord
from discord import app_commands
from datetime import datetime, timedelta, timezone
import asyncio
import io
import csv
import random

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
        f"Hold on Chewie, this might get a little hairy!\nScrubbing {channel.mention} for **{attachment_type}** older than **{days} days**...",
        ephemeral=True
    )

    await prune_attachments(interaction, channel, days, "days", attachment_type)