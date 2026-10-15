import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

LOG_FILE = Path(__file__).parent.parent / "data" / "command_log.json"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log_command(command: str, user_id: int, guild_id: int):
    entry = {
        "command": command,
        "user_id": str(user_id),
        "guild_id": str(guild_id),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        data = json.loads(LOG_FILE.read_text()) if LOG_FILE.exists() else []
        data.append(entry)
        if len(data) > 10000:
            data = data[-10000:]
        LOG_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def get_log(days: int = 7) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        data = json.loads(LOG_FILE.read_text()) if LOG_FILE.exists() else []
        return [e for e in data if datetime.fromisoformat(e["ts"]) > cutoff]
    except Exception:
        return []
