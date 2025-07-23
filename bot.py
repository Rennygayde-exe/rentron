
import os
import signal
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from utils.responses import load_responses, match_response
from commands import general, moderation, application, osint
from commands.application import init_db
from commands.application import ApplicationView
from discord import app_commands

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
    moderation.setup(bot.tree)
    bot.tree.add_command(signal_command)
    bot.tree.add_command(blackbird)
    await bot.tree.sync()
    load_responses()
    bot.add_view(ApplicationView())
    print("Bot is ready and commands are synced.")

@bot.tree.command(name="post_application", description="Post the application button")
@app_commands.checks.has_permissions(administrator=True)
async def post_application(interaction: discord.Interaction):
    await interaction.response.send_message("Click below to apply!", view=ApplicationView())


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
        bot.run(token)
    else:
        print("Bot token not found in .env file.")

