import os, json, io, asyncio, math
from pathlib import Path
from typing import Any
import discord
from discord import app_commands, Interaction, File
from discord.ext import commands

NOTES_PATH = Path(os.getenv("NOTES_JSON_PATH") or Path(__file__).resolve().parents[1] / "data" / "mod_notes.json")
NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)

def _now(): return discord.utils.utcnow().isoformat()

class ModNotes(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self):
        if NOTES_PATH.exists():
            try:
                self._data = json.loads(NOTES_PATH.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}
        if "guilds" not in self._data: self._data = {"guilds": {}}

    async def _save(self):
        tmp = NOTES_PATH.with_suffix(".tmp")
        txt = json.dumps(self._data, ensure_ascii=False, indent=2)
        await asyncio.to_thread(tmp.write_text, txt, "utf-8")
        await asyncio.to_thread(tmp.replace, NOTES_PATH)

    def _g(self, gid: int) -> dict:
        key = str(gid)
        g = self._data["guilds"].get(key)
        if not g:
            g = {"_seq": 1, "notes": []}
            self._data["guilds"][key] = g
        return g

    def _next_id(self, gid: int) -> int:
        g = self._g(gid); nid = int(g["_seq"]); g["_seq"] = nid + 1; return nid

    notes_group = app_commands.Group(name="note", description="Moderation notes")

    @notes_group.command(name="add", description="Add a note")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def add(self, interaction: Interaction, member: discord.Member, note: str, tags: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._lock:
            gid = interaction.guild_id
            entry = {
                "id": self._next_id(gid),
                "user_id": str(member.id),
                "author_id": str(interaction.user.id),
                "ts": _now(),
                "note": note,
                "tags": [t.strip() for t in tags.split(",")] if tags else []
            }
            self._g(gid)["notes"].append(entry)
            await self._save()
        await interaction.followup.send(f"Added note #{entry['id']} for {member}.")

    @notes_group.command(name="list", description="List notes for a member")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def list_notes(self, interaction: Interaction, member: discord.Member, page: app_commands.Range[int,1,1000]=1):
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = self._g(interaction.guild_id)
        rows = [n for n in g["notes"] if n["user_id"] == str(member.id)]
        rows.sort(key=lambda x: x["id"])
        per = 10
        pages = max(1, math.ceil(len(rows)/per))
        page = min(max(1, page), pages)
        start = (page-1)*per; chunk = rows[start:start+per]
        if not chunk:
            await interaction.followup.send("No notes found."); return
        lines = []
        for n in chunk:
            t = n["ts"].replace("T"," ").split(".")[0]
            tag = f" [{', '.join(n['tags'])}]" if n.get("tags") else ""
            lines.append(f"#{n['id']} • {t} • by <@{n['author_id']}>{tag}\n{n['note']}")
        await interaction.followup.send(f"Notes for {member} — page {page}/{pages}\n\n" + "\n\n".join(lines))

    @notes_group.command(name="remove", description="Remove a note by id")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def remove(self, interaction: Interaction, id: int):
        await interaction.response.defer(ephemeral=True)
        async with self._lock:
            g = self._g(interaction.guild_id)
            before = len(g["notes"])
            g["notes"] = [n for n in g["notes"] if int(n["id"]) != id]
            changed = len(g["notes"]) != before
            if changed: await self._save()
        await interaction.followup.send("Removed." if changed else "Not found.")

    @notes_group.command(name="edit", description="Edit a note by id")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def edit(self, interaction: Interaction, id: int, note: str):
        await interaction.response.defer(ephemeral=True)
        async with self._lock:
            g = self._g(interaction.guild_id)
            target = next((n for n in g["notes"] if int(n["id"]) == id), None)
            if not target:
                await interaction.followup.send("Not found."); return
            target["note"] = note
            target["edited_ts"] = _now()
            await self._save()
        await interaction.followup.send("Updated.")

    @notes_group.command(name="search", description="Search notes")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def search(self, interaction: Interaction, query: str, member: discord.Member | None = None, author: discord.Member | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = self._g(interaction.guild_id)
        q = query.lower()
        rows = [n for n in g["notes"] if q in n["note"].lower() or any(q in t.lower() for t in n.get("tags", []))]
        if member: rows = [n for n in rows if n["user_id"] == str(member.id)]
        if author: rows = [n for n in rows if n["author_id"] == str(author.id)]
        rows.sort(key=lambda x: x["id"])
        rows = rows[:15]
        if not rows:
            await interaction.followup.send("No matches."); return
        out = []
        for n in rows:
            tag = f" [{', '.join(n['tags'])}]" if n.get("tags") else ""
            out.append(f"#{n['id']} • user <@{n['user_id']}> • by <@{n['author_id']}>{tag}\n{n['note']}")
        await interaction.followup.send("\n\n".join(out))

    @notes_group.command(name="export", description="Export notes to JSON")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def export(self, interaction: Interaction, member: discord.Member | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = self._g(interaction.guild_id)
        rows = g["notes"] if member is None else [n for n in g["notes"] if n["user_id"] == str(member.id)]
        b = io.BytesIO(json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8"))
        await interaction.followup.send(file=File(b, filename=f"notes_{interaction.guild_id}.json"))

async def setup(bot: commands.Bot):
    await bot.add_cog(ModNotes(bot))
