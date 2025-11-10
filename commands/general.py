from discord.ext import commands
from utils.responses import load_responses, RESPONSES, compile_triggers
from discord.ui import View, Select, Modal, TextInput
import discord, random, json
from io import BytesIO
from PIL import Image
import random
from discord import app_commands, Interaction, File, TextChannel, Member
from discord.ui import View, Select, Modal, TextInput
import aiohttp
import os
import requests
import openai
from datetime import datetime, timezone, timedelta
import io
import csv
from openai import AsyncOpenAI
from dotenv import load_dotenv
import subprocess
import asyncio, re, fnmatch
from typing import Literal
from pathlib import Path
load_dotenv()
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")

_FRAMES = 30
_FRAME_DURATION = 60  # ms per frame

OUT_OF_OFFICE_FILE = Path("data/out_of_office.json")
OUT_OF_OFFICE: dict[str, dict[str, str]] = {}


def load_out_of_office() -> dict[str, dict[str, str]]:
    """Load the cached out-of-office map from disk."""
    global OUT_OF_OFFICE
    if not OUT_OF_OFFICE_FILE.exists():
        OUT_OF_OFFICE = {}
        return OUT_OF_OFFICE
    try:
        with OUT_OF_OFFICE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                OUT_OF_OFFICE = {str(k): v for k, v in data.items()}
            else:
                OUT_OF_OFFICE = {}
    except (json.JSONDecodeError, OSError):
        OUT_OF_OFFICE = {}
    return OUT_OF_OFFICE


def save_out_of_office() -> None:
    """Persist the current out-of-office cache to disk."""
    OUT_OF_OFFICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUT_OF_OFFICE_FILE.open("w", encoding="utf-8") as f:
        json.dump(OUT_OF_OFFICE, f, indent=2)


def set_out_of_office(user_id: int, message: str) -> None:
    OUT_OF_OFFICE[str(user_id)] = {
        "message": message,
        "set_at": datetime.now(timezone.utc).isoformat(),
    }
    save_out_of_office()


def clear_out_of_office(user_id: int) -> bool:
    removed = OUT_OF_OFFICE.pop(str(user_id), None)
    if removed is not None:
        save_out_of_office()
        return True
    return False


def get_out_of_office_status(user_id: int) -> dict[str, str] | None:
    return OUT_OF_OFFICE.get(str(user_id))


load_out_of_office()


def _attach_match(name: str, needle: str, mode: str, cs: bool) -> bool:
    if not cs:
        name = name.lower(); needle = needle.lower()
    if mode == "exact": return name == needle
    if mode == "contains": return needle in name
    if mode == "glob": return fnmatch.fnmatch(name, needle)
    try:
        flags = 0 if cs else re.IGNORECASE
        return re.search(needle, name, flags) is not None
    except re.error:
        return False

def create_github_issue(title, body, labels=[]):
    import requests
    import os

    repo = os.getenv("GITHUB_REPO")
    token = os.getenv("GITHUB_TOKEN")

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json"
    }

    data = {
        "title": title,
        "body": body,
        "labels": labels
    }

    response = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        json=data,
        headers=headers
    )

    return response.status_code, response.json()
class LabelSelectView(View):
    def __init__(self):
        super().__init__(timeout=300)
        self.selected_labels = []

        self.label_select = Select(
            placeholder="Select labels for the issue",
            min_values=1,
            max_values=3,
            options=[
                discord.SelectOption(label="bug", description="Something isn't working"),
                discord.SelectOption(label="feature", description="Suggest a new idea"),
                discord.SelectOption(label="enhancement", description="Improve existing feature"),
                discord.SelectOption(label="documentation", description="Docs or info issues")
            ]
        )
        self.label_select.callback = self.select_callback
        self.add_item(self.label_select)

    async def select_callback(self, interaction: Interaction):
        self.selected_labels = self.label_select.values

        class IssueModal(Modal, title="Submit GitHub Issue"):
            title_input = TextInput(label="Title", max_length=80)
            description_input = TextInput(label="Description", style=discord.TextStyle.paragraph, required=False)

            async def on_submit(modal_self, modal_interaction: Interaction):
                status, data = create_github_issue(
                    modal_self.title_input.value,
                    modal_self.description_input.value or "No description provided.",
                    self.selected_labels
                )
                if status == 201:
                    await modal_interaction.response.send_message(
                        f"Issue created: {data['html_url']}", ephemeral=True
                    )
                else:
                    await modal_interaction.response.send_message(
                        f"Failed to create issue: `{data.get('message')}`", ephemeral=True
                    )

        await interaction.response.send_modal(IssueModal())

@app_commands.command(name="gitissue", description="Submit a GitHub issue with labels.")
async def gitissue(interaction: Interaction):
    await interaction.response.send_message(
        "Select labels for your GitHub issue:", view=LabelSelectView(), ephemeral=True
    )


@app_commands.command(
    name="outofoffice",
    description="Set or clear your out of office status."
)
@app_commands.describe(
    status="Set to true to enable and false to clear your out of office message.",
    message="Message to display when others tag you. Required when enabling."
)
async def out_of_office(interaction: Interaction, status: bool, message: str = ""):
    user_id = interaction.user.id

    if status:
        trimmed = message.strip()
        if not trimmed:
            await interaction.response.send_message(
                "You need to include a message when enabling out of office.",
                ephemeral=True,
            )
            return
        set_out_of_office(user_id, trimmed)
        await interaction.response.send_message(
            "You're marked out of office. I'll let folks know when they ping you.",
            ephemeral=True,
        )
        return

    was_set = clear_out_of_office(user_id)
    if was_set:
        await interaction.response.send_message(
            "Your out of office status is cleared.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "You were not marked out of office.",
            ephemeral=True,
        )

@commands.is_owner()
@commands.command(name="reloadresponses")
async def reload_responses(ctx):
    load_responses()
    compile_triggers()
    await ctx.send("Response triggers reloaded.")

@commands.is_owner()
@commands.command(name="listresponses")
async def list_responses(ctx):
    try:
        if not RESPONSES:
            await ctx.send("No responses are currently loaded.")
            return

        chunks = []
        chunk = ""
        for entry in RESPONSES:
            line = f"**ID**: `{entry.get('id', 'n/a')}`\n"
            line += f"**Triggers**: `{', '.join(entry.get('triggers', []))}`\n"
            line += f"**Response**: {entry.get('response')}\n"
            line += f"**Mention Required**: `{entry.get('mention_required', False)}`\n\n"
            if len(chunk + line) > 1900:
                chunks.append(chunk)
                chunk = ""
            chunk += line
        chunks.append(chunk)

        for part in chunks:
            await ctx.send(part)
    except Exception as e:
        await ctx.send(f"Failed to list responses: {e}")

@app_commands.command(
    name="fortune",
    description="Get a random fortune from the server"
)
async def fortune_cmd(interaction: Interaction):

    await interaction.response.defer(thinking=True)
    try:
        proc = subprocess.run(
            ["fortune"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        output = proc.stdout.strip() or proc.stderr.strip() or "No fortune found."
    except Exception as e:
        output = f"Error running fortune: {e}"

    await interaction.followup.send(f"```\n{output}\n```")
import subprocess, shutil
from discord import app_commands, Interaction

@app_commands.command(name="moo", description="Have the cow say your text")
@app_commands.describe(text="Text to have the cow say")
async def cowsay_cmd(interaction: Interaction, text: str):
    await interaction.response.defer()

    cowsay_path = shutil.which("cowsay") or "/usr/games/cowsay"

    try:
        proc = subprocess.run(
            [cowsay_path, text],
            capture_output=True,
            text=True,
            check=True
        )
        output = proc.stdout.strip()
        if not output:
            output = "No output from cowsay."
    except FileNotFoundError:
        output = "`cowsay` not found on this system."
    except subprocess.CalledProcessError as e:
        output = f"Error running cowsay:\n{e.stderr or e.stdout}"

    await interaction.followup.send(f"```{output}```")

@app_commands.command(
    name="trace_act",
    description="Audit members inactive for N days and export CSV"
)
@app_commands.describe(
    days="Number of days of inactivity",
    log_channel="Where to send the CSV"
)
@app_commands.checks.has_permissions(administrator=True)
async def trace_act(interaction: Interaction, days: int, log_channel: TextChannel):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("This command can only be used inside a server.", ephemeral=True)
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    bot_member = guild.get_member(interaction.client.user.id) if interaction.client.user else None
    if bot_member is None:
        bot_member = getattr(guild, "me", None)
    if bot_member is None:
        try:
            bot_member = await guild.fetch_member(interaction.client.user.id)
        except discord.HTTPException:
            pass
    if bot_member is None:
        await interaction.followup.send("Unable to resolve bot permissions for this guild.", ephemeral=True)
        return

    if not guild.chunked and guild.member_count and len(guild.members) < guild.member_count:
        try:
            await guild.chunk()
        except discord.HTTPException:
            pass

    members = list(guild.members)
    total_members = len(members)
    if total_members == 0:
        await interaction.followup.send("No members found to audit.", ephemeral=True)
        return

    def build_bar(progress: int, total: int) -> str:
        if total <= 0:
            return "[██████████] 100%"
        percent = int(progress / total * 100)
        percent = max(0, min(100, percent))
        filled = percent // 10
        return "[" + "█" * filled + "░" * (10 - filled) + f"] {percent}%"

    accessible_channels = [
        channel
        for channel in guild.text_channels
        if channel.permissions_for(bot_member).read_message_history
    ]
    total_channels = len(accessible_channels)
    if total_channels == 0:
        await interaction.followup.send("No readable text channels were found for the bot.", ephemeral=True)
        return

    progress_msg = await interaction.followup.send(
        f"Scanning channels… {build_bar(0, total_channels)}",
        ephemeral=True
    )

    activity_map: dict[int, datetime] = {}
    skipped_channels: list[str] = []
    scanned_channels = 0

    for channel in accessible_channels:
        try:
            async for msg in channel.history(limit=None, after=cutoff, oldest_first=False):
                if not msg.author:
                    continue
                last_seen = activity_map.get(msg.author.id)
                if last_seen is None or msg.created_at > last_seen:
                    activity_map[msg.author.id] = msg.created_at
        except discord.Forbidden:
            skipped_channels.append(f"#{channel.name}")
        except discord.HTTPException:
            skipped_channels.append(f"#{channel.name}")

        scanned_channels += 1
        if scanned_channels % max(1, total_channels // 20) == 0 or scanned_channels == total_channels:
            await progress_msg.edit(content=f"Scanning channels… {build_bar(scanned_channels, total_channels)}")

        await asyncio.sleep(0.2)

    await progress_msg.edit(content="Building report…")

    inactive_members = []
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["Member", "ID", "Status", "Last Seen (UTC)"])

    for member in members:
        last_seen = activity_map.get(member.id)
        if last_seen is None:
            inactive_members.append(member)
            writer.writerow([str(member), member.id, "Inactive", "N/A"])
        else:
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            writer.writerow([
                str(member),
                member.id,
                "Active",
                last_seen.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            ])

    csv_buffer.seek(0)
    file = File(fp=io.BytesIO(csv_buffer.read().encode()), filename="inactive_members.csv")

    summary_lines = [
        f"Inactive (> {days}d): {len(inactive_members)}/{total_members}",
        f"Channels scanned: {scanned_channels}/{total_channels}",
    ]
    if skipped_channels:
        summary_lines.append(f"Skipped {len(skipped_channels)} channels (no access).")

    try:
        await log_channel.send("\n".join(summary_lines), file=file)
        await progress_msg.edit(content="Audit complete! Report posted.")
    except discord.Forbidden:
        await progress_msg.edit(
            content="Audit complete, but I could not post in the selected log channel."
        )


@app_commands.command(
    name="rennygadetarget",
    description="Roko's Basilisk"
)
@app_commands.describe(
    user="Who we dusting??"
)
async def rennygadetarget(
    interaction: Interaction,
    user: Member
):
    await interaction.response.defer()

    avatar_url = user.display_avatar.with_format("png").with_size(256).url
    async with aiohttp.ClientSession() as sess:
        async with sess.get(str(avatar_url)) as resp:
            data = await resp.read()

    img = Image.open(BytesIO(data)).convert("RGBA")
    w, h = img.size
    pixels = img.load()

    tile_size = 32
    w_tiles = (w + tile_size - 1) // tile_size
    h_tiles = (h + tile_size - 1) // tile_size

    tiles = []
    for ty in range(h_tiles):
        for tx in range(w_tiles):
            x0, y0 = tx * tile_size, ty * tile_size
            box = (x0, y0, min(x0 + tile_size, w), min(y0 + tile_size, h))
            tile_img = img.crop(box)
            start_frame = random.randint(0, _FRAMES - 1)
            tiles.append({
                "img": tile_img,
                "orig_pos": (x0, y0),
                "start": start_frame,
            })

    frames = []
    gravity = tile_size / _FRAMES  # pixels per frame after start

    for frame_i in range(_FRAMES):
        canvas = Image.new("RGBA", (w, h))
        for t in tiles:
            ox, oy = t["orig_pos"]
            if frame_i < t["start"]:
                canvas.paste(t["img"], (ox, oy), t["img"])
            else:
                dy = int((frame_i - t["start"]) * gravity)
                new_y = oy + dy
                if new_y < h:
                    canvas.paste(t["img"], (ox, new_y), t["img"])
        frames.append(canvas)

    buffer = BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=_FRAME_DURATION,
        loop=0,
        disposal=2,
        transparency=0
    )
    buffer.seek(0)

    await interaction.followup.send(
        content=f"{user.mention}, you’ve been dusted!",
        file=File(fp=buffer, filename="disintegrate.gif")
    )


@app_commands.command(name="attach_search", description="Search attachment file names in a channel and export matches.")
@app_commands.describe(
    channel="Channel to scan",
    query="Filename pattern (text, regex, or glob). Optional if ext filter is set.",
    match="How to match the query",
    case_sensitive="Case sensitive match",
    ext="Comma-separated extensions, e.g. png,jpg,zip",
    min_kb="Min size in KB",
    max_kb="Max size in KB",
    author="Only messages from this user",
    days="Only look back this many days",
    limit="Max messages to scan (up to 5000)",
    log_channel="Where to post results (optional)"
)
@app_commands.checks.has_permissions(view_channel=True, read_message_history=True)
async def attach_search(
    interaction: Interaction,
    channel: discord.TextChannel,
    query: str | None = None,
    match: Literal["contains","exact","regex","glob"] = "contains",
    case_sensitive: bool = False,
    ext: str | None = None,
    min_kb: app_commands.Range[int, 0, 10_000_000] | None = None,
    max_kb: app_commands.Range[int, 0, 10_000_000] | None = None,
    author: discord.Member | None = None,
    days: app_commands.Range[int, 1, 3650] | None = None,
    limit: app_commands.Range[int, 1, 5000] = 2000,
    log_channel: discord.TextChannel | None = None,
):

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    if not query and not ext:
        await interaction.followup.send("Provide a query or an ext filter.", ephemeral=True)
        return

    exts = None
    if ext:
        exts = {"." + e.strip(". ").lower() for e in ext.split(",") if e.strip()}

    cutoff_after = None
    if days:
        cutoff_after = datetime.now(timezone.utc) - timedelta(days=int(days))

    rows = []
    scanned = 0
    progress = await interaction.followup.send("Scanning… 0%", ephemeral=True)

    async for msg in channel.history(limit=int(limit), oldest_first=False, after=cutoff_after):
        scanned += 1
        if author and msg.author.id != author.id:
            if scanned % 250 == 0:
                await progress.edit(content=f"Scanning… {scanned}/{limit}")
            continue
        if not msg.attachments:
            if scanned % 250 == 0:
                await progress.edit(content=f"Scanning… {scanned}/{limit}")
            continue

        for a in msg.attachments:
            name = a.filename or ""
            if exts and not any(name.lower().endswith(x) for x in exts):
                continue
            if min_kb is not None and (a.size or 0) < (min_kb * 1024):
                continue
            if max_kb is not None and (a.size or 0) > (max_kb * 1024):
                continue
            if query and not _attach_match(name, query, match, case_sensitive):
                continue
            jump = f"https://discord.com/channels/{msg.guild.id}/{msg.channel.id}/{msg.id}"
            rows.append({
                "message_id": str(msg.id),
                "author_id": str(msg.author.id),
                "author": str(msg.author),
                "created": msg.created_at.replace(tzinfo=timezone.utc).isoformat(),
                "filename": name,
                "size_bytes": a.size,
                "content_type": a.content_type or "",
                "url": a.url,
                "jump_url": jump,
            })

        if scanned % 250 == 0:
            await progress.edit(content=f"Scanning… {scanned}/{limit}")
            await asyncio.sleep(0)

    await progress.edit(content=f"Done. Matches: {len(rows)}")

    if not rows:
        await interaction.followup.send("No matches.", ephemeral=True)
        return

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    out = discord.File(data, filename=f"attach_search_{channel.id}.csv")

    summary = (f"Attachment search in {channel.mention}\n"
               f"Query: {query or '(none)'} | Mode: {match}"
               + (f" | Ext: {','.join(sorted(exts))}" if exts else "")
               + (f" | Size: {min_kb or 0}–{max_kb or '∞'} KB" if (min_kb is not None or max_kb is not None) else "")
               + (f" | Author: {author.mention}" if author else "")
               + (f" | Days: {days}" if days else "")
               + f"\nMatches: {len(rows)}")

    if log_channel:
        try:
            await log_channel.send(summary, file=out, allowed_mentions=discord.AllowedMentions.none())
            await interaction.followup.send("Posted results to the log channel.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("No permission to post in the log channel.", ephemeral=True)
    else:
        await interaction.followup.send(summary, file=out, ephemeral=True)


IMAGE_DIR = Path("stuart_content/images")
QUOTE_FILE = Path("stuart_content/quotes.json")

def load_stuart_quotes() -> list[str]:
    """Safely load Stuart Little quotes from the JSON file."""
    if not QUOTE_FILE.exists():
        return []
    try:
        with QUOTE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            quotes = data.get("quotes", [])
            if isinstance(quotes, list):
                return quotes
    except Exception as e:
        print(f"[stuartlittle] Error loading quotes: {e}")
    return []


@app_commands.command(
    name="stuartlittle",
    description="Reflect on the actions and legacy of Stuart Little."
)
async def stuartlittle(interaction: discord.Interaction):
    """Send a random Stuart Little slander quote or image."""
    await interaction.response.defer()

    # 50/50: send an image or text
    if random.choice([True, False]):
        if not IMAGE_DIR.exists():
            await interaction.followup.send("No images directory found.")
            return

        images = [p for p in IMAGE_DIR.glob("*") if p.is_file()]
        if not images:
            await interaction.followup.send("No images available.")
            return

        chosen = random.choice(images)
        await interaction.followup.send(file=discord.File(chosen))

    else:
        quotes = load_stuart_quotes()
        if not quotes:
            await interaction.followup.send("No Stuart Little reports found.")
            return

        chosen = random.choice(quotes)
        await interaction.followup.send(chosen)

def setup(tree: app_commands.CommandTree):
    tree.add_command(gitissue)
    tree.add_command(out_of_office)
    tree.add_command(fortune_cmd)
    tree.add_command(cowsay_cmd)
    tree.add_command(trace_act)
    tree.add_command(rennygadetarget)
    tree.add_command(attach_search)
    tree.add_command(stuartlittle)
