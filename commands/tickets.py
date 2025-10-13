import discord
from discord import app_commands, Interaction, TextStyle
from discord.ext import commands
from discord.ui import View, Button, Modal, TextInput, Select
import sqlite3
import os
import io
import csv
import asyncio
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

LOG_CHANNEL_ID = int(os.getenv("TICKET_LOG_CHANNEL_ID", "0"))
WAITING_CHANNEL_ID = int(os.getenv("TICKET_WAITING_CHANNEL_ID", "0"))

TICKET_CATEGORIES = {
    "staff": {"category_name": "Staff Tickets", "role_name": "Staff"},
    "ember": {"category_name": "Ember Tickets", "role_name": "Ember"},
    "medic": {"category_name": "Medic Tickets", "role_name": "Medic"},
}

ROLE_ACCESS = {
    "Staff": ["staff", "ember", "medic"],
    "Ember": ["ember"],
    "Medic": ["medic"],
}

ALLOWED_CLOSE_ROLES = ["Admin", "Moderator", "Staff"]

DB_PATH = "tickets.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            ticket_type TEXT,
            title TEXT,
            description TEXT,
            status TEXT,
            claimer_id INTEGER,
            channel_id INTEGER,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def get_allowed_types(member: discord.Member):
    names = {r.name for r in member.roles}
    allowed = set()
    for role, types in ROLE_ACCESS.items():
        if role in names:
            allowed.update(types)
    return allowed


class TicketSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(TicketPanelView())

    @app_commands.command(name="setup_ticket_panel", description="Post the ticket creation panel.")
    async def setup_ticket_panel(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return
        view = TicketPanelView()
        embed = discord.Embed(
            title="Create a Ticket",
            description="Click below to submit a ticket request. A team member will claim it.",
            color=discord.Color.blurple(),
        )
        await interaction.channel.send(embed=embed, view=view)
        await interaction.followup.send("Panel posted.", ephemeral=True)

    @app_commands.command(name="viewtickets", description="View unclaimed tickets you can claim.")
    async def viewtickets(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Use this in a server.", ephemeral=True)
            return
        allowed = get_allowed_types(interaction.user)
        if not allowed:
            await interaction.followup.send("You are not allowed to view tickets.", ephemeral=True)
            return
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        placeholders = ",".join("?" for _ in allowed)
        c.execute(
            f"SELECT id, ticket_type, title, user_id, created_at FROM tickets WHERE status='OPEN' AND ticket_type IN ({placeholders}) ORDER BY id ASC",
            tuple(allowed),
        )
        rows = c.fetchall()
        conn.close()
        if not rows:
            await interaction.followup.send("No open tickets you can claim.", ephemeral=True)
            return
        options = []
        for tid, ttype, title, uid, created in rows:
            label = f"#{tid} {ttype.title()}"
            desc = f"{title[:90]}" if title else ""
            options.append(discord.SelectOption(label=label, value=str(tid), description=desc))
        view = TicketQueueView(options)
        await interaction.followup.send("Select a ticket to claim:", view=view, ephemeral=True)

    @app_commands.command(name="claimticket", description="Claim an open ticket by ID.")
    @app_commands.describe(ticket_id="The ID of the ticket to claim")
    async def claimticket(self, interaction: Interaction, ticket_id: int):
        await self._claim_by_id(interaction, ticket_id)

    async def _claim_by_id(self, interaction: Interaction, ticket_id: int):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Use this in a server.", ephemeral=True)
            return
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT user_id, ticket_type, title, description, status FROM tickets WHERE id=?", (ticket_id,))
        row = c.fetchone()
        if not row:
            await interaction.followup.send("Ticket not found.", ephemeral=True)
            conn.close()
            return
        user_id, ticket_type, title, desc, status = row
        if status != "OPEN":
            await interaction.followup.send("That ticket is already claimed or closed.", ephemeral=True)
            conn.close()
            return
        allowed = get_allowed_types(interaction.user)
        if ticket_type not in allowed:
            await interaction.followup.send("You are not allowed to claim this ticket type.", ephemeral=True)
            conn.close()
            return
        member = interaction.guild.get_member(user_id)
        cfg = TICKET_CATEGORIES[ticket_type]
        category = discord.utils.get(interaction.guild.categories, name=cfg["category_name"])
        if not category:
            category = await interaction.guild.create_category(cfg["category_name"])
        role = discord.utils.get(interaction.guild.roles, name=cfg["role_name"])
        base_name = member.name if member else f"user-{user_id}"
        channel_name = f"{ticket_type}-{base_name}-{ticket_id}".replace(" ", "-").lower()
        channel = await interaction.guild.create_text_channel(channel_name, category=category)
        if member:
            await channel.set_permissions(member, view_channel=True, send_messages=True)
        if role:
            await channel.set_permissions(role, view_channel=True, send_messages=True)
        await channel.set_permissions(interaction.guild.default_role, view_channel=False)
        await channel.set_permissions(interaction.user, view_channel=True, send_messages=True)
        embed = discord.Embed(title=title or "Ticket", description=desc or "", color=discord.Color.blurple(), timestamp=datetime.now())
        embed.add_field(name="Ticket Type", value=ticket_type.title(), inline=True)
        embed.add_field(name="Ticket ID", value=str(ticket_id), inline=True)
        if member:
            embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        view = TicketCloseView(ticket_type=ticket_type, opener=member)
        msg_intro = f"{interaction.user.mention} claimed this ticket"
        if member:
            msg_intro += f" for {member.mention}"
        await channel.send(msg_intro + ".", embed=embed, view=view)
        await interaction.followup.send(f"Ticket #{ticket_id} claimed. {channel.mention}", ephemeral=True)
        c.execute("UPDATE tickets SET status='CLAIMED', claimer_id=?, channel_id=? WHERE id=?", (interaction.user.id, channel.id, ticket_id))
        conn.commit()
        conn.close()


class TicketQueueView(View):
    def __init__(self, options):
        super().__init__(timeout=60)
        self.selector = TicketQueueSelect(options)
        self.add_item(self.selector)
        self.add_item(TicketClaimButton())

    async def claim_selected(self, interaction: Interaction):
        if not self.selector.values:
            await interaction.response.send_message("Select a ticket first.", ephemeral=True)
            return
        ticket_id = int(self.selector.values[0])
        cog: TicketSystem = interaction.client.get_cog("TicketSystem")
        await cog._claim_by_id(interaction, ticket_id)


class TicketQueueSelect(Select):
    def __init__(self, options):
        super().__init__(placeholder="Choose a ticket", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)


class TicketClaimButton(Button):
    def __init__(self):
        super().__init__(label="Claim Selected", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: Interaction):
        parent: TicketQueueView = self.view  # type: ignore
        await parent.claim_selected(interaction)


class TicketSelectDropdown(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Staff Ticket", value="staff"),
            discord.SelectOption(label="Ember Ticket", value="ember"),
            discord.SelectOption(label="Medic Ticket", value="medic"),
        ]
        super().__init__(placeholder="Select ticket type...", options=options)

    async def callback(self, interaction: Interaction):
        modal = TicketModal(ticket_type=self.values[0])
        await interaction.response.send_modal(modal)


class TicketButton(Button):
    def __init__(self):
        super().__init__(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="create_ticket_btn")

    async def callback(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        view = View()
        view.add_item(TicketSelectDropdown())
        await interaction.followup.send("Select your ticket type:", view=view, ephemeral=True)


class TicketPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketButton())


class TicketModal(Modal, title="New Ticket"):
    def __init__(self, ticket_type: str):
        super().__init__(timeout=None)
        self.ticket_type = ticket_type
        self.title_input = TextInput(label="Title", placeholder="Enter a short title", max_length=100)
        self.desc_input = TextInput(label="Description", style=TextStyle.paragraph, placeholder="Describe your issue", max_length=2000)
        self.add_item(self.title_input)
        self.add_item(self.desc_input)

    async def on_submit(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO tickets (user_id, ticket_type, title, description, status, created_at) VALUES (?, ?, ?, ?, 'OPEN', ?)",
            (interaction.user.id, self.ticket_type, self.title_input.value, self.desc_input.value, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
        conn.close()
        await interaction.followup.send("Your ticket has been submitted to the queue.", ephemeral=True)
        if WAITING_CHANNEL_ID and interaction.guild:
            channel = interaction.client.get_channel(WAITING_CHANNEL_ID)
            cfg = TICKET_CATEGORIES.get(self.ticket_type)
            role = discord.utils.get(interaction.guild.roles, name=cfg["role_name"]) if cfg else None
            if channel:
                embed = discord.Embed(
                    title="New Ticket Submitted",
                    description=f"A new {self.ticket_type.title()} ticket has been added to the queue.",
                    color=discord.Color.blurple(),
                    timestamp=datetime.now(),
                )
                mention = role.mention if role else f"`{cfg['role_name']}`" if cfg else ""
                await channel.send(content=f"{mention} A new ticket is waiting.", embed=embed)


class TicketCloseButton(Button):
    def __init__(self, ticket_type: str, opener: discord.User | None):
        super().__init__(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket_btn")
        self.ticket_type = ticket_type
        self.opener = opener

    async def callback(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        if not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Use this in a server.", ephemeral=True)
            return
        if not any(r.name in ALLOWED_CLOSE_ROLES for r in interaction.user.roles):
            await interaction.followup.send("You cannot close this ticket.", ephemeral=True)
            return
        channel = interaction.channel
        closer = interaction.user
        messages = [m async for m in channel.history(limit=None, oldest_first=True)]
        log_buffer = io.StringIO()
        writer = csv.writer(log_buffer)
        writer.writerow(["Timestamp", "Author", "Message", "Attachments"])
        for m in messages:
            text = m.clean_content.replace("\n", " ") if m.clean_content else ""
            attachments = ", ".join(a.url for a in m.attachments) if m.attachments else ""
            writer.writerow([m.created_at.isoformat(), m.author.display_name, text, attachments])
        log_buffer.seek(0)
        csv_data = log_buffer.getvalue().encode()
        csv_file = discord.File(io.BytesIO(csv_data), filename=f"{channel.name}.csv")
        if LOG_CHANNEL_ID:
            log_channel = interaction.client.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                embed = discord.Embed(title=f"Ticket Closed: {channel.name}", color=discord.Color.red(), timestamp=datetime.now())
                embed.add_field(name="Type", value=self.ticket_type.title(), inline=True)
                if self.opener:
                    embed.add_field(name="Opened by", value=self.opener.mention, inline=True)
                embed.add_field(name="Closed by", value=closer.mention, inline=True)
                await log_channel.send(embed=embed, file=csv_file)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE tickets SET status='CLOSED' WHERE channel_id=?", (channel.id,))
        conn.commit()
        conn.close()
        await channel.send(f"Ticket closed by {closer.mention}. Channel deleting in 5s.")
        await asyncio.sleep(5)
        await channel.delete()


class TicketCloseView(View):
    def __init__(self, ticket_type: str, opener: discord.User | None):
        super().__init__(timeout=None)
        self.add_item(TicketCloseButton(ticket_type, opener))


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketSystem(bot))
