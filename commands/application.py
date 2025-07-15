import discord
from discord.ext import commands
from discord.utils import get
from discord import app_commands
import os
import asyncio
import io
import csv
import sqlite3


submitted_applications = set()

def has_pending_application(user_id: int) -> bool:
    conn = sqlite3.connect("applications.db")
    c = conn.cursor()
    c.execute("SELECT * FROM applications WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def init_db():
    conn = sqlite3.connect("applications.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            user_id INTEGER PRIMARY KEY,
            submitted_at TEXT,
            status TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def has_submitted(user_id: int) -> bool:
    conn = sqlite3.connect("applications.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM applications WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_as_submitted(user_id: int):
    conn = sqlite3.connect("applications.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO applications (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def clear_submission(user_id: int):
    conn = sqlite3.connect("applications.db")
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
        self.add_item(ApplicationTextInput("1. Preferred Name", "name", "First names only", required=True))
        self.add_item(ApplicationTextInput("2. Pronouns", "pronouns", "she/her, he/him, etc.", required=True))
        self.add_item(BranchDropdown(responses))
        self.add_item(StatusDropdown(responses))
        self.add_item(ApplicationTextInput("3. Referral Source", "refer", "Where did you hear about us?", required=True))
        self.add_item(ApplicationSubmitButton(responses))


class ApplicationTextInput(discord.ui.Button):
    def __init__(self, label, custom_id, placeholder, required=False):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
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
        options = [discord.SelectOption(label=b) for b in ["Army", "Navy", "Marines", "Air Force", "Coast Guard", "Space Force"]]
        super().__init__(placeholder="Select your branch", options=options)

    async def callback(self, interaction: discord.Interaction):
        self.view.responses["branch_choice"] = self.values[0]
        await interaction.response.defer()

class StatusDropdown(discord.ui.Select):
    def __init__(self, responses):
        self.responses = responses
        options = [discord.SelectOption(label=s) for s in ["Current", "Former", "DEP/Future Warrior"]]
        super().__init__(placeholder="Select your status", options=options)

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
        super().__init__(label="Submit", style=discord.ButtonStyle.success)
        self.responses = responses

    async def callback(self, interaction: discord.Interaction):
        conn = sqlite3.connect("applications.db")
        c = conn.cursor()

        c.execute("SELECT status FROM applications WHERE user_id = ?", (interaction.user.id,))
        row = c.fetchone()
        if row and row[0] in ("pending", "approved"):
            await interaction.response.send_message("You already have a submitted application that is still under review or approved. Please wait for staff to process it.", ephemeral=True)
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
            await staff_channel.send(embed=embed, view=ApplicationReviewView(interaction.user.id, self.responses))

        await interaction.response.send_message("Your application has been submitted!", ephemeral=True)

        c.execute("INSERT OR REPLACE INTO applications (user_id, submitted_at, status) VALUES (?, ?, ?)", 
                  (interaction.user.id, discord.utils.utcnow().isoformat(), 'pending'))
        conn.commit()
        conn.close()


class ApplicationReviewView(discord.ui.View):
    def __init__(self, applicant_id, application_data):
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
        await self.open_ticket(interaction)

    async def handle_response(self, interaction: discord.Interaction, approved: bool):
        modal_title = "Approval Reason" if approved else "Denial Reason"
        custom_id = "approve_reason" if approved else "deny_reason"

        class ReasonModal(discord.ui.Modal, title=modal_title):
            reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph)

            async def on_submit(modal_self, modal_interaction: discord.Interaction):
                member = interaction.guild.get_member(self.applicant_id)
                if not member:
                    await modal_interaction.response.send_message("User not found.", ephemeral=True)
                    return

                if approved:
                    branch = self.application_data.get("branch_choice")
                    if branch:
                        role = discord.utils.get(interaction.guild.roles, name=branch)
                        if role:
                            await member.add_roles(role)
                    msg = f"Your application has been approved!\n\n**Reason:** {modal_self.reason.value}"
                else:
                    msg = f"Your application has been denied.\n\n**Reason:** {modal_self.reason.value}"

                try:
                    await member.send(msg)
                    for child in self.children:
                        child.disabled = True
                        await modal_interaction.message.edit(view=self)
                    await modal_interaction.response.send_message(f"{'Approved' if approved else 'Denied'} the application and notified the applicant.", ephemeral=True)
                    csv_buffer = io.StringIO()
                    writer = csv.writer(csv_buffer)
                    writer.writerow(["Field", "Value"])
                    for key, value in self.application_data.items():
                        writer.writerow([key.replace("_", " ").title(), value])
                    writer.writerow(["Decision", "Approved" if approved else "Denied"])
                    writer.writerow(["Reason", modal_self.reason.value])
                    writer.writerow(["Reviewed By", str(modal_interaction.user)])
                    writer.writerow(["Applicant", str(member)])

                    csv_buffer.seek(0)
                    username = str(member).replace("#", "_")
                    filename = f"{username}_application_log.csv"
                    csv_file = discord.File(io.BytesIO(csv_buffer.read().encode()), filename=filename)

                    log_channel_id = int(os.getenv("TICKET_LOG_CHANNEL_ID"))
                    log_channel = interaction.guild.get_channel(log_channel_id)
                    if log_channel:
                        await log_channel.send(
                            f"Application log for {member.mention} ({'Approved' if approved else 'Denied'} by {modal_interaction.user.mention})",
                            file=csv_file
                        )
                    conn = sqlite3.connect("applications.db")
                    c = conn.cursor()
                    new_status = "approved" if approved else "denied"
                    c.execute("INSERT OR REPLACE INTO applications (user_id, status) VALUES (?, ?)", (self.applicant_id, new_status))
                    conn.commit()
                    conn.close()

                except:
                    await modal_interaction.response.send_message("Failed to DM the user, but decision was recorded.", ephemeral=True)

        await interaction.response.send_modal(ReasonModal())

    async def open_ticket(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(self.applicant_id)
        if not member:
            await interaction.response.send_message("User not found.", ephemeral=True)
            return

        category_id = int(os.getenv("TICKET_CATEGORY_ID"))
        category = interaction.guild.get_channel(category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
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

class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger)
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