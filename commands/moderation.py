import logging
import re
import sys
import time

log = logging.getLogger(__name__)
import zipfile
import platform
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta
import os, json, io, asyncio, math
from pathlib import Path
from typing import Any
import aiohttp
import pandas as pd
import psutil
import discord
from discord import app_commands, Interaction, Attachment, File
from discord.ext import commands
from dotenv import load_dotenv
from utils.command_book import save_command, get_commands, delete_command
from utils.bot_helpers import save_helper, get_helpers, delete_helper, load_into_namespace

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")
TRACE_LOG_CHANNEL_ID = int(os.getenv("BLACKBIRDLOGS_ID", "0"))

def progress_bar(current: int, total: int, width: int = 30) -> str:
    filled = int(width * current / total) if total else 0
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


NOTES_PATH = Path(os.getenv("NOTES_JSON_PATH") or Path(__file__).resolve().parents[1] / "data" / "mod_notes.json")
NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)

def _now(): return discord.utils.utcnow().isoformat()

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.start_time = time.time()
        self.bot = bot
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {}
        self._load()


    def normalize_ban_entries(payload):
        """
        Accepts many shapes and returns a list of dicts: {"id": str|None, "username": str|None, "reason": str}
        Accepted payloads:
        - ["123", "456"]
        - [{"id":"123","reason":"..."}]
        - {"bans": [...]}
        - {"123":"reason", "456":"reason"}
        """
        def is_id(x):
            s = str(x).strip()
            return s.isdigit() and 17 <= len(s) <= 20

        entries = []

        if isinstance(payload, dict) and "bans" in payload:
            payload = payload["bans"]

        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, (str, int)) and is_id(item):
                    entries.append({"id": str(item).strip(), "username": None, "reason": "No reason provided"})
                elif isinstance(item, dict):
                    uid = None
                    for k in ("id", "user_id", "uid", "snowflake"):
                        if k in item and is_id(item[k]):
                            uid = str(item[k]).strip()
                            break
                    uname = None
                    for k in ("username", "name", "display_name", "global_name"):
                        if k in item and isinstance(item[k], str) and item[k].strip():
                            uname = item[k].strip()
                            break
                    reason = item.get("reason") or "No reason provided"
                    entries.append({"id": uid, "username": uname, "reason": reason})
        elif isinstance(payload, dict):
            for k, v in payload.items():
                if is_id(k):
                    entries.append({"id": k, "username": None, "reason": str(v) if v else "No reason provided"})

        seen = set()
        out = []
        for e in entries:
            key = ("id", e["id"]) if e["id"] else ("user", e["username"])
            if key not in seen:
                seen.add(key)
                out.append(e)
        return out
    @app_commands.command(name="massshadowgenerator", description="Malachor-V Mass Shadow Generator.")
    @app_commands.describe(file="Attach a .json file with ban entries")
    @app_commands.checks.has_permissions(ban_members=True)
    async def massshadowgenerator(self, interaction: Interaction, file: Attachment):
        if not file.filename.endswith(".json"):
            await interaction.response.send_message("Please upload a valid `.json` file.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        message = await interaction.followup.send("Initializing mass shadow generator...")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(file.url) as resp:
                    if resp.status != 200:
                        await message.edit(content="Failed to download JSON.")
                        return
                    content = await resp.text()

            try:
                data = json.loads(content)
                raw = data["bans"] if isinstance(data, dict) and "bans" in data else data
                ban_entries = []
                for item in raw:
                    if isinstance(item, (str, int)) and str(item).isdigit():
                        ban_entries.append({"id": str(item), "username": None, "reason": "No reason provided"})
                    elif isinstance(item, dict):
                        ban_entries.append({
                            "id": str(item.get("id") or item.get("user_id") or item.get("uid") or item.get("snowflake") or "").strip() or None,
                            "username": (item.get("username") or item.get("name") or item.get("display_name") or item.get("global_name")),
                            "reason": item.get("reason") or "No reason provided"
                        })
            except json.JSONDecodeError as e:
                await message.edit(content=f"Invalid JSON format: {e}")
                return

            guild = interaction.guild

            try:
                async for _ in guild.fetch_members(limit=None):
                    pass
            except Exception:
                pass

            name_map = {}
            for m in guild.members:
                for k in (m.name, m.display_name, m.global_name or ""):
                    if k:
                        name_map.setdefault(k.lower(), m)

            total = len(ban_entries)
            banned = failed = skipped_dup = 0
            update_every = max(1, total // 25) if total else 1
            seen_ids: set[int] = set()

            for index, entry in enumerate(ban_entries, start=1):
                user_obj = None
                reason = (entry.get("reason") or "No reason provided")[:512]
                raw_id = entry.get("id")

                target_id: int | None = None
                if raw_id and str(raw_id).isdigit():
                    target_id = int(raw_id)

                if target_id:
                    if target_id in seen_ids:
                        skipped_dup += 1
                    else:
                        try:
                            await guild.ban(discord.Object(id=target_id), reason=reason)
                            seen_ids.add(target_id)
                            banned += 1
                        except Exception as e:
                            uname = (entry.get("username") or "").strip().lower()
                            if uname:
                                user_obj = name_map.get(uname)
                                if user_obj is None:
                                    candidates = [m for k, m in name_map.items() if uname in k]
                                    if len(candidates) == 1:
                                        user_obj = candidates[0]
                            if user_obj:
                                try:
                                    await guild.ban(user_obj, reason=reason)
                                    seen_ids.add(int(user_obj.id))
                                    banned += 1
                                except Exception as ee:
                                    failed += 1
                                    print(f"Failed to ban {target_id} via fallback: {ee}")
                            else:
                                failed += 1
                                print(f"Failed to ban {target_id}: {e}")
                else:
                    uname = (entry.get("username") or "").strip().lower()
                    if not uname:
                        failed += 1
                    else:
                        user_obj = name_map.get(uname)
                        if user_obj is None:
                            candidates = [m for k, m in name_map.items() if uname in k]
                            if len(candidates) == 1:
                                user_obj = candidates[0]
                        if user_obj:
                            tid = int(user_obj.id)
                            if tid in seen_ids:
                                skipped_dup += 1
                            else:
                                try:
                                    await guild.ban(user_obj, reason=reason)
                                    seen_ids.add(tid)
                                    banned += 1
                                except Exception as e:
                                    failed += 1
                                    print(f"Failed to ban {tid}: {e}")
                        else:
                            failed += 1

                if index % update_every == 0 or index == total:
                    bar = progress_bar(index, total)
                    await message.edit(content=f"{bar}\nProgress: {index}/{total}")

            await message.edit(
                content=(
                    "Mass Shadow Generator complete.\n"
                    f"Banned: {banned}\nFailed: {failed}\nSkipped (duplicate): {skipped_dup}"
                )
            )
        except Exception as e:
            await message.edit(content=f"Error: {e}")
    @app_commands.command(name="parse_zip", description="Parse a zip of .txt files into a banlist JSON and Excel.")
    @app_commands.describe(file="Upload a zip file containing .txt files with user info.")
    @app_commands.checks.has_permissions(ban_members=True)
    async def parse_zip(self, interaction: Interaction, file: Attachment):
        if not file.filename.endswith(".zip"):
            await interaction.response.send_message("Please upload a `.zip` file.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(file.url) as resp:
                    if resp.status != 200:
                        return await interaction.followup.send("Failed to download zip.")
                    zip_data = await resp.read()

            banlist = []
            with TemporaryDirectory() as tmpdir:
                tmp_root = Path(tmpdir).resolve()
                with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                    for member in z.infolist():
                        target = (tmp_root / member.filename).resolve()
                        if target != tmp_root and tmp_root not in target.parents:
                            return await interaction.followup.send(
                                f"Rejected: zip contains an unsafe path (`{member.filename}`)."
                            )
                    z.extractall(tmp_root)

                for path in Path(tmpdir).rglob("*.txt"):
                    content = path.read_text(encoding="utf-8", errors="ignore")
                    id_match = re.search(r"Account ID:\s*(\d+)", content)
                    user_match = re.search(r"Username:\s*(.+)", content)
                    if id_match and user_match:
                        banlist.append({
                            "id": id_match.group(1),
                            "username": user_match.group(1),
                            "reason": "Scraped from uploaded dump"
                        })

            if not banlist:
                return await interaction.followup.send("No valid entries found.")

            json_bytes = io.BytesIO(json.dumps(banlist, indent=4).encode())
            json_bytes.seek(0)
            excel_buffer = io.BytesIO()
            pd.DataFrame(banlist).to_excel(excel_buffer, index=False)
            excel_buffer.seek(0)

            await interaction.followup.send("Banlist generated successfully:", files=[
                File(json_bytes, filename="banlist.json"),
                File(excel_buffer, filename="banlist.xlsx")
            ])
        except Exception as e:
            await interaction.followup.send(f"Error: `{e}`", ephemeral=True)

    @app_commands.command(name="botstats", description="View bot uptime and stats.")
    async def botstats(self, interaction: Interaction):
        await interaction.response.defer()
        try:
            proc = await asyncio.create_subprocess_exec(
                "neofetch", "--stdout",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode(errors="replace").strip()
        except Exception as e:
            output = f"Failed to run neofetch: {e}"
        await interaction.followup.send(f"```\n{output}\n```")

    @app_commands.command(name="help", description="List all commands and what they do.")
    async def show_help(self, interaction: Interaction):
        tree = interaction.client.tree
        entries: list[tuple[str, str]] = []
        seen: set[str] = set()

        for cmd in tree.walk_commands():
            if isinstance(cmd, app_commands.Group):
                continue

            key = getattr(cmd, "qualified_name", cmd.name)
            key = f"{key}:{getattr(cmd, 'type', 'unknown')}"
            if key in seen:
                continue
            seen.add(key)

            cmd_type = getattr(cmd, "type", discord.AppCommandType.chat_input)
            if cmd_type is discord.AppCommandType.chat_input:
                label = f"/{getattr(cmd, 'qualified_name', cmd.name)}"
            elif cmd_type is discord.AppCommandType.user:
                label = f"User menu • {cmd.name}"
            elif cmd_type is discord.AppCommandType.message:
                label = f"Message menu • {cmd.name}"
            else:
                label = cmd.name

            description = (getattr(cmd, "description", "") or "No description provided.").strip()
            entries.append((label, description))

        if not entries:
            await interaction.response.send_message("No commands are currently registered.", ephemeral=True)
            return

        entries.sort(key=lambda x: x[0].lower())
        lines = [f"{label} — {desc}" for label, desc in entries]

        chunks: list[str] = []
        current = ""
        limit = 3500
        for line in lines:
            addition = f"{line}\n"
            if len(current) + len(addition) > limit and current:
                chunks.append(current.rstrip())
                current = addition
            else:
                current += addition
        if current:
            chunks.append(current.rstrip())

        embed = discord.Embed(
            title="RenTron Command Manual",
            description=chunks[0],
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"{len(entries)} commands available")

        await interaction.response.send_message(embed=embed, ephemeral=True)

        for chunk in chunks[1:]:
            followup_embed = discord.Embed(description=chunk, color=discord.Color.blurple())
            await interaction.followup.send(embed=followup_embed, ephemeral=True)

    @app_commands.command(name="restart_bot", description="Restarts Rentron.")
    async def restart_bot(self, interaction: Interaction):
        if not any(role.name in ("Admin", "S6 Professional", "Staff") for role in interaction.user.roles):
            await interaction.response.send_message("Permission denied.", ephemeral=True)
            return
        await interaction.response.send_message("Restarting.", ephemeral=True)
        await interaction.client.close()
        os.execv(sys.executable, ['python'] + sys.argv)

    @app_commands.command(name="massmove", description="Move everyone from one voice channel to another.")
    @app_commands.describe(from_channel="Source voice channel", to_channel="Destination voice channel", delay_ms="Delay per move (0-1000)")
    @app_commands.checks.has_permissions(move_members=True)
    async def massmove(
        self,
        interaction: Interaction,
        from_channel: discord.VoiceChannel,
        to_channel: discord.VoiceChannel,
        delay_ms: int = 100
    ):
        if from_channel.id == to_channel.id:
            await interaction.response.send_message("Source and destination must be different.", ephemeral=True)
            return
        members = list(from_channel.members)
        if not members:
            await interaction.response.send_message("No members to move.", ephemeral=True)
            return
        delay_ms = max(0, min(1000, delay_ms))
        await interaction.response.defer(ephemeral=True, thinking=True)

        moved = 0
        failed = 0
        for m in members:
            try:
                await m.move_to(to_channel, reason=f"Mass move by {interaction.user}")
                moved += 1
            except Exception as e:
                failed += 1
                print(f"Failed to move {m} ({m.id}): {e}")
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)

        await interaction.followup.send(f"Moved {moved} member(s). Failed: {failed}", ephemeral=True)
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


class BotCommandEditModal(discord.ui.Modal, title="Edit Code"):
    code = discord.ui.TextInput(
        label="Code",
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )

    def __init__(self, current_code: str):
        super().__init__()
        self.code.default = current_code
        self.edited_code = None

    async def on_submit(self, interaction: Interaction):
        self.edited_code = self.code.value
        await interaction.response.defer()


class BotCommandConfirmView(discord.ui.View):
    def __init__(self, action: dict, executor: discord.Member, code: str = ""):
        super().__init__(timeout=120)
        self.action = action
        self.executor = executor
        self.result = None
        self.code = code

    async def interaction_check(self, interaction: Interaction) -> bool:
        return interaction.user.id == self.executor.id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: Interaction, button: discord.ui.Button):
        self.result = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.blurple)
    async def edit(self, interaction: Interaction, button: discord.ui.Button):
        modal = BotCommandEditModal(self.code)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.edited_code:
            self.code = modal.edited_code
            await interaction.edit_original_response(
                content=f"**Plan — review this code before confirming:**\n```python\n{self.code[:1500]}\n```",
                view=self,
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: Interaction, button: discord.ui.Button):
        self.result = False
        self.stop()
        await interaction.response.defer()


async def _exec_code(interaction: Interaction, code: str):
    """Execute botcommand-style code. Returns (result_str, error_str)."""
    guild = interaction.guild
    import datetime as _dt

    def find_channel(query: str):
        query = str(query).strip().lstrip("<#").rstrip(">").lstrip("#")
        if query.isdigit():
            return guild.get_channel(int(query))
        q = query.lower()
        return (discord.utils.find(lambda c: c.name.lower() == q, guild.channels)
                or discord.utils.find(lambda c: q in c.name.lower(), guild.channels))

    def find_member(query: str):
        query = str(query).strip().lstrip("<@!").rstrip(">")
        if query.isdigit():
            return guild.get_member(int(query))
        return discord.utils.find(
            lambda m: m.name.lower() == query.lower() or m.display_name.lower() == query.lower(),
            guild.members,
        )

    def find_role(query: str):
        query = str(query).strip().lstrip("<@&").rstrip(">")
        if query.isdigit():
            return guild.get_role(int(query))
        return discord.utils.find(lambda r: r.name.lower() == query.lower(), guild.roles)

    def list_roles(prefix: str = "") -> list:
        return [r for r in guild.roles if r.name.lower().startswith(prefix.lower())]

    def list_members_with_role(role_name: str) -> list:
        role = find_role(role_name)
        return [m for m in guild.members if role in m.roles] if role else []

    def list_channels(prefix: str = "") -> list:
        return [c for c in guild.text_channels if c.name.lower().startswith(prefix.lower())]

    async def search_messages(keyword: str, limit_per_channel: int = 500, channel=None) -> list:
        results = []
        channels = [channel] if channel else guild.text_channels
        for ch in channels:
            try:
                async for msg in ch.history(limit=limit_per_channel):
                    if keyword.lower() in msg.content.lower() and not msg.author.bot:
                        results.append({"author": str(msg.author), "author_id": msg.author.id, "channel": ch.name, "content": msg.content, "jump_url": msg.jump_url})
            except Exception:
                continue
        return results

    async def get_recent_messages(channel, limit: int = 50) -> list:
        return [{"author": str(m.author), "author_id": m.author.id, "content": m.content, "created_at": m.created_at, "jump_url": m.jump_url, "id": m.id} async for m in channel.history(limit=limit)]

    async def get_member_messages(member, limit_per_channel: int = 200) -> list:
        results = []
        for ch in guild.text_channels:
            try:
                async for msg in ch.history(limit=limit_per_channel):
                    if msg.author.id == member.id:
                        results.append({"channel": ch.name, "content": msg.content, "created_at": msg.created_at, "jump_url": msg.jump_url})
            except Exception:
                continue
        return results

    async def kick_member(member, reason: str = "No reason provided"):
        await member.kick(reason=reason); return f"Kicked {member}"

    async def ban_member(member, reason: str = "No reason provided", delete_days: int = 0):
        await member.ban(reason=reason, delete_message_days=delete_days); return f"Banned {member}"

    async def timeout_member(member, minutes: int = 10, reason: str = "No reason provided"):
        await member.timeout(_dt.timedelta(minutes=minutes), reason=reason); return f"Timed out {member} for {minutes} minutes"

    async def send_dm(member, message: str):
        await member.send(message); return f"DMed {member}"

    async def send_message(channel, message: str):
        await channel.send(message); return f"Sent message to #{channel.name}"

    async def add_role(member, role):
        await member.add_roles(role); return f"Gave {member} the {role.name} role"

    async def remove_role(member, role):
        await member.remove_roles(role); return f"Removed {role.name} from {member}"

    async def set_nickname(member, nickname: str):
        await member.edit(nick=nickname); return f"Set {member}'s nickname to {nickname}"

    async def create_role(name: str, color=None):
        role = await guild.create_role(name=name, color=color or discord.Color.default()); return f"Created role {role.name}"

    async def delete_role(role):
        name = role.name; await role.delete(); return f"Deleted role {name}"

    async def create_channel(name: str, category=None):
        ch = await guild.create_text_channel(name, category=category); return f"Created channel #{ch.name}"

    async def delete_channel(channel):
        name = channel.name; await channel.delete(); return f"Deleted channel #{name}"

    async def purge_channel(channel, limit: int = 10):
        deleted = await channel.purge(limit=limit); return f"Deleted {len(deleted)} messages from #{channel.name}"

    async def get_audit_log(limit: int = 20) -> list:
        return [{"action": str(e.action), "user": str(e.user), "target": str(e.target), "reason": e.reason, "created_at": e.created_at} async for e in guild.audit_logs(limit=limit)]

    def get_member_roles(member) -> list:
        return [r.name for r in member.roles if r.name != "@everyone"]

    def get_members_joined_after(date) -> list:
        return [m for m in guild.members if m.joined_at and m.joined_at > date]

    def get_members_joined_before(date) -> list:
        return [m for m in guild.members if m.joined_at and m.joined_at < date]

    def get_online_members() -> list:
        return [m for m in guild.members if m.status != discord.Status.offline]

    def member_info(member) -> dict:
        return {"name": str(member), "id": member.id, "display_name": member.display_name, "joined_at": str(member.joined_at), "created_at": str(member.created_at), "roles": [r.name for r in member.roles if r.name != "@everyone"], "top_role": member.top_role.name, "bot": member.bot}

    def role_info(role) -> dict:
        return {"name": role.name, "id": role.id, "color": str(role.color), "members": len(role.members), "mentionable": role.mentionable, "position": role.position}

    import pathlib as _pathlib
    _bot_dir = _pathlib.Path(__file__).parent.parent
    ns = {
        "discord": discord, "asyncio": asyncio, "timedelta": timedelta, "datetime": _dt, "timezone": _dt.timezone,
        "Path": _pathlib.Path, "BOT_DIR": _bot_dir,
        "find_channel": find_channel, "find_member": find_member, "find_role": find_role,
        "list_roles": list_roles, "list_members_with_role": list_members_with_role, "list_channels": list_channels,
        "search_messages": search_messages, "get_recent_messages": get_recent_messages, "get_member_messages": get_member_messages,
        "kick_member": kick_member, "ban_member": ban_member, "timeout_member": timeout_member,
        "send_dm": send_dm, "send_message": send_message, "add_role": add_role, "remove_role": remove_role,
        "set_nickname": set_nickname, "create_role": create_role, "delete_role": delete_role,
        "create_channel": create_channel, "delete_channel": delete_channel, "purge_channel": purge_channel,
        "get_audit_log": get_audit_log, "get_member_roles": get_member_roles,
        "get_members_joined_after": get_members_joined_after, "get_members_joined_before": get_members_joined_before,
        "get_online_members": get_online_members, "member_info": member_info, "role_info": role_info,
    }
    load_into_namespace(ns)
    log.warning(
        "botcommand EXEC by %s (%s) in guild %s:\n%s",
        interaction.user, interaction.user.id, getattr(guild, "id", None), code,
    )
    if TRACE_LOG_CHANNEL_ID:
        log_ch = interaction.client.get_channel(TRACE_LOG_CHANNEL_ID)
        if log_ch:
            try:
                await log_ch.send(
                    f"**botcommand exec** by {interaction.user.mention} (`{interaction.user.id}`) "
                    f"in guild `{getattr(guild, 'id', '?')}`:\n```python\n{code[:1800]}\n```"
                )
            except Exception:
                pass
    try:
        exec(compile(code, "<botcommand>", "exec"), ns)
        run_fn = ns.get("run")
        if not run_fn:
            return None, "No `run` function found in generated code."
        result = await run_fn(interaction.client, guild, interaction)
        return str(result), None
    except Exception as e:
        return None, str(e)


class SaveConfirmView(discord.ui.View):
    def __init__(self, executor, prompt: str, code: str, msg, result_content: str):
        super().__init__(timeout=60)
        self.executor = executor
        self.prompt = prompt
        self.code = code
        self.msg = msg
        self.result_content = result_content

    async def interaction_check(self, interaction: Interaction) -> bool:
        return interaction.user.id == self.executor.id

    @discord.ui.button(label="Save", style=discord.ButtonStyle.green)
    async def save(self, interaction: Interaction, button: discord.ui.Button):
        self.stop()
        modal = SaveCommandModal(self.prompt, self.code, self.msg)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.gray)
    async def cancel(self, interaction: Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content=self.result_content, view=None)


class BotCommandInputModal(discord.ui.Modal, title="Bot Command"):
    command = discord.ui.TextInput(
        label="Command",
        style=discord.TextStyle.paragraph,
        placeholder="What do you want the bot to do?",
        max_length=4000,
    )

    def __init__(self, model: str, hidden: bool, file):
        super().__init__()
        self.model = model
        self.hidden = hidden
        self.file = file
        self.submitted = False
        self.command_text = None
        self.modal_interaction = None

    async def on_submit(self, interaction: Interaction):
        self.submitted = True
        self.command_text = self.command.value
        self.modal_interaction = interaction
        self.stop()


class BotPythonModal(discord.ui.Modal, title="Run Python Code"):
    code = discord.ui.TextInput(
        label="Code",
        style=discord.TextStyle.paragraph,
        placeholder="async def run(bot, guild, interaction):\n    return str(len(guild.members))",
        max_length=4000,
    )

    async def on_submit(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        await _execute_botpython(interaction, self.code.value, hidden=True)


class BotPython(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="botpython", description="Execute custom Python code against the bot and guild.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(hidden="Whether the response is only visible to you (default: True)")
    async def botpython(self, interaction: Interaction, hidden: bool = True):
        modal = BotPythonModal()
        await interaction.response.send_modal(modal)


async def _execute_botpython(interaction: Interaction, code: str, hidden: bool = True):
    guild = interaction.guild
    import datetime as _dt
    import io

    def find_channel(query: str):
        query = str(query).strip().lstrip("<#").rstrip(">").lstrip("#")
        if query.isdigit():
            return guild.get_channel(int(query))
        q = query.lower()
        return (discord.utils.find(lambda c: c.name.lower() == q, guild.channels)
                or discord.utils.find(lambda c: q in c.name.lower(), guild.channels))

    def find_member(query: str):
        query = str(query).strip().lstrip("<@!").rstrip(">")
        if query.isdigit():
            return guild.get_member(int(query))
        return discord.utils.find(
            lambda m: m.name.lower() == query.lower() or m.display_name.lower() == query.lower(),
            guild.members,
        )

    def find_role(query: str):
        query = str(query).strip().lstrip("<@&").rstrip(">")
        if query.isdigit():
            return guild.get_role(int(query))
        return discord.utils.find(lambda r: r.name.lower() == query.lower(), guild.roles)

    namespace = {
        "discord": discord,
        "asyncio": asyncio,
        "timedelta": timedelta,
        "datetime": _dt,
        "timezone": _dt.timezone,
        "find_channel": find_channel,
        "find_member": find_member,
        "find_role": find_role,
        "guild": guild,
        "bot": interaction.client,
        "interaction": interaction,
    }

    log.warning(
        "botpython EXEC by %s (%s) in guild %s:\n%s",
        interaction.user, interaction.user.id, getattr(guild, "id", None), code,
    )
    if TRACE_LOG_CHANNEL_ID:
        log_ch = interaction.client.get_channel(TRACE_LOG_CHANNEL_ID)
        if log_ch:
            try:
                await log_ch.send(
                    f"**botpython exec** by {interaction.user.mention} (`{interaction.user.id}`):\n```python\n{code[:1800]}\n```"
                )
            except Exception:
                pass
    try:
        exec(compile(code, "<botpython>", "exec"), namespace)
        run_fn = namespace.get("run")
        if not run_fn:
            await interaction.followup.send("Error: code must define an `async def run(bot, guild, interaction)` function.", ephemeral=hidden)
            return
        result = await run_fn(interaction.client, guild, interaction)
        output = f"```\n{result}\n```"
        if len(output) > 1990:
            file = discord.File(io.BytesIO(str(result).encode()), filename="output.txt")
            await interaction.followup.send("Output too long, see attached file.", file=file, ephemeral=hidden)
        else:
            await interaction.followup.send(output, ephemeral=hidden)
    except Exception as e:
        await interaction.followup.send(f"Error: `{e}`", ephemeral=hidden)


class Privacy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _build_embed_from_markdown(
        self,
        file_path: Path,
        *,
        title: str,
        description: str,
        footer: str | None = None,
    ) -> discord.Embed | None:
        if not file_path.exists():
            return None

        text = file_path.read_text(encoding="utf-8")
        sections = text.split("---")

        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.blue()
        )

        for section in sections:
            raw_lines = [line.strip() for line in section.strip().splitlines() if line.strip()]
            cleaned_lines = []
            for line in raw_lines:
                cleaned = re.sub(r"<[^>]+>", "", line).strip()
                if cleaned:
                    cleaned_lines.append(cleaned)
            if not cleaned_lines:
                continue
            header = cleaned_lines[0].lstrip("#").strip() or "Section"
            body = "\n".join(cleaned_lines[1:]).strip()
            if not body:
                body = "*No additional details provided.*"
            if len(body) > 1024:
                for i in range(0, len(body), 1024):
                    chunk = body[i:i+1024]
                    embed.add_field(name=header if i == 0 else f"{header} (cont.)", value=chunk, inline=False)
            else:
                embed.add_field(name=header, value=body, inline=False)

        if footer:
            embed.set_footer(text=footer)

        return embed

    @app_commands.command(name="privacy", description="View RenTron's official Privacy Policy.")
    async def privacy(self, interaction: discord.Interaction):
        policy_path = Path("privacy.md")

        embed = self._build_embed_from_markdown(
            policy_path,
            title="Privacy Policy of RenTron",
            description="Summary of RenTron's privacy and data handling practices.",
            footer="© 2025 RenTron • Tools at the crossroads of security, creativity, and community."
        )

        if embed is None:
            await interaction.response.send_message(
                "Privacy Policy file (`privacy.md`) not found on the server.",
                ephemeral=True
            )
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
    await bot.add_cog(Privacy(bot))
    await bot.add_cog(BotPython(bot))
