import os
import signal
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from utils.responses import load_responses, match_response
from commands import general, moderation
from signal_handler import signal_command

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

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    bot.add_command(general.reload_responses)
    bot.add_command(general.list_responses)
    bot.tree.add_command(moderation.prune_cmd)
    bot.tree.add_command(moderation.rams_cmd)
    bot.tree.add_command(signal_command)
    await bot.tree.sync()
    load_responses()

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

