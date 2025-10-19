# commands/keyword_alerts.py
import os, re, json, asyncio
from pathlib import Path
from typing import Literal
import discord
from discord import app_commands, Interaction
from discord.ext import commands

STORE = Path(os.getenv("KEYWORD_ALERTS_PATH") or Path(__file__).resolve().parents[1] / "data" / "keyword_alerts.json")
STORE.parent.mkdir(parents=True, exist_ok=True)

def _now(): return discord.utils.utcnow().isoformat()

class KeywordAlerts(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._data = {"guilds": {}}
        self._cool: dict[tuple[int,int,int], float] = {}
        self._load()

    def _load(self):
        if STORE.exists():
            try: self._data = json.loads(STORE.read_text(encoding="utf-8"))
            except: self._data = {"guilds": {}}
        if "guilds" not in self._data: self._data = {"guilds": {}}

    async def _save(self):
        txt = json.dumps(self._data, ensure_ascii=False, indent=2)
        await asyncio.to_thread(STORE.write_text, txt, "utf-8")

    def _g(self, gid: int) -> dict:
        s = self._data["guilds"].get(str(gid))
        if not s:
            s = {"_seq": 1, "rules": []}
            self._data["guilds"][str(gid)] = s
        return s

    keyword = app_commands.Group(name="keyword", description="Keyword tools")
    alerts = app_commands.Group(name="alert", description="Manage keyword alerts", parent=keyword)

    @alerts.command(name="add", description="Add a keyword alert rule")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(
        phrase="Text or regex",
        channel="Where to send alerts",
        match="Match type",
        case_sensitive="Case sensitive match",
        scope_channel="Only watch this channel",
        include_bots="Alert on bot messages",
        cooldown="Seconds between alerts per rule+channel"
    )
    async def alert_add(
        self,
        i: Interaction,
        phrase: str,
        channel: discord.TextChannel,
        match: Literal["contains","exact","regex"]="contains",
        case_sensitive: bool=False,
        scope_channel: discord.TextChannel | None=None,
        include_bots: bool=False,
        cooldown: app_commands.Range[int,1,3600]=20
    ):
        if not i.response.is_done(): await i.response.defer(ephemeral=True, thinking=True)
        async with self._lock:
            g = self._g(i.guild_id)
            rid = int(g["_seq"]); g["_seq"] = rid + 1
            rule = {
                "id": rid,
                "phrase": phrase,
                "match": match,
                "case": bool(case_sensitive),
                "channel_id": int(channel.id),
                "scope_channel_id": int(scope_channel.id) if scope_channel else None,
                "include_bots": bool(include_bots),
                "cooldown": int(cooldown),
                "created_by": str(i.user.id),
                "created_at": _now(),
            }
            if match == "regex":
                try:
                    flags = 0 if case_sensitive else re.IGNORECASE
                    re.compile(phrase, flags)
                except re.error as e:
                    await i.followup.send(f"Invalid regex: {e}", ephemeral=True); return
            g["rules"].append(rule)
            await self._save()
        await i.followup.send(f"Rule #{rid} added → {channel.mention}", ephemeral=True)

    @alerts.command(name="remove", description="Remove a rule by id")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def alert_remove(self, i: Interaction, id: int):
        if not i.response.is_done(): await i.response.defer(ephemeral=True)
        async with self._lock:
            g = self._g(i.guild_id)
            before = len(g["rules"])
            g["rules"] = [r for r in g["rules"] if int(r.get("id",0)) != id]
            ok = len(g["rules"]) != before
            if ok: await self._save()
        await i.followup.send("Removed." if ok else "Not found.", ephemeral=True)

    @alerts.command(name="list", description="List rules")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def alert_list(self, i: Interaction):
        if not i.response.is_done(): await i.response.defer(ephemeral=True)
        g = self._g(i.guild_id)
        if not g["rules"]:
            await i.followup.send("No rules.", ephemeral=True); return
        out = []
        for r in sorted(g["rules"], key=lambda x:int(x["id"])):
            ch = i.guild.get_channel(r["channel_id"])
            scope = i.guild.get_channel(r["scope_channel_id"]) if r.get("scope_channel_id") else None
            out.append(f"#{r['id']} • {r['match']}{' (cs)' if r['case'] else ''} • '{r['phrase']}' • out→ {ch.mention if ch else r['channel_id']}"
                       + (f" • scope: {scope.mention}" if scope else "")
                       + (f" • bots" if r.get('include_bots') else "")
                       + f" • cd:{r.get('cooldown',20)}s")
        txt = "\n".join(out)
        await i.followup.send(txt[:1900] if len(txt)<=1900 else txt[:1900]+"…", ephemeral=True)

    def _match(self, r: dict, m: discord.Message) -> bool:
        if r.get("scope_channel_id") and int(r["scope_channel_id"]) != m.channel.id:
            return False
        if not r.get("include_bots") and m.author.bot:
            return False

        text = m.content or ""
        phrase = r["phrase"]
        case_sensitive = r.get("case", False)
        match_type = r.get("match", "contains")

        flags = 0 if case_sensitive else re.IGNORECASE

        if match_type == "exact":
            return text == phrase if case_sensitive else text.lower() == phrase.lower()

        elif match_type == "contains":
            pattern = rf"\b{re.escape(phrase)}\b"
            return re.search(pattern, text, flags) is not None

        elif match_type == "regex":
            try:
                return re.search(phrase, text, flags) is not None
            except re.error:
                return False

        return False

    @commands.Cog.listener("on_message")
    async def _on_message(self, m: discord.Message):
        if not m.guild or not m.content: return
        g = self._data["guilds"].get(str(m.guild.id))
        if not g or not g.get("rules"): return
        now = discord.utils.utcnow().timestamp()
        for r in g["rules"]:
            try:
                if not self._match(r, m): continue
                key = (m.guild.id, int(r["id"]), m.channel.id)
                cd = int(r.get("cooldown", 20))
                last = self._cool.get(key, 0.0)
                if now - last < cd: continue
                self._cool[key] = now
                out_ch = m.guild.get_channel(int(r["channel_id"]))
                if not out_ch: continue
                jump = f"https://discord.com/channels/{m.guild.id}/{m.channel.id}/{m.id}"
                content = (f"Keyword match #{r['id']}\n"
                           f"Author: {m.author} ({m.author.id})\n"
                           f"Channel: {m.channel.mention}\n"
                           f"Phrase: {r['phrase']} ({r['match']}{' cs' if r.get('case') else ''})\n"
                           f"Link: {jump}\n\n"
                           f"{m.content[:1500]}")

                # ping Staff role only
                role = discord.utils.get(m.guild.roles, name="Staff")
                am = None
                if role:
                    content = f"{role.mention}\n{content}"
                    am = discord.AllowedMentions(
                        everyone=False, users=False, roles=[role], replied_user=False
                    )

                try:
                    await out_ch.send(content, allowed_mentions=am)
                except:pass
            except Exception:
                continue

async def setup(bot: commands.Bot):
    await bot.add_cog(KeywordAlerts(bot))
