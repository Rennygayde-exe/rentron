import json
import os
from discord.ext import commands

RESPONSES = []

@commands.command(name="reloadresponses")
@commands.is_owner()
async def reload_responses(ctx):
    global RESPONSES
    base_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(base_dir)
    file_path = os.path.join(root_dir, "responses.json")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            RESPONSES = data.get("responses", [])
        await ctx.send("Response triggers reloaded.")
    except Exception as e:
        RESPONSES = []
        await ctx.send(f"Failed to load responses: {e}")

@commands.command(name="listresponses")
@commands.is_owner()
async def list_responses(ctx):
    if not RESPONSES:
        await ctx.send("No responses are currently loaded.")
        return

    chunks = []
    chunk = ""

    for entry in RESPONSES:
        try:
            safetrg = entry.get("safetrg", "n/a")
            mention = entry.get("mention_required", False)
            line = f"**Trigger**: `{safetrg}`\n"
            line += f"**Mention Required**: `{mention}`\n\n"

            if len(chunk + line) > 1900:
                chunks.append(chunk)
                chunk = ""
            chunk += line

        except Exception as e:
            await ctx.send(f"Error reading entry: {e}")

    chunks.append(chunk)
    for part in chunks:
        await ctx.send(part)

