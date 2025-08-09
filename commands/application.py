import discord
from discord.ext import commands
from discord.utils import get
from discord import app_commands, Interaction, Member
from dotenv import load_dotenv
import os
import asyncio
import io
import csv
import sqlite3
import json
from pathlib import Path

load_dotenv()
BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = str(Path(os.getenv("APPLICATIONS_DB") or (BASE_DIR / "applications.db")))
STAFF_REVIEW_CHANNEL_ID = int(os.getenv("STAFF_REVIEW_CHANNEL_ID", "0"))
submitted_applications = set()

load_dotenv()
STAFF_REVIEW_CHANNEL_ID = int(os.getenv("STAFF_REVIEW_CHANNEL_ID", "0"))


def delete_pending(*, user_id: int | None = None, message_id: int | None = None) -> None:
    if not user_id and not message_id:
        return
    with sqlite3.connect(DB_PATH) as con:
        if user_id:
            con.execute("DELETE FROM pending_applications WHERE user_id = ?", (user_id,))
        if message_id:
            con.execute("DELETE FROM pending_applications WHERE message_id = ?", (message_id,))
        con.commit()

def store_pending_application(message_id: int, user_id: int, data: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS pending_applications (
            message_id TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            data       TEXT    NOT NULL
        )
    """)
    c.execute(
        "INSERT OR REPLACE INTO pending_applications (message_id, user_id, data) VALUES (?, ?, ?)",
        (str(message_id), user_id, json.dumps(data))
    )
    conn.commit()
    conn.close()

def has_pending_application(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM applications WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            user_id    INTEGER PRIMARY KEY,
            submitted_at TEXT,
            status     TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS pending_applications (
            message_id INTEGER PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            data       TEXT    NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def has_submitted(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM applications WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_as_submitted(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO applications (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def clear_submission(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM applications WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
class ApplicationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Join the TMH Family!", style=discord.ButtonStyle.primary, custom_id="application_button")
    async def open_application(self, interaction: discord.Interaction, button: discord.ui.Button):
        if has_submitted(interaction.user.id):
            await interaction.response.send_message("You have already submitted an application. Please wait for a staff member to review it.", ephemeral=True)
            return

        try:
            await interaction.user.send("Let's begin your application!", view=SinglePageApplication({}))
            await interaction.response.send_message("Check your DMs to start the application!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I couldn't DM you. Please enable messages from server members and try again.", ephemeral=True)

class SinglePageApplication(discord.ui.View):
    def __init__(self, responses):
        super().__init__(timeout=None)
        self.responses = responses
        self.add_item(ApplicationTextInput("1. Preferred Name", "name", "First names only", required=True, row=0))
        self.add_item(ApplicationTextInput("2. Pronouns", "pronouns", "she/her, he/him, etc.", required=True, row=0))
        self.add_item(ApplicationTextInput("3. Referral Source", "refer", "Where did you hear about us?", required=True, row=0))
        self.add_item(BranchDropdown(responses))
        self.add_item(StatusDropdown(responses))
        self.add_item(ApplicationSubmitButton(responses))

class ApplicationTextInput(discord.ui.Button):
    def __init__(self, label, custom_id, placeholder, required=False, row=None):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)
        self.custom_id = custom_id
        self.placeholder = placeholder
        self.required = required

    async def callback(self, interaction: discord.Interaction):
        class ResponseModal(discord.ui.Modal, title=self.label):
            input = discord.ui.TextInput(label=self.placeholder, style=discord.TextStyle.paragraph, required=self.required)

            async def on_submit(modal_self, modal_interaction: discord.Interaction):
                self.view.responses[self.custom_id] = modal_self.input.value
                await modal_interaction.response.send_message(f"{self.label} saved.", ephemeral=True)

        await interaction.response.send_modal(ResponseModal())

class BranchDropdown(discord.ui.Select):
    def __init__(self, responses):
        self.responses = responses
        options = [discord.SelectOption(label=b) for b in ["Army","Navy","Marines","Air Force","Coast Guard","Space Force","Family"]]
        super().__init__(placeholder="Select your branch", options=options, row=1)

class StatusDropdown(discord.ui.Select):
    def __init__(self, responses):
        self.responses = responses
        options = [discord.SelectOption(label=s) for s in ["Current","Former","DEP/Future Warrior"]]
        super().__init__(placeholder="Select your status", options=options, row=2)

    async def callback(self, interaction: discord.Interaction):
        self.view.responses["status_choice"] = self.values[0]
        await interaction.response.defer()

class NextButton(discord.ui.Button):
    def __init__(self, label, next_page, responses):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.next_page = next_page
        self.responses = responses

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=self.next_page(self.responses))

class BackButton(discord.ui.Button):
    def __init__(self, label, prev_page, responses):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.prev_page = prev_page
        self.responses = responses

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=self.prev_page(self.responses))

class ApplicationSubmitButton(discord.ui.Button):
    def __init__(self, responses):
        super().__init__(label="Submit", style=discord.ButtonStyle.success, row=3)
        self.responses = responses

    async def callback(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_applications (
                message_id INTEGER PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                data       TEXT    NOT NULL
            )
        """)

        c.execute("SELECT status FROM applications WHERE user_id = ?", (interaction.user.id,))
        row = c.fetchone()
        if row and row[0] in ("pending", "approved"):
            await interaction.response.send_message(
                "You already have an application under review or approved. Please wait for staff to process it.",
                ephemeral=True
            )
            conn.close()
            return

        guild_id = int(os.getenv("GUILD_ID"))
        staff_channel_id = int(os.getenv("STAFF_REVIEW_CHANNEL_ID"))

        guild = interaction.client.get_guild(guild_id)
        staff_channel = guild.get_channel(staff_channel_id) if guild else None

        embed = discord.Embed(title="New Application", color=discord.Color.blue())
        for key, value in self.responses.items():
            embed.add_field(name=key.replace("_", " ").title(), value=value, inline=False)
        embed.set_footer(text=f"Applicant: {interaction.user} ({interaction.user.id})")

        if staff_channel:
            staff_role = discord.utils.get(guild.roles, name="Staff")
            staff_ping = staff_role.mention if staff_role else ""
            review_msg = await staff_channel.send(
                content=f"{staff_ping}",
                embed=embed,
                view=ApplicationReviewView(interaction.user.id, self.responses)
            )
            store_pending_application(review_msg.id, interaction.user.id, self.responses)
        await interaction.response.send_message(
            "Your application has been submitted! A staff member will review it shortly.",
            ephemeral=True
        )
        c.execute(
            "INSERT OR REPLACE INTO applications (user_id, submitted_at, status) VALUES (?, ?, ?)",
            (
                interaction.user.id,
                discord.utils.utcnow().isoformat(),
                "pending"
            )
        )
        conn.commit()
        conn.close()
class ApplicationReviewView(discord.ui.View):
    def __init__(self, applicant_id: int, application_data: dict):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.application_data = application_data

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="application_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, approved=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="application_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, approved=False)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, custom_id="application_ticket")
    async def ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_ticket(interaction, button)

    async def handle_response(self, interaction: discord.Interaction, approved: bool):
        modal_title = "Approval Reason" if approved else "Denial Reason"

        class ReasonModal(discord.ui.Modal, title=modal_title):
            reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph)

            async def on_submit(modal_self, modal_interaction: discord.Interaction):
                guild = interaction.guild
                member = guild.get_member(self.applicant_id)
                if not member:
                    return await modal_interaction.response.send_message("User not found.", ephemeral=True)

                if approved:
                    branch = self.application_data.get("branch_choice")
                    if branch:
                        role = discord.utils.get(guild.roles, name=branch)
                        if role:
                            await member.add_roles(role)

                    verified = discord.utils.get(guild.roles, name="Verified")
                    pending  = discord.utils.get(guild.roles, name="Pending Application")
                    if verified:
                        await member.add_roles(verified)
                    if pending:
                        await member.remove_roles(pending)

                    name     = self.application_data.get("name","").strip()
                    pronouns = self.application_data.get("pronouns","").strip()
                    delete_pending(user_id=member.id, message_id=interaction.message.id)
                    if name and pronouns:
                        try:
                            await member.edit(nick=f"{name} ({pronouns})")
                        except discord.Forbidden:
                            pass

                    dm_msg = f"Your application has been **approved**!\nReason: {modal_self.reason.value}"
                else:
                    dm_msg = f"Your application has been **denied**.\nReason: {modal_self.reason.value}"

                try:
                    await member.send(dm_msg)
                except:
                    pass

                for child in self.children:
                    child.disabled = True
                await modal_interaction.message.edit(view=self)

                await modal_interaction.response.send_message(
                    f"Application {'approved' if approved else 'denied'} for {member.mention}.", ephemeral=True
                )
                with sqlite3.connect(DB_PATH) as con:
                    con.execute(
                        "UPDATE applications SET status = ? WHERE user_id = ?",
                        ("approved" if approved else "denied", member.id)
                    )
                    con.execute(
                        "DELETE FROM pending_applications WHERE message_id = ?",
                        (interaction.message.id,)
                    )
                    con.commit()
                for child in self.children:
                    child.disabled = True
                await interaction.message.edit(view=self)

                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["Field","Value"])
                for k,v in self.application_data.items():
                    w.writerow([k.replace("_"," ").title(), v])
                w.writerow(["Decision", "Approved" if approved else "Denied"])
                w.writerow(["Reason", modal_self.reason.value])
                w.writerow(["Reviewed By", str(modal_interaction.user)])
                w.writerow(["Applicant", str(member)])
                buf.seek(0)

                csv_file = discord.File(io.BytesIO(buf.read().encode()), filename=f"{member.id}_app_log.csv")
                log_ch = guild.get_channel(int(os.getenv("TICKET_LOG_CHANNEL_ID", "0")))
                if log_ch:
                    await log_ch.send(
                        f"Application log for {member.mention} — {'Approved' if approved else 'Denied'} by {modal_interaction.user.mention}",
                        file=csv_file
                    )

                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute(
                    "DELETE FROM pending_applications WHERE message_id = ?",
                    (interaction.message.id,)
                )
                conn.commit()
                conn.close()

        await interaction.response.send_modal(ReasonModal())

    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.guild.get_member(self.applicant_id)
        if not member:
            await interaction.response.send_message("User not found.", ephemeral=True)
            return

        category_id = int(os.getenv("TICKET_CATEGORY_ID"))
        category = interaction.guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Ticket section is missing!", ephemeral=True)
            return

        staff_role = discord.utils.get(interaction.guild.roles, name="Staff")
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True)

        ticket_channel = await interaction.guild.create_text_channel(
            name=f"ticket-{member.name}",
            category=category,
            overwrites=overwrites,
            topic=f"Application of {member.display_name}"
        )

        await ticket_channel.send(
            f"{member.mention}, a staff member will assist you shortly.",
            view=TicketCloseView()
        )

        await interaction.response.send_message(f"Ticket created: {ticket_channel.mention}", ephemeral=True)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO tickets (message_id, channel_id) VALUES (?, ?)",
            (interaction.message.id, ticket_channel.id)
        )
        conn.commit()
        conn.close()

class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_close_button")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        class CloseModal(discord.ui.Modal, title="Close Ticket"):
            reason = discord.ui.TextInput(label="Reason for closing", style=discord.TextStyle.paragraph, required=True)

            async def on_submit(modal_self, modal_interaction: discord.Interaction):
                messages = [msg async for msg in interaction.channel.history(limit=1000, oldest_first=True)]
                transcript_data = [{
                    "Author": f"{msg.author} ({msg.author.id})",
                    "Content": msg.content,
                    "Time": msg.created_at.isoformat()
                } for msg in messages if not msg.author.bot]

                import csv, io
                buffer = io.StringIO()
                writer = csv.DictWriter(buffer, fieldnames=["Author", "Content", "Time"])
                writer.writeheader()
                writer.writerows(transcript_data)
                buffer.seek(0)

                member_name = str(modal_interaction.user).replace("#", "_")
                filename = f"{member_name}_ticket_log.csv"
                csv_file = discord.File(io.BytesIO(buffer.read().encode()), filename=filename)

                log_channel_id = int(os.getenv("TICKET_LOG_CHANNEL_ID"))
                log_channel = interaction.guild.get_channel(log_channel_id)
                if log_channel:
                    await log_channel.send(
                        f"Ticket closed by {modal_interaction.user.mention}\n**Reason:** {modal_self.reason.value}",
                        file=csv_file
                    )

                await modal_interaction.response.send_message("Ticket closed. This channel will now self destruct", ephemeral=True)
                await asyncio.sleep(2)
                await interaction.channel.delete()

        await interaction.response.send_modal(CloseModal())

@app_commands.command(
    name="refreshview",
    description="Re-attach the review buttons to an application message"
)
@app_commands.describe(
    message_id="ID of the staff-review message to refresh"
)
async def refreshview(interaction: Interaction, message_id: str):
    try:
        msg_id = int(message_id)
    except ValueError:
        return await interaction.response.send_message(
            "Invalid message ID.", ephemeral=True
        )
    staff_ch_id = int(os.getenv("STAFF_REVIEW_CHANNEL_ID"))
    staff_ch    = interaction.client.get_channel(staff_ch_id)
    if not staff_ch:
        return await interaction.response.send_message(
            "Channel Not Found.", ephemeral=True
        )

    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute(
      "SELECT user_id, data FROM pending_applications WHERE message_id = ?",
      (msg_id,)
    )
    row = c.fetchone()
    conn.close()

    if not row:
        try:
            msg = await staff_ch.fetch_message(msg_id)
            emb = msg.embeds[0]
            data = {}
            for f in emb.fields:
                data[f.name.lower().replace(" ", "_")] = f.value

            footer = emb.footer.text or ""
            uid = int(footer.split("(")[-1].rstrip(")"))

            conn = sqlite3.connect(DB_PATH)
            c    = conn.cursor()
            c.execute(
              "INSERT OR REPLACE INTO pending_applications(message_id,user_id,data) VALUES(?,?,?)",
              (msg_id, uid, json.dumps(data))
            )
            conn.commit()
            conn.close()

            row = (uid, json.dumps(data))
        except Exception as e:
            return await interaction.response.send_message(
                f"Could not backfill legacy data: {e}", ephemeral=True
            )


    applicant_id, data_json = row
    application_data       = json.loads(data_json)
    msg                    = await staff_ch.fetch_message(msg_id)
    view                   = ApplicationReviewView(applicant_id, application_data)

    try:
        await msg.edit(view=view)
        interaction.client.add_view(view, message_id=msg_id)
        await interaction.response.send_message(
            f"Re-attached buttons to message {msg_id}", ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"Failed to refresh view: {e}", ephemeral=True
        )


@app_commands.command(
    name="list_pending",
    description="List all users with pending applications"
)
async def list_pending(interaction: Interaction):
    if not any(role.name == "Staff" for role in interaction.user.roles):
        return await interaction.response.send_message(
            "You don't have permission to do that.", ephemeral=True
        )

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM applications WHERE status = 'pending'")
    rows = c.fetchall()
    conn.close()

    if not rows:
        return await interaction.response.send_message(
            "There are no pending applications.", ephemeral=True
        )

    lines = []
    for (uid,) in rows:
        member = interaction.guild.get_member(uid)
        if member:
            lines.append(f"• {member}")
        else:
            try:
                user = await interaction.client.fetch_user(uid)
                lines.append(f"• {user} (not in guild)")
            except:
                lines.append(f"• {uid} (unknown user)")

    chunk = "\n".join(lines)
    await interaction.response.send_message(
        f"**Pending applications:**\n{chunk}", ephemeral=True
    )

@app_commands.command(
    name="remove_pending",
    description="Remove a user's pending application"
)
@app_commands.describe(
    user="The user whose pending application you want to remove"
)
async def remove_pending(interaction: Interaction, user: Member):
    if not any(role.name == "Staff" for role in interaction.user.roles):
        return await interaction.response.send_message(
            "You don't have permission to do that.", ephemeral=True
        )

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "DELETE FROM applications WHERE user_id = ? AND status = 'pending'",
        (user.id,)
    )
    deleted = c.rowcount
    conn.commit()
    conn.close()

    if deleted:
        await interaction.response.send_message(
            f"Removed pending application for {user.mention}.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"No pending application found for {user.mention}.", ephemeral=True
        )

def setup(tree: app_commands.CommandTree):
    tree.add_command(refreshview)
    tree.add_command(list_pending)
    tree.add_command(remove_pending)