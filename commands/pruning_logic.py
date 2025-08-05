
import discord
from discord import app_commands, Interaction, File
import asyncio
import csv
import json
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEDULE_CONFIG_FILE = Path("prune_schedule.json")
LAST_PRUNE_FILE      = Path("last_prune.txt")

UNIT_SECONDS = {
    "minutes": 60,
    "hours":   3600,
    "days":    86400,
    "weeks":   604800,
}

def get_last_prune_time() -> datetime | None:
    if LAST_PRUNE_FILE.exists():
        try:
            ts = int(LAST_PRUNE_FILE.read_text().strip())
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except:
            pass
    return None

def set_last_prune_time(dt: datetime):
    LAST_PRUNE_FILE.write_text(str(int(dt.timestamp())))

def load_prune_schedule() -> dict:
    if SCHEDULE_CONFIG_FILE.exists():
        return json.loads(SCHEDULE_CONFIG_FILE.read_text())
    return {"interval": 72, "unit": "hours", "channel_id": None}

def save_prune_schedule(cfg: dict):
    SCHEDULE_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

async def prune_attachments(
    interaction: discord.Interaction,
    channel: discord.abc.Messageable,
    amount: int,
    unit: str,
    attach_type: str
) -> list[dict]:
    """
    Deletes attachments older than `amount` `unit` from `channel`,
    and returns a list of deleted-item dicts so the caller can log them.
    """
    if not any(r.name == "Staff" for r in interaction.user.roles):
        await interaction.followup.send("You don't have permission to run this.", ephemeral=True)
        return []

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    unit = unit.lower()
    attach_type = attach_type.lower()
    if unit not in UNIT_SECONDS or attach_type not in ("all", "images"):
        await interaction.followup.send("Invalid unit or type.", ephemeral=True)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(**{unit: amount})
    deleted = []

    await interaction.followup.send(f"Scanning {channel.mention} for `{attach_type}` attachments older than {amount} {unit}…", ephemeral=True)
    progress = await interaction.followup.send("Progress: 0%", ephemeral=True)

    history = [m async for m in channel.history(limit=2000)]
    total   = len(history)

    for i, msg in enumerate(history, start=1):
        if i == total or (i % 20 == 0):
            perc = int(i/total*100)
            await progress.edit(content=f"Progress: {perc}%")
        if msg.created_at < cutoff and msg.attachments:
            if attach_type == "images" and not any(a.filename.lower().endswith((
                ".png",".jpg",".jpeg",".gif",".bmp",".webp"
            )) for a in msg.attachments):
                continue
            try:
                await msg.delete()
                deleted.append({
                    "id":        msg.id,
                    "author":    f"{msg.author} ({msg.author.id})",
                    "created":   msg.created_at.isoformat(),
                    "attachment": msg.attachments[0].url,
                    "channel":   channel.name
                })
                await asyncio.sleep(0.7)
            except:
                pass

    await progress.edit(content=f"Done—deleted {len(deleted)} items.")

    return deleted


@app_commands.command(name="set_prune_config", description="Configure auto-prune.")
@app_commands.describe(
    channel="Where to prune",
    amount="Interval amount",
    unit="Unit: minutes/hours/days/weeks"
)
async def set_prune_config(interaction: Interaction, channel: discord.TextChannel, amount: int, unit: str):
    if not any(r.name == "Staff" for r in interaction.user.roles):
        await interaction.response.send_message("No perms.", ephemeral=True)
        return
    unit = unit.lower()
    if unit not in UNIT_SECONDS:
        await interaction.response.send_message("Invalid unit.", ephemeral=True)
        return

    cfg = {"channel_id": channel.id, "interval": amount, "unit": unit}
    save_prune_schedule(cfg)
    await interaction.response.send_message(
        f"Auto-prune set: every {amount} {unit} in {channel.mention}",
        ephemeral=True
    )


@app_commands.command(name="next_prune", description="Show next scheduled prune.")
async def next_prune(interaction: Interaction):
    cfg = load_prune_schedule()
    chan = cfg.get("channel_id")
    interval, unit = cfg["interval"], cfg["unit"]
    last = get_last_prune_time()
    now  = datetime.now(timezone.utc)
    thresh = timedelta(seconds=interval * UNIT_SECONDS[unit])

    if not chan or not last:
        nxt = now + thresh
    else:
        nxt = last + thresh

    await interaction.response.send_message(
        f"Next prune: <t:{int(nxt.timestamp())}:F> (`{unit}` interval)",
        ephemeral=True
    )


def setup(tree: app_commands.CommandTree):
    tree.add_command(set_prune_config)
    tree.add_command(next_prune)