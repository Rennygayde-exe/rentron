import json
import os
from discord.ext import commands
from utils.responses import load_responses, RESPONSES

RESPONSES = []

@commands.is_owner()
@commands.command(name="listresponses")
def listresponses():
    global RESPONSES
    base_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(base_dir)
    file_path = os.path.join(root_dir, "responses.json")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            RESPONSES = json.load(f)
    except Exception as e:
        print(f"Error loading responses.json: {e}")
        RESPONSES = []
