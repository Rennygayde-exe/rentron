import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import signal
import json
import io
import csv
import re
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone


load_dotenv()

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


RESPONSES = []

def load_responses():
    global RESPONSES
    try:
        with open("responses.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            RESPONSES = data.get("responses", [])
            print(f"[responses] Loaded {len(RESPONSES)} entries.")
    except Exception as e:
        print(f"[responses] Failed to load: {e}")

load_responses()

@bot.command(name="reloadresponses")
@commands.is_owner()
async def reload_responses(ctx):
    load_responses()
    await ctx.send("Response triggers reloaded.")


async def cli_chatbox(bot_instance: commands.Bot):
    await bot_instance.wait_until_ready()
    await asyncio.sleep(1)
    channels = [c for g in bot_instance.guilds for c in g.text_channels]

    if not channels:
        print("No text channels found.")
        return

    current_channel = None

    def list_channels():
        print("\nAvailable Channels:")
        for idx, c in enumerate(channels):
            print(f"{idx + 1}: #{c.name} ({c.guild.name})")

    async def show_recent_messages(channel):
        print(f"\nLast 5 messages in #{channel.name}:\n")
        messages = [msg async for msg in channel.history(limit=5)]
        for msg in reversed(messages):
            print(f"[{msg.id}] {msg.author}: {msg.content}")

    loop = asyncio.get_running_loop()
    list_channels()

    while True:
        try:
            if current_channel is None:
                sel = await loop.run_in_executor(None, input, "\nSelect channel number: ")
                if sel.strip().lower() == "exit":
                    print("Exiting CLI chatbox.")
                    break
                try:
                    current_channel = channels[int(sel) - 1]
                    await show_recent_messages(current_channel)
                    print("\nNow chatting. Use '!switch' to change channels.")
                    print("To reply: !reply <message_id> your message\n")
                except (IndexError, ValueError):
                    print("Invalid selection.")
                    continue

            content = await loop.run_in_executor(None, input, "> ")
            if content.strip().lower() == "exit":
                print("Exiting CLI chatbox.")
                break
            elif content.strip().lower() == "!switch":
                current_channel = None
                list_channels()
                continue
            elif content.startswith("!reply "):
                try:
                    parts = content.split(maxsplit=2)
                    msg_id = int(parts[1])
                    message_content = parts[2] if len(parts) > 2 else ""
                    ref = discord.MessageReference(
                        message_id=msg_id,
                        channel_id=current_channel.id,
                        guild_id=current_channel.guild.id
                    )
                    await current_channel.send(content=message_content.strip(), reference=ref)
                except Exception as e:
                    print(f"Failed to reply: {e}")
            else:
                try:
                    await current_channel.send(content.strip())
                except Exception as e:
                    print(f"Failed to send: {e}")

        except (EOFError, KeyboardInterrupt):
            print("\n[CLI] Input closed or interrupted. Exiting.")
            break


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        await tree.sync()
    except Exception as e:
        print(f"Failed to sync: {e}")
    asyncio.create_task(cli_chatbox(bot))

@bot.event
async def on_message(message):
    await bot.process_commands(message)

    if message.author.bot:
        return

    content = message.content.lower()

    for entry in RESPONSES:
        mention_required = entry.get("mention_required", False)
        if mention_required and bot.user not in message.mentions:
            continue

        for pattern in entry["triggers"]:
            if re.search(pattern, content):
                response = entry["response"]
                if "{mention}" in response:
                    response = response.format(mention=message.author.mention)
                await message.channel.send(response)
                return

@tree.command(
    name="prune_attachments",
    description="Delete messages with attachments older than the specified time in a specific channel."
)
@app_commands.describe(
    channel="Channel to prune",
    amount="How far back to prune (as a number)",
    unit="Time unit: seconds, minutes, hours, days, weeks, or years",
    type="Attachment type to delete: 'all' or 'images'"
)
async def prune_attachments(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    amount: int,
    unit: str,
    type: str
):
    if not any(role.name == "Staff" for role in interaction.user.roles):
        await interaction.response.send_message("You don't have permission to run this command.", ephemeral=True)
        return

    unit = unit.lower()
    type = type.lower()
    if unit not in ["seconds", "minutes", "hours", "days", "weeks", "years"]:
        await interaction.response.send_message("Invalid unit", ephemeral=True)
        return
    if type not in ["all", "images"]:
        await interaction.response.send_message("Invalid type", ephemeral=True)
        return

    time_kwargs = {unit: amount} if unit != "years" else {"days": amount * 365}
    cutoff = datetime.now(timezone.utc) - timedelta(**time_kwargs)

    await interaction.response.send_message(
        f"Deleting {type} attachments older than {amount} {unit} in #{channel.name}...",
        ephemeral=True
    )

    deleted = []
    async for message in channel.history(limit=2000):
        if message.created_at < cutoff and message.attachments:
            if type == "images" and not any(att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")) for att in message.attachments):
                continue
            try:
                await message.delete()
                deleted.append({
                    "Author": f"{message.author} ({message.author.id})",
                    "Date": message.created_at.isoformat(),
                    "Attachment": message.attachments[0].url,
                    "Message ID": message.id,
                    "Channel": channel.name
                })
                await asyncio.sleep(0.75)
            except Exception as e:
                print(f"Error deleting message: {e}")

    if deleted:
        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=["Author", "Date", "Attachment", "Message ID", "Channel"])
        writer.writeheader()
        writer.writerows(deleted)
        csv_buffer.seek(0)
        csv_file = discord.File(fp=io.BytesIO(csv_buffer.read().encode()), filename="pruned_attachments_log.csv")

        log_channel = discord.utils.get(interaction.guild.text_channels, name="backdoor-bot-stuff")
        if log_channel:
            await log_channel.send(f"{len(deleted)} messages deleted from #{channel.name} by {interaction.user.mention}", file=csv_file)

    await interaction.followup.send(f"Deleted {len(deleted)} messages from #{channel.name}.", ephemeral=True)


@bot.command(name="listresponses")
@commands.is_owner()
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

def start_bot():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("Bot token not found in .env file.")
        return

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
    bot.run(token)

if __name__ == "__main__":
    start_bot()
