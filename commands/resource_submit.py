"""Community resource submissions -> Airtable, with staff approval.

Flow:
    /resource panel          posts a submit button into a channel
    member presses it        fills a 5 field modal
    staff review card        posted to the review channel with a table picker
    staff pick a table       then Approve, and the record is created in Airtable

WHY THE FORM IS GENERIC
Discord modals allow at most 5 text inputs, while the Airtable tables carry 8
to 10 fields each, and the "name" column differs per table (Shelter Name,
Organization Name, Clinic Name, Center Name, Organization...). National Help
has only three fields and no address or phone at all.

So the modal collects a common core, and on approval the submission is mapped
onto whatever fields the chosen table actually has - anything the table does
not define is simply not sent. That keeps one form working across all eight
tables and means adding a column in Airtable does not break anything here.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import aiohttp
import discord
from discord import Interaction, app_commands
from discord.ext import commands
from dotenv import load_dotenv

from utils.resource_clean import (
    normalise, resolve_location, split_contact, looks_like_prose,
)

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger(__name__)

DB_PATH = str(Path(os.getenv("RESOURCE_DB") or (BASE_DIR / "data" / "resources.db")))
TOKEN = os.getenv("AIRTABLE_TOKEN", "")
BASE_ID = os.getenv("AIRTABLE_BASE_ID", "")
REVIEW_CHANNEL_ID = int(os.getenv("RESOURCE_REVIEW_CHANNEL_ID")
                        or os.getenv("STAFF_REVIEW_CHANNEL_ID", "0") or 0)

META_URL = "https://api.airtable.com/v0/meta/bases/{base}/tables"
RECORDS_URL = "https://api.airtable.com/v0/{base}/{table}"
URL_RE = re.compile(r"https?://\S+|(?:www\.)\S+\.\S+")

_schema_cache: list[dict] = []
_schema_lock = asyncio.Lock()


@contextmanager
def _connect(path):
    con = sqlite3.connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


def ensure_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect(DB_PATH) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS resource_panels(
            message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL, created_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS resource_submissions(
            message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
            submitter_id INTEGER NOT NULL, data TEXT NOT NULL,
            table_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL)""")
        con.commit()


def save_panel(message_id: int, guild_id: int, channel_id: int) -> None:
    with _connect(DB_PATH) as con:
        con.execute("INSERT OR REPLACE INTO resource_panels VALUES(?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                    (int(message_id), int(guild_id), int(channel_id)))
        con.commit()


def panel_exists(message_id: int) -> bool:
    with _connect(DB_PATH) as con:
        return con.execute("SELECT 1 FROM resource_panels WHERE message_id=?",
                           (int(message_id),)).fetchone() is not None


def save_submission(message_id: int, guild_id: int, submitter_id: int, data: dict) -> None:
    with _connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO resource_submissions"
            "(message_id,guild_id,submitter_id,data,table_id,status,created_at) "
            "VALUES(?,?,?,?,NULL,'pending',strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (int(message_id), int(guild_id), int(submitter_id), json.dumps(data)))
        con.commit()


def get_submission(message_id: int) -> dict | None:
    with _connect(DB_PATH) as con:
        row = con.execute("SELECT message_id,guild_id,submitter_id,data,table_id,status "
                          "FROM resource_submissions WHERE message_id=?",
                          (int(message_id),)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[3])
    except Exception:
        data = {}
    return {"message_id": row[0], "guild_id": row[1], "submitter_id": row[2],
            "data": data, "table_id": row[4], "status": row[5]}


def set_submission_table(message_id: int, table_id: str) -> None:
    with _connect(DB_PATH) as con:
        con.execute("UPDATE resource_submissions SET table_id=? WHERE message_id=?",
                    (table_id, int(message_id)))
        con.commit()


def set_submission_status(message_id: int, status: str) -> None:
    with _connect(DB_PATH) as con:
        con.execute("UPDATE resource_submissions SET status=? WHERE message_id=?",
                    (status, int(message_id)))
        con.commit()


def configuration_problem() -> str | None:
    if not TOKEN:
        return "`AIRTABLE_TOKEN` isn't set in the bot's .env."
    if not BASE_ID:
        return "`AIRTABLE_BASE_ID` isn't set in the bot's .env."
    return None


async def get_schema(force: bool = False) -> list[dict]:
    """Table and field definitions, cached. Fetched live so that adding a
    column in Airtable needs no code change here."""
    global _schema_cache
    async with _schema_lock:
        if _schema_cache and not force:
            return _schema_cache
        async with aiohttp.ClientSession() as session:
            async with session.get(META_URL.format(base=BASE_ID),
                                   headers={"Authorization": f"Bearer {TOKEN}"},
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 401:
                    raise RuntimeError("Airtable rejected the token (401). It may have been rotated.")
                if resp.status == 403:
                    raise RuntimeError("The token lacks `schema.bases:read` for this base.")
                if resp.status != 200:
                    raise RuntimeError(f"Airtable returned HTTP {resp.status}.")
                payload = await resp.json()
        _schema_cache = payload.get("tables", [])
        return _schema_cache


def _field_names(table: dict) -> list[str]:
    return [f["name"] for f in table.get("fields", [])]


def map_to_table(data: dict, table: dict) -> dict:
    """Fit the generic submission onto this table's actual columns.

    Only fields the table defines are returned, so National Help (Organization,
    Website, Type of Help) receives a valid subset rather than an error about
    unknown fields.
    """
    names = _field_names(table)
    lowered = {n.lower(): n for n in names}
    out: dict[str, str] = {}

    def put(candidates: list[str], value: str, *, url: bool = False):
        """Write one field, normalised. Empty after cleaning means omit it -
        a whitespace-only value looks filled in Airtable's grid and is
        impossible to spot by eye."""
        clean = normalise(value, is_url=url)
        if not clean:
            return
        for cand in candidates:
            real = lowered.get(cand.lower())
            if real:
                out[real] = clean
                return

    name_priority = [r"organization", r"clinic\s*name", r"cent(er|re)\s*name",
                     r"shelter\s*name", r"hospital\s*name", r"\bname\b", r"doctor"]
    name_field = None
    for pattern in name_priority:
        name_field = next((n for n in names if re.search(pattern, n, re.I)), None)
        if name_field:
            break
    name_value = normalise(data.get("name", ""))
    overflow = ""
    if name_value and looks_like_prose(name_value):
        overflow, name_value = name_value, ""
    if name_field and name_value:
        out[name_field] = name_value

    put(["Location"], resolve_location(data.get("location", "")))
    address = normalise(data.get("address", ""))
    if address.lower().startswith(("http://", "https://", "www.")):
        if not normalise(data.get("website", ""), is_url=True):
            data = {**data, "website": address}
        else:
            overflow = (overflow + "\n" if overflow else "") + f"Also listed: {address}"
        data = {**data, "address": ""}
        address = ""
    put(["Address"], address)
    put(["Phone Number", "Phone"], data.get("phone", ""))
    put(["Website"], data.get("website", ""), url=True)

    notes = [normalise(data.get("notes", "")), overflow]
    if data.get("address") and not lowered.get("address"):
        notes.append(f"Address: {data['address']}")
    if data.get("phone") and not (lowered.get("phone number") or lowered.get("phone")):
        notes.append(f"Phone: {data['phone']}")
    if data.get("location") and not lowered.get("location"):
        notes.append(f"Location: {data['location']}")
    blob = "\n".join(p for p in notes if p).strip()
    if blob:
        put(["Notes", "Type of Help", "How They Help", "Some Services Provided"], blob)

    return out


async def create_record(table_id: str, fields: dict) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            RECORDS_URL.format(base=BASE_ID, table=table_id),
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
            json={"fields": fields, "typecast": True},
            timeout=aiohttp.ClientTimeout(total=25),
        ) as resp:
            body = await resp.text()
            if resp.status in (200, 201):
                try:
                    return json.loads(body).get("id", "")
                except Exception:
                    return ""
            if resp.status == 401:
                raise RuntimeError("Airtable rejected the token (401).")
            if resp.status == 403:
                raise RuntimeError("The token lacks `data.records:write` for this base.")
            if resp.status == 422:
                raise RuntimeError(f"Airtable rejected the fields: {body[:200]}")
            raise RuntimeError(f"Airtable returned HTTP {resp.status}: {body[:160]}")


NAME_HINT = re.compile(r"(organization|clinic\s*name|cent(er|re)\s*name|shelter\s*name|"
                      r"hospital\s*name|doctor|\bname\b)", re.I)


async def list_records(table_id: str) -> list[dict]:
    """Every record in a table, following pagination."""
    out, offset = [], None
    async with aiohttp.ClientSession() as session:
        while True:
            params = {"pageSize": 100}
            if offset:
                params["offset"] = offset
            async with session.get(
                RECORDS_URL.format(base=BASE_ID, table=table_id),
                headers={"Authorization": f"Bearer {TOKEN}"}, params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Airtable returned HTTP {resp.status} listing records")
                payload = await resp.json()
            out.extend(payload.get("records", []))
            offset = payload.get("offset")
            if not offset:
                return out


def _key_values(fields: dict) -> tuple[str, str]:
    """(name, website) lowered, for duplicate comparison."""
    name = ""
    for key, value in fields.items():
        if NAME_HINT.search(key) and str(value).strip():
            name = normalise(value).lower()
            break
    site = normalise(fields.get("Website", ""), is_url=True).lower().rstrip("/")
    return name, site


async def find_existing(table_id: str, fields: dict) -> str | None:
    """Record id of a row with the same name or website, or None.

    Adding a second row for a resource already on the list puts it on the
    public site twice, so an existing match is updated in place instead.
    """
    name, site = _key_values(fields)
    if not (name or site):
        return None
    for record in await list_records(table_id):
        ex_name, ex_site = _key_values(record.get("fields", {}))
        if site and ex_site and site == ex_site:
            return record["id"]
        if name and ex_name and name == ex_name:
            return record["id"]
    return None


async def update_record(table_id: str, record_id: str, fields: dict) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.patch(
            f"{RECORDS_URL.format(base=BASE_ID, table=table_id)}/{record_id}",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
            json={"fields": fields, "typecast": True},
            timeout=aiohttp.ClientTimeout(total=25),
        ) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f"Airtable returned HTTP {resp.status} updating the record")


def submission_embed(data: dict, submitter: str) -> discord.Embed:
    embed = discord.Embed(title="Resource submission", color=discord.Color.teal())
    for label, key in (("Name", "name"), ("Location", "location"), ("Address", "address"),
                       ("Phone", "phone"), ("Website", "website"), ("Notes", "notes")):
        if data.get(key):
            embed.add_field(name=label, value=str(data[key])[:1024], inline=False)
    if data.get("location_raw") and not data.get("location"):
        embed.add_field(
            name="Location needs a state",
            value=(f"They typed `{str(data['location_raw'])[:80]}`, which isn't a state. "
                   "It will be filed under Nationwide unless you set one in Airtable."),
            inline=False)
    embed.set_footer(text=f"Submitted by {submitter}")
    return embed


class ResourceModal(discord.ui.Modal, title="Submit a resource"):
    name = discord.ui.TextInput(label="Name of the place or organisation",
                                max_length=200, required=True)
    location = discord.ui.TextInput(label="Which US state? (or National)",
                                    placeholder="e.g. Washington  ·  or National if nationwide",
                                    max_length=120, required=True)
    address = discord.ui.TextInput(label="Street address", required=False, max_length=300)
    contact = discord.ui.TextInput(label="Phone and/or website",
                                   placeholder="206-555-0100  https://example.org",
                                   required=False, max_length=300)
    notes = discord.ui.TextInput(label="What they offer, hours, restrictions",
                                 style=discord.TextStyle.paragraph,
                                 required=False, max_length=1500)

    async def on_submit(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        phone, website = split_contact(self.contact.value)
        data = {
            "name": normalise(self.name.value),
            "location": resolve_location(self.location.value),
            "location_raw": normalise(self.location.value),
            "address": normalise(self.address.value),
            "phone": phone,
            "website": website,
            "notes": normalise(self.notes.value),
        }

        if not (data["name"] or data["website"] or data["phone"]):
            await interaction.followup.send(
                "I need at least a name, a website or a phone number - a location "
                "on its own isn't enough to add. Please try again with more detail.",
                ephemeral=True)
            return

        channel = interaction.client.get_channel(REVIEW_CHANNEL_ID)
        if channel is None:
            await interaction.followup.send(
                "Thanks, but I couldn't reach the staff review channel. Tell an admin.",
                ephemeral=True)
            return

        try:
            tables = await get_schema()
        except RuntimeError as exc:
            await interaction.followup.send(f"Couldn't reach Airtable: {exc}", ephemeral=True)
            return

        view = ResourceReviewView(tables)
        message = await channel.send(embed=submission_embed(data, str(interaction.user)),
                                     view=view)
        save_submission(message.id, interaction.guild_id or 0, interaction.user.id, data)
        try:
            interaction.client.add_view(view, message_id=message.id)
        except Exception:
            pass
        await interaction.followup.send(
            "Thanks. Your resource has been sent to staff for review.", ephemeral=True)


class ResourceReviewView(discord.ui.View):
    """Persistent. Holds no per submission state: the chosen table is written to
    the database against the message id, so it survives a restart and cannot be
    confused between two submissions sharing this one registered instance."""

    def __init__(self, tables: list[dict] | None = None):
        super().__init__(timeout=None)
        options = [discord.SelectOption(label=t["name"][:100], value=t["id"])
                   for t in (tables or [])[:25]]
        if options:
            self.pick_table.options = options

    async def interaction_check(self, interaction: Interaction) -> bool:
        from commands.application import is_staff
        if is_staff(interaction.user):
            return True
        await interaction.response.send_message("Only staff can review submissions.",
                                                ephemeral=True)
        return False

    @discord.ui.select(placeholder="Choose which table this belongs in",
                       custom_id="resource:table", row=0,
                       options=[discord.SelectOption(label="loading", value="none")])
    async def pick_table(self, interaction: Interaction, select: discord.ui.Select):
        set_submission_table(interaction.message.id, select.values[0])
        label = next((o.label for o in select.options if o.value == select.values[0]),
                     select.values[0])
        await interaction.response.send_message(f"Will file under **{label}**.", ephemeral=True)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success,
                       custom_id="resource:approve", row=1)
    async def approve(self, interaction: Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        sub = get_submission(interaction.message.id)
        if sub is None:
            await interaction.followup.send("I have no record of this submission.", ephemeral=True)
            return
        if sub["status"] != "pending":
            await interaction.followup.send(f"Already {sub['status']}.", ephemeral=True)
            return
        if not sub["table_id"]:
            await interaction.followup.send(
                "Pick a table from the dropdown first, then press Approve.", ephemeral=True)
            return

        try:
            tables = await get_schema()
            table = next((t for t in tables if t["id"] == sub["table_id"]), None)
            if table is None:
                await interaction.followup.send(
                    "That table no longer exists in Airtable.", ephemeral=True)
                return
            fields = map_to_table(sub["data"], table)
            if not fields:
                await interaction.followup.send(
                    "Nothing in this submission maps onto that table's columns.", ephemeral=True)
                return
            if set(fields) <= {"Location"}:
                await interaction.followup.send(
                    "That would create a row with only a Location in it, which the site "
                    "can't show. Add a name, website or phone number first.", ephemeral=True)
                return
            existing = await find_existing(table["id"], fields)
            if existing:
                await update_record(table["id"], existing, fields)
            else:
                await create_record(table["id"], fields)
        except RuntimeError as exc:
            await interaction.followup.send(f"Airtable refused it: {exc}", ephemeral=True)
            return
        except Exception as exc:
            log.exception("resource approve failed")
            await interaction.followup.send(f"Something went wrong: {exc}", ephemeral=True)
            return

        verb = "Updated in" if existing else "Added to"
        set_submission_status(interaction.message.id, "approved")
        await self._retire(interaction, f"{verb} **{table['name']}** by {interaction.user.mention}")
        await self._notify(interaction, sub, f"{verb.lower()} **{table['name']}**")
        note = " (matched an existing row, so it was updated rather than duplicated)" if existing else ""
        await interaction.followup.send(
            f"{verb} **{table['name']}** ({len(fields)} fields).{note}", ephemeral=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger,
                       custom_id="resource:deny", row=1)
    async def deny(self, interaction: Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        sub = get_submission(interaction.message.id)
        if sub is None:
            await interaction.followup.send("I have no record of this submission.", ephemeral=True)
            return
        set_submission_status(interaction.message.id, "denied")
        await self._retire(interaction, f"Denied by {interaction.user.mention}")
        await self._notify(interaction, sub, "not added this time")
        await interaction.followup.send("Denied.", ephemeral=True)

    async def _retire(self, interaction: Interaction, note: str):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(content=note, view=self)
        except discord.HTTPException:
            pass

    async def _notify(self, interaction: Interaction, sub: dict, outcome: str):
        try:
            user = interaction.client.get_user(sub["submitter_id"]) or \
                   await interaction.client.fetch_user(sub["submitter_id"])
            await user.send(f"Your resource submission **{sub['data'].get('name','')}** was {outcome}. "
                            "Thank you for helping keep the list useful.")
        except Exception:
            pass


class ResourcePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Submit a resource", style=discord.ButtonStyle.primary,
                       custom_id="resource:open")
    async def open_form(self, interaction: Interaction, _: discord.ui.Button):
        problem = configuration_problem()
        if problem:
            await interaction.response.send_message(
                f"Submissions aren't set up yet. {problem}", ephemeral=True)
            return
        await interaction.response.send_modal(ResourceModal())


class ResourceSubmissions(commands.Cog):
    resource = app_commands.Group(
        name="resource", description="Community resource submissions",
        default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        ensure_db()

    async def cog_load(self):
        self.bot.add_view(ResourcePanelView())

    @resource.command(name="panel", description="Post the resource submission panel")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Where to post it (defaults to here)")
    async def panel(self, interaction: Interaction, channel: discord.TextChannel | None = None):
        await interaction.response.defer(ephemeral=True)
        problem = configuration_problem()
        if problem:
            await interaction.followup.send(f"Not set up yet. {problem}", ephemeral=True)
            return
        if not REVIEW_CHANNEL_ID:
            await interaction.followup.send(
                "Set `RESOURCE_REVIEW_CHANNEL_ID` in .env so submissions have somewhere to go.",
                ephemeral=True)
            return
        try:
            tables = await get_schema(force=True)
        except RuntimeError as exc:
            await interaction.followup.send(f"Couldn't reach Airtable: {exc}", ephemeral=True)
            return

        target = channel or interaction.channel
        embed = discord.Embed(
            title="Submit a community resource",
            description=(
                "Know a shelter, clinic, food bank, or other resource that should be on our "
                "list? Press the button and tell us about it.\n\n"
                "Staff review every submission before it is added, so it may take a little "
                "while to appear. Thank you for helping."),
            color=discord.Color.teal())
        message = await target.send(embed=embed, view=ResourcePanelView())
        save_panel(message.id, interaction.guild.id, target.id)
        await interaction.followup.send(
            f"Posted in {target.mention}. Submissions go to <#{REVIEW_CHANNEL_ID}>.\n"
            f"Airtable reachable, {len(tables)} tables available to file under.",
            ephemeral=True)

    @resource.command(name="tables", description="List the Airtable tables submissions can go to")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def tables(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        problem = configuration_problem()
        if problem:
            await interaction.followup.send(f"Not set up yet. {problem}", ephemeral=True)
            return
        try:
            tables = await get_schema(force=True)
        except RuntimeError as exc:
            await interaction.followup.send(f"Couldn't reach Airtable: {exc}", ephemeral=True)
            return
        lines = [f"**{t['name']}** - {len(t.get('fields', []))} fields" for t in tables]
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ResourceSubmissions(bot))
