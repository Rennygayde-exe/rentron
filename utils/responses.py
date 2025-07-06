import json
import re

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

def match_response(message, bot):
    content = message.content.lower()
    for entry in RESPONSES:
        if entry.get("mention_required", False) and bot.user not in message.mentions:
            continue
        for pattern in entry.get("triggers", []):
            if re.search(pattern, content):
                response = entry.get("response", "")
                if "{mention}" in response:
                    response = response.format(mention=message.author.mention)
                return response
    return None
