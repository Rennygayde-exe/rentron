from discord.ext import commands
from utils.responses import load_responses, RESPONSES
from discord.ui import View, Select, Modal, TextInput
import discord
from discord import app_commands, Interaction
from discord.ui import View, Select, Modal, TextInput
import aiohttp
import os
import requests
import openai
from openai import AsyncOpenAI
from dotenv import load_dotenv
import subprocess
load_dotenv()
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")

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

@app_commands.command(
    name="moo",
    description="Moooooooo"
)
@app_commands.describe(
    text="Mooooo"
)
async def cowsay_cmd(interaction: Interaction, text: str):
    await interaction.response.defer(thinking=True)
    try:
        proc = subprocess.run(
            ["cowsay", text],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        output = proc.stdout.strip() or proc.stderr.strip() or "No output."
    except Exception as e:
        output = f"No Moos {e}"

    await interaction.followup.send(f"```\n{output}\n```")

def setup(tree: app_commands.CommandTree):
    tree.add_command(gitissue)
    tree.add_command(fortune_cmd)
    tree.add_command(cowsay_cmd)
