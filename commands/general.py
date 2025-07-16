from discord.ext import commands
from utils.responses import load_responses, RESPONSES
import json
import os

RESPONSES = []

def load_responses():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(base_dir)
    file_path = os.path.join(root_dir, "responses.json")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            RESPONSES.clear()
            RESPONSES.extend(data.get("responses", []))
            print(f"[responses] Loaded {len(RESPONSES)} entries.")
    except Exception as e:
        print(f"[responses] Failed to load: {e}")
        RESPONSES.clear()

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
            line += f"**Response**: {entry.get('response', 'No response')}`\n"
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
