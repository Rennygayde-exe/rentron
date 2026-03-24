import requests, time, sqlite3
from commands.xp import XP_DB_PATH, calculate_level, init_xp_db

GUILD_ID = "663035397416419351"
PAGE_SIZE = 1000

players = []
page = 0
while True:
    url = f"https://mee6.xyz/api/plugins/levels/leaderboard/{GUILD_ID}?page={page}&limit={PAGE_SIZE}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    chunk = data.get("players") or []
    if not chunk:
        break
    players.extend(chunk)
    print(f"Fetched {len(chunk)} users (page {page})")
    page += 1
    if len(chunk) < PAGE_SIZE:
        break
    time.sleep(0.5)                        

print(f"Total fetched: {len(players)}")
print(players[:3])           

init_xp_db()

with sqlite3.connect(XP_DB_PATH) as con:
    cur = con.cursor()
    for entry in players:
        try:
            uid = int(entry["id"])
            xp = int(entry["xp"])
        except (KeyError, ValueError, TypeError):
            continue
        level = calculate_level(xp)
        cur.execute(
            """
            INSERT INTO xp_progress(guild_id, user_id, xp, level)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                xp=excluded.xp,
                level=excluded.level
            """,
            (int(GUILD_ID), uid, xp, level),
        )
    con.commit()
print("XP import complete.")
