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
