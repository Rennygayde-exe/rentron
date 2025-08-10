import os, io, csv, json, sqlite3, asyncio
from pathlib import Path
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = str(Path(os.getenv("APPLICATIONS_DB") or (BASE_DIR / "applications.db")))
STAFF_REVIEW_CHANNEL_ID = int(os.getenv("STAFF_REVIEW_CHANNEL_ID", "0"))
TICKET_LOG_CHANNEL_ID = int(os.getenv("TICKET_LOG_CHANNEL_ID", "0"))
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "0"))


# SQL Shit
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        c = con.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS applications(
            user_id INTEGER PRIMARY KEY, submitted_at TEXT, status TEXT NOT NULL DEFAULT 'pending')""")
        c.execute("""CREATE TABLE IF NOT EXISTS pending_applications(
            message_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, data TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets(
            message_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS app_sessions(
            message_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, data TEXT NOT NULL)""")
        con.commit()

def has_submitted(user_id:int)->bool:
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("SELECT 1 FROM applications WHERE user_id=?", (int(user_id),)).fetchone() is not None

def mark_as_submitted(user_id:int, submitted_at:str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR REPLACE INTO applications(user_id,submitted_at,status) VALUES(?,?,'pending')",
                    (int(user_id), submitted_at)); con.commit()

def store_pending_application(message_id:int, user_id:int, data:dict):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR REPLACE INTO pending_applications(message_id,user_id,data) VALUES(?,?,?)",
                    (int(message_id), int(user_id), json.dumps(data))); con.commit()

def delete_pending(*, user_id:int|None=None, message_id:int|None=None):
    with sqlite3.connect(DB_PATH) as con:
        if user_id: con.execute("DELETE FROM pending_applications WHERE user_id=?", (int(user_id),))
        if message_id: con.execute("DELETE FROM pending_applications WHERE message_id=?", (int(message_id),))
        con.commit()

def session_get(mid:int)->dict|None:
    with sqlite3.connect(DB_PATH) as con:
        r = con.execute("SELECT data FROM app_sessions WHERE message_id=?", (int(mid),)).fetchone()
        return json.loads(r[0]) if r else None

def session_set(mid:int, uid:int, data:dict):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR REPLACE INTO app_sessions(message_id,user_id,data) VALUES(?,?,?)",
                    (int(mid), int(uid), json.dumps(data))); con.commit()

def session_del(mid:int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM app_sessions WHERE message_id=?", (int(mid),)); con.commit()

class ApplicationFormView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="1. Preferred Name", style=discord.ButtonStyle.secondary, custom_id="app:name")
    async def name_btn(self, i: discord.Interaction, _: discord.ui.Button):
        msg_id = i.message.id
        class M(discord.ui.Modal, title="Preferred Name"):
            def __init__(self, mid: int):
                super().__init__(timeout=None)
                self.mid = mid
                self.v = discord.ui.TextInput(label="First name only", style=discord.TextStyle.short, required=True)
                self.add_item(self.v)
            async def on_submit(ms, mi: discord.Interaction):
                d = session_get(ms.mid) or {}
                d["name"] = str(ms.v.value)
                session_set(ms.mid, mi.user.id, d)
                await mi.response.send_message("Saved.", ephemeral=(mi.guild_id is not None))
        await i.response.send_modal(M(msg_id))

    @discord.ui.button(label="2. Pronouns", style=discord.ButtonStyle.secondary, custom_id="app:pronouns")
    async def pronouns_btn(self, i: discord.Interaction, _: discord.ui.Button):
        msg_id = i.message.id
        class M(discord.ui.Modal, title="Pronouns"):
            def __init__(self, mid: int):
                super().__init__(timeout=None)
                self.mid = mid
                self.v = discord.ui.TextInput(label="she/her, he/him, etc.", style=discord.TextStyle.short, required=True)
                self.add_item(self.v)
            async def on_submit(ms, mi: discord.Interaction):
                d = session_get(ms.mid) or {}
                d["pronouns"] = str(ms.v.value)
                session_set(ms.mid, mi.user.id, d)
                await mi.response.send_message("Saved.", ephemeral=(mi.guild_id is not None))
        await i.response.send_modal(M(msg_id))

    @discord.ui.button(label="3. Referral Source", style=discord.ButtonStyle.secondary, custom_id="app:refer")
    async def refer_btn(self, i: discord.Interaction, _: discord.ui.Button):
        msg_id = i.message.id
        class M(discord.ui.Modal, title="Referral Source"):
            def __init__(self, mid: int):
                super().__init__(timeout=None)
                self.mid = mid
                self.v = discord.ui.TextInput(label="Where did you hear about us?", style=discord.TextStyle.paragraph, required=True)
                self.add_item(self.v)
            async def on_submit(ms, mi: discord.Interaction):
                d = session_get(ms.mid) or {}
                d["refer"] = str(ms.v.value)
                session_set(ms.mid, mi.user.id, d)
                await mi.response.send_message("Saved.", ephemeral=(mi.guild_id is not None))
        await i.response.send_modal(M(msg_id))

    @discord.ui.select(placeholder="Select your branch",
                       options=[discord.SelectOption(label=x) for x in ["Army","Navy","Marines","Air Force","Coast Guard","Space Force","Family"]],
                       custom_id="app:branch")
    async def branch_sel(self, i: discord.Interaction, sel: discord.ui.Select):
        d = session_get(i.message.id) or {}
        d["branch_choice"] = sel.values[0]
        session_set(i.message.id, i.user.id, d)
        await i.response.defer()

    @discord.ui.select(placeholder="Select your status",
                       options=[discord.SelectOption(label=x) for x in ["Current","Former","DEP/Future Warrior"]],
                       custom_id="app:status")
    async def status_sel(self, i: discord.Interaction, sel: discord.ui.Select):
        d = session_get(i.message.id) or {}
        d["status_choice"] = sel.values[0]
        session_set(i.message.id, i.user.id, d)
        await i.response.defer()

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.success, custom_id="app:submit")
    async def submit_btn(self, i: discord.Interaction, _: discord.ui.Button):
        try:
            data = session_get(i.message.id) or {}
            missing = [k for k in ("name","pronouns","refer","branch_choice","status_choice") if not data.get(k)]
            if missing:
                await i.response.send_message("Missing: " + ", ".join(m.replace("_"," ") for m in missing), ephemeral=False)
                return

            gid = int(data.get("guild_id") or HOME_GUILD_ID or 0)
            guild = i.client.get_guild(gid) if gid else None
            if not guild:
                await i.response.send_message("Setup error: guild not found.", ephemeral=False)
                return

            staff_ch = guild.get_channel(STAFF_REVIEW_CHANNEL_ID)
            embed = discord.Embed(title="New Application", color=discord.Color.blue())
            embed.add_field(name="Preferred Name", value=data["name"], inline=False)
            embed.add_field(name="Pronouns", value=data["pronouns"], inline=False)
            embed.add_field(name="Branch", value=data["branch_choice"], inline=False)
            embed.add_field(name="Status", value=data["status_choice"], inline=False)
            embed.add_field(name="Referral Source", value=data["refer"], inline=False)
            embed.set_footer(text=f"{i.user} ({i.user.id})")

            if staff_ch:
                review_msg = await staff_ch.send(embed=embed, view=ApplicationReviewView(i.user.id, data))
                store_pending_application(review_msg.id, i.user.id, data)

            mark_as_submitted(i.user.id, discord.utils.utcnow().isoformat())
            session_del(i.message.id)

            for c in self.children:
                c.disabled = True
            await i.message.edit(view=self)
            await i.response.send_message("Submitted.", ephemeral=False)

        except Exception as e:
            # Logging/ So i can see if its silently failing -ren
            if not i.response.is_done():
                await i.response.send_message("Unexpected error submitting. Try again.", ephemeral=False)
            else:
                await i.followup.send("Unexpected error submitting. Try again.", ephemeral=False)
            raise


class ApplicationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.primary, custom_id="app:open")
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button):
        if has_submitted(interaction.user.id):
            await interaction.response.send_message("You already submitted.", ephemeral=True)
            return
        try:
            dm = await interaction.user.create_dm()
            msg = await dm.send("Let's begin your application!", view=ApplicationFormView())
            # store server id
            session_set(msg.id, interaction.user.id, {"guild_id": interaction.guild.id})
            await interaction.response.send_message("Check your DMs.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Enable DMs and try again.", ephemeral=True)

class ApplicationReviewView(discord.ui.View):
    def __init__(self, applicant_id:int, data:dict):
        super().__init__(timeout=None); self.applicant_id = applicant_id; self.data = data

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="application_approve")
    async def approve(self, i: discord.Interaction, _: discord.ui.Button):
        await self._decide(i, True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="application_deny")
    async def deny(self, i: discord.Interaction, _: discord.ui.Button):
        await self._decide(i, False)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, custom_id="application_ticket")
    async def ticket(self, i: discord.Interaction, _: discord.ui.Button):
        await self._ticket(i)

    async def _decide(self, interaction: discord.Interaction, approved: bool):
        class Reason(discord.ui.Modal, title=("Approval Reason" if approved else "Denial Reason")):
            reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph)
            async def on_submit(ms, mi: discord.Interaction):
                guild = mi.guild; member = guild.get_member(self.applicant_id)
                if not member:
                    await mi.response.send_message("User not found.", ephemeral=True); return
                if approved:
                    branch = self.data.get("branch_choice")
                    if branch:
                        role = discord.utils.get(guild.roles, name=branch)
                        if role:
                            try: await member.add_roles(role)
                            except discord.Forbidden: pass
                    verified = discord.utils.get(guild.roles, name="Verified")
                    pending = discord.utils.get(guild.roles, name="Pending Application")
                    if verified:
                        try: await member.add_roles(verified)
                        except discord.Forbidden: pass
                    if pending:
                        try: await member.remove_roles(pending)
                        except discord.Forbidden: pass
                    name = (self.data.get("name") or "").strip()
                    pronouns = (self.data.get("pronouns") or "").strip()
                    delete_pending(user_id=member.id, message_id=interaction.message.id)
                    if name and pronouns:
                        try: await member.edit(nick=f"{name} ({pronouns})")
                        except discord.Forbidden: pass
                    dm_text = f"Your application has been approved.\nReason: {ms.reason.value}"
                else:
                    dm_text = f"Your application has been denied.\nReason: {ms.reason.value}"
                try: await member.send(dm_text)
                except Exception: pass

                for c in self.children: c.disabled = True
                await mi.message.edit(view=self)
                await mi.response.send_message(
                    f"Application {'approved' if approved else 'denied'} for {member.mention}.", ephemeral=True)

                buf = io.StringIO(); w = csv.writer(buf)
                w.writerow(["Field","Value"])
                for k,v in self.data.items(): w.writerow([k.replace('_',' ').title(), v])
                w.writerow(["Decision","Approved" if approved else "Denied"])
                w.writerow(["Reason", ms.reason.value]); w.writerow(["Reviewed By", str(mi.user)])
                w.writerow(["Applicant", str(member)]); buf.seek(0)
                file = discord.File(io.BytesIO(buf.read().encode()), filename=f"{member.id}_app_log.csv")
                log = guild.get_channel(TICKET_LOG_CHANNEL_ID)
                if log: await log.send(f"Application log for {member.mention}", file=file)
                delete_pending(message_id=interaction.message.id)
        await interaction.response.send_modal(Reason())

    async def _ticket(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(self.applicant_id)
        if not member:
            await interaction.response.send_message("User not found.", ephemeral=True); return
        category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Ticket category missing.", ephemeral=True); return
        staff = discord.utils.get(interaction.guild.roles, name="Staff")
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True),
        }
        if staff: overwrites[staff] = discord.PermissionOverwrite(view_channel=True)
        ch = await interaction.guild.create_text_channel(
            name=f"ticket-{member.name}", category=category, overwrites=overwrites,
            topic=f"Application of {member.display_name}")
        await ch.send(f"{member.mention}, a staff member will assist you shortly.", view=TicketCloseView())
        await interaction.response.send_message(f"Ticket created: {ch.mention}", ephemeral=True)
        with sqlite3.connect(DB_PATH) as con:
            con.execute("INSERT OR REPLACE INTO tickets(message_id,channel_id) VALUES(?,?)",
                        (interaction.message.id, ch.id)); con.commit()

class TicketCloseView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close_button")
    async def close_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        class Close(discord.ui.Modal, title="Close Ticket"):
            reason = discord.ui.TextInput(label="Reason for closing", style=discord.TextStyle.paragraph, required=True)
            async def on_submit(ms, mi: discord.Interaction):
                messages = [m async for m in interaction.channel.history(limit=1000, oldest_first=True)]
                rows = [{"Author": f"{m.author} ({m.author.id})", "Content": m.content,
                         "Time": m.created_at.isoformat()} for m in messages if not m.author.bot]
                buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=["Author","Content","Time"])
                w.writeheader(); w.writerows(rows); buf.seek(0)
                file = discord.File(io.BytesIO(buf.read().encode()),
                                    filename=f"{str(mi.user).replace('#','_')}_ticket_log.csv")
                log = interaction.guild.get_channel(TICKET_LOG_CHANNEL_ID)
                if log: await log.send(f"Ticket closed by {mi.user.mention}\nReason: {ms.reason.value}", file=file)
                await mi.response.send_message("Ticket closed. This channel will now self destruct", ephemeral=True)
                await asyncio.sleep(2); await interaction.channel.delete()
        await interaction.response.send_modal(Close())

# Cog Setup
class Applications(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot; init_db()

    async def cog_load(self):
        self.bot.add_view(ApplicationView())
        self.bot.add_view(ApplicationFormView())
        self.bot.add_view(TicketCloseView())

    @app_commands.command(name="app_repost", description="Repost the application button")
    @app_commands.describe(channel="Target channel")
    async def app_repost(self, interaction: Interaction, channel: discord.TextChannel | None = None):
        ch = channel or interaction.channel
        await ch.send("Let's begin your application!", view=ApplicationView())
        await interaction.response.send_message("Posted.", ephemeral=True)

    @app_commands.command(name="refreshview", description="Re-attach review buttons to a staff message")
    @app_commands.describe(message_id="Staff-review message ID")
    async def refreshview(self, interaction: Interaction, message_id: str):
        try: mid = int(message_id)
        except ValueError:
            await interaction.response.send_message("Invalid message ID.", ephemeral=True); return
        ch = interaction.client.get_channel(STAFF_REVIEW_CHANNEL_ID)
        if not ch:
            await interaction.response.send_message("Channel not found.", ephemeral=True); return
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute("SELECT user_id,data FROM pending_applications WHERE message_id=?", (mid,)).fetchone()
        if not row:
            try:
                msg = await ch.fetch_message(mid)
                emb = msg.embeds[0]; data = {}
                for f in emb.fields: data[f.name.lower().replace(" ","_")] = f.value
                uid = int((emb.footer.text or "").split("(")[-1].rstrip(")"))
                store_pending_application(mid, uid, data); row = (uid, json.dumps(data))
            except Exception as e:
                await interaction.response.send_message(f"Could not backfill: {e}", ephemeral=True); return
        uid, data_json = row
        await interaction.response.send_message("View refreshed.", ephemeral=True)
        try:
            msg = await ch.fetch_message(mid)
            await msg.edit(view=ApplicationReviewView(uid, json.loads(data_json)))
        except Exception: pass

    @app_commands.command(name="list_pending", description="List pending application message IDs")
    async def list_pending(self, interaction: Interaction):
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute("SELECT message_id,user_id FROM pending_applications ORDER BY message_id DESC").fetchall()
        if not rows:
            await interaction.response.send_message("No pending applications.", ephemeral=True); return
        await interaction.response.send_message("\n".join(f"{m} — {u}" for m,u in rows[:50]), ephemeral=True)

    @app_commands.command(name="remove_pending", description="Remove a pending application by message ID")
    @app_commands.describe(message_id="Staff-review message ID")
    async def remove_pending(self, interaction: Interaction, message_id: str):
        try: mid = int(message_id)
        except ValueError:
            await interaction.response.send_message("Invalid message ID.", ephemeral=True); return
        delete_pending(message_id=mid)
        await interaction.response.send_message("Removed.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Applications(bot))