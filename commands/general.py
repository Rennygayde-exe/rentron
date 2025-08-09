from discord.ext import commands
from utils.responses import load_responses, RESPONSES
from discord.ui import View, Select, Modal, TextInput
import discord
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
load_dotenv()
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")

_FRAMES = 30
_FRAME_DURATION = 60  # ms per frame

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

@commands.is_owner()
@commands.command(name="reloadresponses")
async def reload_responses(ctx):
    load_responses()
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

@app_commands.command(name="moo", description="Have the cow say your text")
@app_commands.describe(text="Text to have the cow say")
async def cowsay_cmd(interaction: Interaction, text: str):
    await interaction.response.defer()
    proc = subprocess.run(["cowsay", text], capture_output=True, text=True)
    await interaction.followup.send(f"```{proc.stdout}```")

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
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    members = guild.members
    total = len(members)
    checked = 0
    inactive = []

    bar = "[░░░░░░░░░░] 0%"
    progress_msg = await interaction.followup.send(f"Scanning members… {bar}", ephemeral=True)

    for member in members:
        last_seen = None
        for channel in guild.text_channels:
            if not channel.permissions_for(guild.me).read_message_history:
                continue
            try:
                async for msg in channel.history(limit=100, after=cutoff):
                    if msg.author.id == member.id:
                        last_seen = msg.created_at
                        break
                if last_seen:
                    break
            except discord.Forbidden:
                continue

        if last_seen is None:
            inactive.append(member)

        checked += 1
        if checked % max(1, total // 20) == 0 or checked == total:
            percent = int(checked / total * 100)
            filled = percent // 10
            bar = "[" + "█"*filled + "░"*(10-filled) + f"] {percent}%"
            await progress_msg.edit(content=f"Scanning members… {bar}")

        await asyncio.sleep(0.05)

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["Member", "ID", "Status"])
    for m in members:
        status = "Inactive" if m in inactive else "Active"
        writer.writerow([str(m), m.id, status])
    csv_buffer.seek(0)

    file = File(fp=io.BytesIO(csv_buffer.read().encode()), filename="inactive_members.csv")
    await log_channel.send(f"Inactive (> {days}d): {len(inactive)}/{total}", file=file)
    await progress_msg.edit(content="Audit complete!")


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
def setup(tree: app_commands.CommandTree):
    tree.add_command(gitissue)
    tree.add_command(fortune_cmd)
    tree.add_command(cowsay_cmd)
    tree.add_command(trace_act)
    tree.add_command(rennygadetarget)

