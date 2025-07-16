
import os
import signal
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from utils.responses import load_responses, match_response, RESPONSES
from commands import general, moderation, application, osint
from commands.application import init_db
from commands.application import ApplicationView
from discord import app_commands
import re

from commands.osint import blackbird
from signal_handler import signal_command
from discord.ui import View, Button, Modal, TextInput
from discord import Interaction, TextStyle

load_dotenv()

token = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

def shutdown_handler(sig, frame):
    print("\n[main] Shutdown signal received.")
    try:
        loop = asyncio.get_event_loop()
        for task in asyncio.all_tasks(loop):
            task.cancel()
    except RuntimeError:
        pass
    exit(0)

signal.signal(signal.SIGINT, shutdown_handler)

async def load_extensions():
    await bot.load_extension("commands.osint")
async def main():
    async with bot:
        await load_extensions()
        await bot.start(TOKEN)
@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user}")
    bot.add_command(general.reload_responses)
    bot.add_command(general.list_responses)
    bot.tree.add_command(moderation.prune_cmd)
    bot.tree.add_command(moderation.rams_cmd)
    bot.tree.add_command(signal_command)
#   bot.tree.add_command(blackbird)   # Removed functionality for now - ren
    await bot.tree.sync()
    bot.add_view(ApplicationView())
    print("Bot is ready and commands are synced.")

@bot.tree.command(name="post_application", description="Post the application button")
@app_commands.checks.has_permissions(administrator=True)
async def post_application(interaction: discord.Interaction):
    await interaction.response.send_message("Click below to apply!", view=ApplicationView())


def match_response(message, bot):
    for entry in RESPONSES:
        triggers = entry.get("triggers", [])
        mention_required = entry.get("mention_required", False)

        for trig in triggers:
            if re.search(trig, message.content, re.IGNORECASE):
                if mention_required and bot.user not in message.mentions:
                    continue  # Requires a mention but wasn't mentioned

                response = entry.get("response", "")
                return response.replace("{mention}", message.author.mention)
    
    return None

@bot.event
async def on_message(message):
    await bot.process_commands(message)
    if message.author.bot:
        return
    response = match_response(message, bot)
    if response:
        await message.channel.send(response)

if __name__ == "__main__":
    import threading

    if token:
        load_responses()
        asyncio.run(main())
    else:
        print("Bot token not found in .env file.")

