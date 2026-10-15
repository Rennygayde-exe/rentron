"""Role panels - storage and migration layer.

STEP 1 of docs/role-selector-redesign.md. This module deliberately ships no
member-facing UI yet: it establishes the data model and imports the existing
role menus and reaction panels, so the model is proven against real data before
anything members touch is changed. The legacy systems keep running untouched.

Why SQLite rather than the existing JSON files: commands/role_menu.py writes
data/role_menus.json with a plain open("w") + json.dump and no temp-file swap
(role_menu.py:244-247). A crash mid-write truncates it, and the loader
(:240-242) catches the error and sets menus = {}, silently killing every live
dropdown. One corrupt write should not be able to take out every panel.
"""

import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import discord
from discord import Interaction, app_commands
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")
DB_PATH = str(Path(os.getenv("ROLE_PANELS_DB") or (BASE_DIR / "data" / "role_panels.db")))
LEGACY_MENUS = BASE_DIR / "data" / "role_menus.json"
LEGACY_REACTS = BASE_DIR / "data" / "react_roles.json"

log = logging.getLogger(__name__)

CANONICAL: dict[int, tuple[str, str, str]] = {
    1198453210659168299: ("📣", "Announcement Ping", "Get notified when TMH Staff makes an important announcement"),
    1108855031593189386: ("🧙‍♀️", "Coven Corner", "Those of the Pagan Persuasion"),
    1052382095613362206: ("🦊", "Furry Club", "More than human"),
    1195390154655932417: ("🏹", "GWA", "All the weapon/armor/kit/gear talk"),
    935559590781792346:  ("🩺", "MHW", "Mental health discussions"),
    1111497314310303774: ("🌲", "Minecraft Updates", "Be kept in the loop on our Minecraft server"),
    910308566672486420:  ("📰", "News", "Access to the news channels"),
    910308472153845761:  ("🗞️", "Politics", "Access to the politics channels"),
    1261402253886230559: ("🫂", "Support", "A place to vent into the void with others. No advice/solution zone."),
    1261402810612842586: ("👥", "Solutions", "Another place to vent, but others can give feedback and advice"),
    1231044702581690410: ("🙋‍♂️", "Volunteer", "Come help work on TMH projects"),
    1322008282969407578: ("🎮", "Gaming Ping", "For LFG, and announcing you want to stream"),
    1322009168730783754: ("🎬", "Movie Ping", "Keep up with what film is getting streamed next"),
    1473129694898426079: ("💼", "Career Nav", ""),
}

RETIRED_ROLE_IDS: dict[int, str] = {
    1058899129671110787: "VC Ping",
}

BRANCH_ROLE_IDS = {
    663035756260098068,
    663035808478920737,
    663035996643786752,
    663035870559076365,
    663035922505269308,
    762443915394547743,
}


@contextmanager
def _connect(path):
    con = sqlite3.connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


def ensure_db() -> None:
    """Schema v2.

    v1 keyed panels by message_id, which made an unposted panel impossible to
    represent and an edit-and-repost impossible to track. Panels are now keyed
    by a stable `panel_key`; `message_id` is nullable and only set once posted.
    """
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect(DB_PATH) as con:
        cols = [r[1] for r in con.execute("PRAGMA table_info(role_panels)")]
        if cols and "panel_key" not in cols:
            con.execute("ALTER TABLE role_panels RENAME TO role_panels_v1")
            cols = []
        if not cols:
            con.execute(
                """CREATE TABLE IF NOT EXISTS role_panels(
                    panel_key   TEXT PRIMARY KEY,
                    guild_id    INTEGER NOT NULL,
                    channel_id  INTEGER,
                    message_id  INTEGER,
                    config      TEXT NOT NULL,
                    created_at  TEXT NOT NULL)"""
            )
            has_v1 = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='role_panels_v1'"
            ).fetchone()
            if has_v1:
                for mid, gid, ch, cfg, created in con.execute(
                    "SELECT message_id, guild_id, channel_id, config, created_at FROM role_panels_v1"
                ):
                    con.execute(
                        "INSERT OR REPLACE INTO role_panels"
                        "(panel_key, guild_id, channel_id, message_id, config, created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (f"legacy-{mid}", gid, ch, None, cfg, created),
                    )
                con.execute("DROP TABLE role_panels_v1")
        con.commit()


def save_panel(panel_key: str, guild_id: int, config: dict,
               channel_id: int | None = None, message_id: int | None = None) -> None:
    with _connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO role_panels(panel_key, guild_id, channel_id, message_id, config, created_at) "
            "VALUES(?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
            "ON CONFLICT(panel_key) DO UPDATE SET guild_id=excluded.guild_id, "
            "channel_id=COALESCE(excluded.channel_id, role_panels.channel_id), "
            "message_id=COALESCE(excluded.message_id, role_panels.message_id), "
            "config=excluded.config",
            (panel_key, int(guild_id), channel_id, message_id, json.dumps(config)),
        )
        con.commit()


def set_panel_message(panel_key: str, channel_id: int, message_id: int) -> None:
    with _connect(DB_PATH) as con:
        con.execute("UPDATE role_panels SET channel_id=?, message_id=? WHERE panel_key=?",
                    (int(channel_id), int(message_id), panel_key))
        con.commit()


def _row_to_panel(row) -> dict:
    try:
        config = json.loads(row[4])
    except Exception:
        config = {}
    return {"panel_key": row[0], "guild_id": row[1], "channel_id": row[2],
            "message_id": row[3], "config": config, "created_at": row[5]}


_SELECT = "SELECT panel_key, guild_id, channel_id, message_id, config, created_at FROM role_panels"


def get_panel(panel_key: str) -> dict | None:
    with _connect(DB_PATH) as con:
        row = con.execute(_SELECT + " WHERE panel_key=?", (panel_key,)).fetchone()
    return _row_to_panel(row) if row else None


def get_panel_by_message(message_id: int) -> dict | None:
    with _connect(DB_PATH) as con:
        row = con.execute(_SELECT + " WHERE message_id=?", (int(message_id),)).fetchone()
    return _row_to_panel(row) if row else None


def list_panels(guild_id: int) -> list[dict]:
    with _connect(DB_PATH) as con:
        rows = con.execute(_SELECT + " WHERE guild_id=? ORDER BY panel_key", (int(guild_id),)).fetchall()
    return [_row_to_panel(r) for r in rows]


def delete_panel(panel_key: str) -> None:
    with _connect(DB_PATH) as con:
        con.execute("DELETE FROM role_panels WHERE panel_key=?", (panel_key,))
        con.commit()


def _norm_role_name(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


def _find_role_by_name(guild: discord.Guild, wanted: str):
    """Case- and whitespace-insensitive role lookup.

    Used to re-link a role that was deleted and recreated under a new
    snowflake. Loose matching is deliberate: the replacement is typed by hand
    in Discord, and a stray capital should not keep five panels broken.
    """
    target = _norm_role_name(wanted)
    if not target:
        return None
    for role in guild.roles:
        if _norm_role_name(role.name) == target:
            return role
    return None


def _similar_role_names(guild: discord.Guild, wanted: str, limit: int = 3) -> list[str]:
    """Near-misses to show when an exact re-link failed, so the report explains itself."""
    import difflib
    names = [r.name for r in getattr(guild, "roles", [])]
    return difflib.get_close_matches(wanted or "", names, n=limit, cutoff=0.6)


def _load_legacy(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _entries_of(blob) -> list[dict]:
    if isinstance(blob, dict):
        for key in ("entries", "mappings", "roles"):
            if key in blob and isinstance(blob[key], list):
                return blob[key]
    return blob if isinstance(blob, list) else []


def build_migration_plan(guild: discord.Guild) -> dict:
    """Work out what importing the legacy data would produce. Reads only."""
    sources: list[dict] = []
    for path, kind in ((LEGACY_MENUS, "rolemenu"), (LEGACY_REACTS, "reactrole")):
        for message_id, blob in (_load_legacy(path) or {}).items():
            if not isinstance(blob, (dict, list)):
                continue
            gid = blob.get("guild_id") if isinstance(blob, dict) else None
            sources.append({
                "kind": kind,
                "message_id": str(message_id),
                "guild_id": gid,
                "channel_id": (blob.get("channel_id") if isinstance(blob, dict) else None),
                "title": (blob.get("title") if isinstance(blob, dict) else None),
                "description": (blob.get("description") if isinstance(blob, dict) else None),
                "raw_entries": _entries_of(blob),
            })

    stale: list[dict] = []
    corrected: list[dict] = []
    duplicate_emoji: list[dict] = []
    branch_found: list[dict] = []
    unknown_roles: list[dict] = []
    remapped: list[dict] = []
    retired: list[dict] = []

    for src in sources:
        resolved, seen_emoji = [], {}
        for item in src["raw_entries"]:
            rid = item.get("role_id") or item.get("role")
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                continue

            if rid in RETIRED_ROLE_IDS:
                retired.append({"source": src["message_id"], "role_id": rid,
                                "label": RETIRED_ROLE_IDS[rid]})
                continue

            canon = CANONICAL.get(rid)
            role = guild.get_role(rid)

            if role is None and canon:
                replacement = _find_role_by_name(guild, canon[1])
                if replacement is not None:
                    remapped.append({"source": src["message_id"], "role": canon[1],
                                     "old_id": rid, "new_id": replacement.id,
                                     "matched_name": replacement.name})
                    role, rid = replacement, replacement.id

            if role is None:
                wanted = (canon[1] if canon else None) or item.get("label") or "unknown"
                stale.append({"source": src["message_id"], "kind": src["kind"],
                              "role_id": rid, "label": wanted,
                              "suggestions": _similar_role_names(guild, wanted)})
                continue

            legacy_emoji = item.get("emoji")
            emoji = canon[0] if canon else legacy_emoji
            label = canon[1] if canon else (role.name or item.get("label") or "Role")
            description = canon[2] if canon else ""

            if canon and legacy_emoji and legacy_emoji != canon[0]:
                corrected.append({"source": src["message_id"], "role_id": rid,
                                  "role": role.name, "was": legacy_emoji, "now": canon[0]})
            if not canon:
                unknown_roles.append({"source": src["message_id"], "role_id": rid, "role": role.name})
            if rid in BRANCH_ROLE_IDS:
                branch_found.append({"source": src["message_id"], "role_id": rid, "role": role.name})

            if emoji and emoji in seen_emoji:
                duplicate_emoji.append({"source": src["message_id"], "emoji": emoji,
                                        "kept": seen_emoji[emoji], "dropped": role.name})
                continue

            if emoji:
                seen_emoji[emoji] = role.name
            resolved.append({"role_id": rid, "label": label, "emoji": emoji,
                             "description": description})
        src["resolved"] = resolved

    groups: dict[tuple, list[dict]] = {}
    for src in sources:
        if not src["resolved"]:
            continue
        key = tuple(sorted(e["role_id"] for e in src["resolved"]))
        groups.setdefault(key, []).append(src)

    keep, retire = [], []
    for _, members in groups.items():
        members.sort(key=lambda s: int(s["message_id"]))
        keep.append(members[0])
        retire.extend(members[1:])

    empty = [s for s in sources if not s["resolved"]]

    return {
        "sources": sources, "keep": keep, "retire": retire, "empty": empty,
        "stale": stale, "corrected": corrected, "duplicate_emoji": duplicate_emoji,
        "branch_found": branch_found, "unknown_roles": unknown_roles,
        "remapped": remapped, "retired": retired,
    }


def apply_migration_plan(plan: dict, guild: discord.Guild) -> int:
    imported = 0
    for src in plan["keep"]:
        config = {
            "title": src.get("title") or "Roles",
            "description": src.get("description") or "",
            "mode": "multi",
            "entries": src["resolved"],
            "imported_from": src["kind"],
            "legacy_message_id": src["message_id"],
            "posted": False,
        }
        save_panel(f"legacy-{src['message_id']}", guild.id, config,
                   channel_id=int(src.get("channel_id") or 0) or None)
        imported += 1
    return imported


COLOUR_PALETTE: list[str] = [
    "Dark Brown", "Scarlet", "Vivid Red", "Dark Red", "Red",
    "Vivid Orange", "Bright Orange", "Soft Orange", "Very Soft Orange",
    "Vivid Yellow", "Amber",
    "Dark Lime", "Strong Cyan/Lime Green", "Dark Green",
    "Cyan", "Teal", "Aquamarine",
    "Periwinkle", "Baby Blue", "Light Blue", "Dark Blue", "Cobalt Blue",
    "Royal Blue", "Indigo",
    "Dark Desat Magenta", "Purple", "Violet", "Amethyst", "Lavender", "Lilac",
    "Bright Magenta", "Strong Magenta", "Pale Magenta",
    "Rose Quartz", "Pink", "Hot Pink", "Light Pink", "Amaranth",
    "Very Light Red",
    "White", "Very Dark Grey", "Black",
]


TARGET_PANELS: dict[str, dict] = {
    "community": {
        "title": "Community & Interests",
        "description": (
            "Pick the roles you want. You can change them any time — open this "
            "again and your current roles will already be ticked."
        ),
        "mode": "multi",
        "role_ids": [rid for rid in CANONICAL if rid not in BRANCH_ROLE_IDS],
    },
    "branch": {
        "title": "Service Branch",
        "description": "Select your branch of service.",
        "mode": "multi",
        "role_ids": sorted(BRANCH_ROLE_IDS),
    },
}


def build_colour_panel(guild: discord.Guild) -> tuple[int, list[str]]:
    """Build the exclusive colour panel from COLOUR_PALETTE. Returns (count, missing)."""
    entries, missing = [], []
    for name in COLOUR_PALETTE:
        role = _find_role_by_name(guild, name)
        if role is None:
            missing.append(name)
            continue
        entries.append({"role_id": role.id, "label": role.name, "emoji": None,
                        "description": f"#{role.color.value:06x}" if role.color.value else ""})
    if entries:
        existing = get_panel("colour")
        save_panel("colour", guild.id, {
            "title": "Name Colour",
            "description": ("Pick one colour for your name. Choosing a new one replaces "
                            "your old one automatically — use **Remove** to go back to default."),
            "mode": "exclusive",
            "entries": entries,
            "posted": bool(existing and existing.get("message_id")),
        })
    return len(entries), missing


def build_target_panels(guild: discord.Guild) -> tuple[list[str], list[str]]:
    """Create/refresh the two agreed panels. Returns (built, skipped_roles)."""
    built, skipped = [], []
    for key, spec in TARGET_PANELS.items():
        entries = []
        for rid in spec["role_ids"]:
            role = guild.get_role(rid)
            if role is None:
                canon = CANONICAL.get(rid)
                if canon:
                    role = _find_role_by_name(guild, canon[1])
            if role is None:
                skipped.append(f"{CANONICAL.get(rid, (None, str(rid), None))[1]} ({key})")
                continue
            emoji, label, desc = CANONICAL.get(rid, (None, role.name, ""))
            entries.append({"role_id": role.id, "label": label or role.name,
                            "emoji": emoji, "description": desc})
        if not entries:
            continue
        existing = get_panel(key)
        config = {
            "title": spec["title"], "description": spec["description"],
            "mode": spec["mode"], "entries": entries,
            "posted": bool(existing and existing.get("message_id")),
        }
        save_panel(key, guild.id, config)
        built.append(f"{key}: {len(entries)} roles")
    return built, skipped


def format_plan(plan: dict) -> str:
    L = []
    L.append(f"Sources found: {len(plan['sources'])}")
    L.append(f"Panels after de-duplication: {len(plan['keep'])}")
    if plan["retire"]:
        L.append(f"Duplicates to retire: {len(plan['retire'])}")
        for s in plan["retire"]:
            L.append(f"  - {s['message_id']} ({s['kind']}, {len(s['resolved'])} roles)")
    if plan["empty"]:
        L.append(f"Sources with no resolvable roles: {len(plan['empty'])}")
        for s in plan["empty"]:
            L.append(f"  - {s['message_id']} ({s['kind']})")
    if plan.get("retired"):
        by_role: dict[str, int] = {}
        for r in plan["retired"]:
            by_role[r["label"]] = by_role.get(r["label"], 0) + 1
        L.append(f"\nRETIRED ON PURPOSE, removed from every panel ({len(plan['retired'])} references):")
        for label, n in by_role.items():
            L.append(f"  - {label} (dropped from {n} panel{'s' if n != 1 else ''})")
    if plan.get("remapped"):
        L.append(f"\nRE-LINKED to a recreated role ({len(plan['remapped'])}):")
        for r in plan["remapped"]:
            L.append(f"  - {r['role']}: {r['old_id']} -> {r['new_id']}  (in {r['source']})")
    if plan["stale"]:
        L.append(f"\nDELETED ROLES, dropped ({len(plan['stale'])}):")
        for s in plan["stale"]:
            L.append(f"  - {s['role_id']} \"{s['label']}\" in {s['source']}")
        L.append("  (recreate the role with the same name and re-run to re-link it)")
    if plan["corrected"]:
        L.append(f"\nEMOJI CORRECTED to the canonical list ({len(plan['corrected'])}):")
        for c in plan["corrected"]:
            L.append(f"  - {c['role']}: {c['was']} -> {c['now']}  (in {c['source']})")
    if plan["duplicate_emoji"]:
        L.append(f"\nDUPLICATE EMOJI resolved ({len(plan['duplicate_emoji'])}):")
        for d in plan["duplicate_emoji"]:
            L.append(f"  - {d['emoji']} kept \"{d['kept']}\", dropped \"{d['dropped']}\" (in {d['source']})")
    if plan["unknown_roles"]:
        L.append(f"\nNOT IN THE CANONICAL LIST ({len(plan['unknown_roles'])}):")
        for u in plan["unknown_roles"]:
            L.append(f"  - {u['role']} ({u['role_id']}) in {u['source']}")
    if plan["branch_found"]:
        L.append(f"\nBranch roles carried across, self-assignable as agreed ({len(plan['branch_found'])}):")
        for b in plan["branch_found"]:
            L.append(f"  - {b['role']} in {b['source']}")
    return "\n".join(L)


async def apply_role_selection(member: discord.Member, panel_entries: list[dict],
                               selected: set[int]) -> tuple[list[str], list[str], list[str]]:
    """Reconcile a member's roles against their picks. Returns (added, removed, failed).

    Applied ONE ROLE AT A TIME on purpose. discord.py's add_roles(*roles) is
    atomic=True by default, meaning one HTTP call per role anyway - but it
    assigns its result only after the whole call returns, so a Forbidden on
    role 3 of 5 leaves roles 1 and 2 applied server-side while the caller
    reports nothing at all. Doing it here means every role's outcome is known
    and reported, and a hierarchy failure on one role cannot hide the rest.
    """
    guild = member.guild
    panel_ids = [e["role_id"] for e in panel_entries]
    current = {r.id for r in member.roles}
    added, removed, failed = [], [], []

    def label_for(rid: int) -> str:
        for e in panel_entries:
            if e["role_id"] == rid:
                return e.get("label") or str(rid)
        role = guild.get_role(rid)
        return role.name if role else str(rid)

    for rid in [r for r in panel_ids if r in selected and r not in current]:
        role = guild.get_role(rid)
        if role is None:
            failed.append(f"{label_for(rid)} — that role no longer exists")
            continue
        try:
            await member.add_roles(role, reason="Role panel")
            added.append(label_for(rid))
        except discord.Forbidden:
            failed.append(f"{label_for(rid)} — I don't have permission to give you this role")
        except discord.HTTPException:
            failed.append(f"{label_for(rid)} — Discord rejected the change, try again")

    for rid in [r for r in panel_ids if r not in selected and r in current]:
        role = guild.get_role(rid)
        if role is None:
            continue
        try:
            await member.remove_roles(role, reason="Role panel")
            removed.append(label_for(rid))
        except discord.Forbidden:
            failed.append(f"{label_for(rid)} — I don't have permission to remove this role")
        except discord.HTTPException:
            failed.append(f"{label_for(rid)} — Discord rejected the change, try again")

    return added, removed, failed


class RolePickerSelect(discord.ui.Select):
    """One page of a member's picker. Options default to what they already have."""

    def __init__(self, panel: dict, entries: list[dict], member: discord.Member, page: int):
        held = {r.id for r in member.roles}
        options = [
            discord.SelectOption(
                label=(e.get("label") or "Role")[:100],
                value=str(e["role_id"]),
                description=(e.get("description") or None) and e["description"][:100],
                emoji=e.get("emoji") or None,
                default=e["role_id"] in held,
            )
            for e in entries
        ]
        exclusive = panel["config"].get("mode") == "exclusive"
        super().__init__(
            placeholder="Choose your roles" if not exclusive else "Choose one",
            min_values=0,
            max_values=1 if exclusive else len(options),
            options=options,
            row=page,
        )
        self.panel = panel
        self.entries = entries

    async def callback(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.followup.send("Use this in the server.", ephemeral=True)
            return

        selected = {int(v) for v in self.values}
        exclusive = self.panel["config"].get("mode") == "exclusive"
        scope = self.entries

        if exclusive:
            scope = self.panel["config"].get("entries", [])
            if not selected:
                await interaction.followup.send(
                    "Nothing selected, so nothing changed. Use **Remove** below to clear it.",
                    ephemeral=True)
                return

        added, removed, failed = await apply_role_selection(member, scope, selected)

        parts = []
        if added:
            parts.append("**Added:** " + ", ".join(added))
        if removed:
            parts.append("**Removed:** " + ", ".join(removed))
        if failed:
            parts.append("**Couldn't change:**\n" + "\n".join(f"• {f}" for f in failed))
        if not parts:
            parts.append("No changes — you already had exactly those roles.")
        await interaction.followup.send("\n".join(parts)[:1900], ephemeral=True)


class ClearExclusiveButton(discord.ui.Button):
    """The only way to clear an exclusive pick, so no submit can do it by accident."""

    def __init__(self, panel: dict, row: int):
        super().__init__(label="Remove", style=discord.ButtonStyle.secondary, row=row)
        self.panel = panel

    async def callback(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.followup.send("Use this in the server.", ephemeral=True)
            return
        entries = self.panel["config"].get("entries", [])
        _, removed, failed = await apply_role_selection(member, entries, set())
        if removed:
            msg = "**Removed:** " + ", ".join(removed)
        elif failed:
            msg = "**Couldn't change:**\n" + "\n".join(f"• {f}" for f in failed)
        else:
            msg = "You didn't have one of these to begin with."
        await interaction.followup.send(msg[:1900], ephemeral=True)


class RolePickerView(discord.ui.View):
    """Ephemeral, per-member, short-lived. Never shared between members."""

    def __init__(self, panel: dict, member: discord.Member):
        super().__init__(timeout=300)
        entries = panel["config"].get("entries", [])
        exclusive = panel["config"].get("mode") == "exclusive"
        max_pages = 4 if exclusive else 5
        pages = [entries[i:i + 25] for i in range(0, len(entries), 25)][:max_pages]
        for page, chunk in enumerate(pages):
            self.add_item(RolePickerSelect(panel, chunk, member, page))
        if exclusive:
            self.add_item(ClearExclusiveButton(panel, row=len(pages)))


class RolePanelView(discord.ui.View):
    """The public panel. One shared persistent instance; holds no member state."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Choose your roles", style=discord.ButtonStyle.primary,
                       custom_id="rolepanel:open", emoji="🎭")
    async def open_picker(self, interaction: Interaction, _: discord.ui.Button):
        panel = get_panel_by_message(interaction.message.id) if interaction.message else None
        if panel is None:
            await interaction.response.send_message(
                "This role panel is no longer active.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this in the server.", ephemeral=True)
            return
        if not panel["config"].get("entries"):
            await interaction.response.send_message(
                "This panel has no roles configured yet.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Your current roles are already ticked. Adjust and they'll be applied.",
            view=RolePickerView(panel, interaction.user),
            ephemeral=True,
        )


async def refresh_posted_panel(bot: commands.Bot, panel: dict) -> str | None:
    """Re-render a live panel message after its roles changed.

    Editing in place is the whole point: the old system had no edit path, so
    every correction meant reposting, which is why the server ended up with
    four duplicate reaction panels.
    """
    if not panel.get("message_id") or not panel.get("channel_id"):
        return None
    channel = bot.get_channel(int(panel["channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(panel["channel_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return "the panel's channel could not be reached"
    try:
        message = await channel.fetch_message(int(panel["message_id"]))
    except discord.NotFound:
        return "the posted message no longer exists — re-post it with `/roles post`"
    except (discord.Forbidden, discord.HTTPException):
        return "the posted message could not be edited"
    try:
        await message.edit(embed=build_panel_embed(panel), view=RolePanelView())
    except (discord.Forbidden, discord.HTTPException):
        return "the posted message could not be edited"
    return None


CUSTOM_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>$")


def validate_emoji(raw: str | None) -> tuple[str | None, str | None]:
    """Returns (emoji, error). A bad emoji must fail here, not at render time.

    Note discord.SelectOption accepts ANY string and treats it as a unicode
    emoji name, so constructing one proves nothing - Discord only rejects it
    with a 400 when the message is sent. Hence the explicit check.
    """
    if not raw:
        return None, None
    emoji = raw.strip()
    if not emoji:
        return None, None
    if CUSTOM_EMOJI_RE.match(emoji):
        return emoji, None
    if len(emoji) > 16 or any(c.isascii() and (c.isalnum() or c.isspace()) for c in emoji):
        return None, (f"`{emoji[:40]}` isn't an emoji. Paste a single emoji, "
                      "or a custom one like `<:Army:953458919232118884>`.")
    return emoji, None


def build_panel_embed(panel: dict) -> discord.Embed:
    cfg = panel["config"]
    embed = discord.Embed(title=cfg.get("title") or "Roles",
                          description=cfg.get("description") or "",
                          color=discord.Color.blurple())
    lines = []
    for e in cfg.get("entries", []):
        emoji = e.get("emoji") or "•"
        desc = f" — {e['description']}" if e.get("description") else ""
        lines.append(f"{emoji} **{e.get('label')}**{desc}")
    if lines:
        embed.add_field(name="​", value="\n".join(lines)[:1024], inline=False)
    return embed


class RolePanels(commands.Cog):
    roles = app_commands.Group(
        name="roles",
        description="Self-assignable role panels",
        default_permissions=discord.Permissions(manage_roles=True),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        ensure_db()

    async def cog_load(self):
        self.bot.add_view(RolePanelView())

    @roles.command(name="setup", description="Build the Community and Service Branch panels")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def setup_panels(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("Run this in the server.", ephemeral=True)
            return
        built, skipped = build_target_panels(interaction.guild)
        n_col, missing_col = build_colour_panel(interaction.guild)
        if n_col:
            built.append(f"colour: {n_col} colours (pick-one)")
        skipped.extend(f"{m} (colour)" for m in missing_col)
        msg = "**Panels built:**\n" + "\n".join(f"• {b}" for b in built) if built else "Nothing built."
        if skipped:
            msg += "\n\n**Skipped (role not found):**\n" + "\n".join(f"• {s}" for s in skipped)
        msg += "\n\nNothing is live yet — use `/roles post` to publish one."
        await interaction.followup.send(msg[:1900], ephemeral=True)

    @roles.command(name="add", description="Add a role to a panel")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.describe(
        panel="Which panel to add it to",
        role="The role — pick it from the list, no IDs needed",
        emoji="Emoji shown next to it",
        description="Short line explaining what the role is for",
    )
    @app_commands.choices(panel=[
        app_commands.Choice(name="Community & Interests", value="community"),
        app_commands.Choice(name="Service Branch", value="branch"),
        app_commands.Choice(name="Name Colour", value="colour"),
    ])
    async def add_role(self, interaction: Interaction, panel: app_commands.Choice[str],
                       role: discord.Role, emoji: str | None = None,
                       description: str | None = None):
        await interaction.response.defer(ephemeral=True)
        row = get_panel(panel.value)
        if not row:
            await interaction.followup.send(
                f"No `{panel.value}` panel yet — run `/roles setup` first.", ephemeral=True)
            return

        clean_emoji, emoji_err = validate_emoji(emoji)
        if emoji_err:
            await interaction.followup.send(emoji_err, ephemeral=True)
            return

        me = interaction.guild.me if interaction.guild else None
        if me:
            if role >= me.top_role:
                await interaction.followup.send(
                    f"**{role.name}** sits above my highest role, so I could never assign it. "
                    "Move my role above it in Server Settings → Roles, then try again.",
                    ephemeral=True)
                return
            if role.managed:
                await interaction.followup.send(
                    f"**{role.name}** is managed by an integration and can't be assigned by anyone.",
                    ephemeral=True)
                return

        entries = list(row["config"].get("entries", []))
        if any(e["role_id"] == role.id for e in entries):
            await interaction.followup.send(
                f"**{role.name}** is already on that panel.", ephemeral=True)
            return
        if len(entries) >= 125:
            await interaction.followup.send(
                "That panel is full (125 roles is Discord's ceiling).", ephemeral=True)
            return
        if clean_emoji and any(e.get("emoji") == clean_emoji for e in entries):
            other = next(e["label"] for e in entries if e.get("emoji") == clean_emoji)
            await interaction.followup.send(
                f"{clean_emoji} is already used by **{other}** on that panel. "
                "Duplicate emoji made two roles unreachable in the old system — pick another.",
                ephemeral=True)
            return

        entries.append({"role_id": role.id, "label": role.name,
                        "emoji": clean_emoji, "description": (description or "")[:100]})
        cfg = dict(row["config"]); cfg["entries"] = entries
        save_panel(panel.value, interaction.guild.id, cfg)

        note = await refresh_posted_panel(self.bot, get_panel(panel.value))
        msg = f"Added **{role.name}** to *{cfg.get('title')}* ({len(entries)} roles)."
        msg += f"\n⚠️ Saved, but {note}." if note else (
            "\nThe posted panel has been updated." if row.get("message_id")
            else "\nNot posted yet — use `/roles post` when ready.")
        log.info("%s added role %s to panel %s", interaction.user, role.id, panel.value)
        await interaction.followup.send(msg, ephemeral=True)

    @roles.command(name="remove", description="Remove a role from a panel")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.describe(panel="Which panel", role="The role to take off it")
    @app_commands.choices(panel=[
        app_commands.Choice(name="Community & Interests", value="community"),
        app_commands.Choice(name="Service Branch", value="branch"),
        app_commands.Choice(name="Name Colour", value="colour"),
    ])
    async def remove_role(self, interaction: Interaction, panel: app_commands.Choice[str],
                          role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        row = get_panel(panel.value)
        if not row:
            await interaction.followup.send(f"No `{panel.value}` panel.", ephemeral=True)
            return
        entries = [e for e in row["config"].get("entries", []) if e["role_id"] != role.id]
        if len(entries) == len(row["config"].get("entries", [])):
            await interaction.followup.send(
                f"**{role.name}** isn't on that panel.", ephemeral=True)
            return
        cfg = dict(row["config"]); cfg["entries"] = entries
        save_panel(panel.value, interaction.guild.id, cfg)
        note = await refresh_posted_panel(self.bot, get_panel(panel.value))
        msg = (f"Removed **{role.name}** from *{cfg.get('title')}* ({len(entries)} left).\n"
               "Nobody loses the role they already have — it just stops being offered here.")
        if note:
            msg += f"\n⚠️ Saved, but {note}."
        log.info("%s removed role %s from panel %s", interaction.user, role.id, panel.value)
        await interaction.followup.send(msg, ephemeral=True)

    @roles.command(name="post", description="Publish a role panel to a channel")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.describe(panel="Which panel to post", channel="Where to post it (defaults to here)")
    @app_commands.choices(panel=[
        app_commands.Choice(name="Community & Interests", value="community"),
        app_commands.Choice(name="Service Branch", value="branch"),
        app_commands.Choice(name="Name Colour", value="colour"),
    ])
    async def post_panel(self, interaction: Interaction, panel: app_commands.Choice[str],
                         channel: discord.TextChannel | None = None):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("Run this in the server.", ephemeral=True)
            return
        row = get_panel(panel.value)
        if not row or not row["config"].get("entries"):
            await interaction.followup.send(
                f"No `{panel.value}` panel yet — run `/roles setup` first.", ephemeral=True)
            return

        target = channel or interaction.channel
        me = interaction.guild.me

        problems = []
        if me and not target.permissions_for(me).send_messages:
            problems.append(f"I can't post in {target.mention}.")
        if me and not me.guild_permissions.manage_roles:
            problems.append("I don't have Manage Roles, so I can't assign anything.")
        if me:
            for e in row["config"]["entries"]:
                role = interaction.guild.get_role(e["role_id"])
                if role is None:
                    problems.append(f"Role **{e.get('label')}** no longer exists.")
                elif role >= me.top_role:
                    problems.append(
                        f"**{role.name}** is above my highest role, so I can't assign it.")
                elif role.managed:
                    problems.append(f"**{role.name}** is managed by an integration and can't be assigned.")
        if problems:
            await interaction.followup.send(
                "**Not posting — fix these first:**\n" + "\n".join(f"• {p}" for p in problems[:12]),
                ephemeral=True)
            return

        message = await target.send(embed=build_panel_embed(row), view=RolePanelView())
        set_panel_message(panel.value, target.id, message.id)
        cfg = dict(row["config"]); cfg["posted"] = True
        save_panel(panel.value, interaction.guild.id, cfg,
                   channel_id=target.id, message_id=message.id)
        log.info("%s posted role panel %s to %s", interaction.user, panel.value, target.id)
        await interaction.followup.send(
            f"Posted **{row['config'].get('title')}** to {target.mention} with "
            f"{len(row['config']['entries'])} roles.", ephemeral=True)

    @roles.command(name="migrate", description="Import the existing role menus and reaction panels")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.describe(commit="Actually write the imported panels. Defaults to a dry run.")
    async def migrate(self, interaction: Interaction, commit: bool = False):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("Run this in the server.", ephemeral=True)
            return

        plan = build_migration_plan(interaction.guild)
        report = format_plan(plan)

        if commit:
            count = apply_migration_plan(plan, interaction.guild)
            log.info("%s migrated %s role panel(s)", interaction.user, count)
            header = (f"**Imported {count} panel(s).** The legacy menus and reaction "
                      f"messages are untouched and still live.\n")
        else:
            header = ("**Dry run — nothing was written.**\nRe-run with `commit: True` "
                      "to import. Legacy data is never modified either way.\n")

        body = f"{header}```\n{report[:1800]}\n```"
        await interaction.followup.send(body, ephemeral=True)

    @roles.command(name="panels", description="List imported role panels")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def panels(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("Run this in the server.", ephemeral=True)
            return
        rows = list_panels(interaction.guild.id)
        if not rows:
            await interaction.followup.send(
                "No panels imported yet. Run `/roles migrate` first.", ephemeral=True
            )
            return
        live, legacy = [], []
        for p in rows:
            cfg = p["config"]
            where = f" in <#{p['channel_id']}>" if p.get("message_id") else " *(not posted)*"
            line = (f"`{p['panel_key']}` — **{cfg.get('title','Roles')}** — "
                    f"{len(cfg.get('entries', []))} roles{where}")
            (legacy if p["panel_key"].startswith("legacy-") else live).append(line)
        out = []
        if live:
            out.append("**Panels**\n" + "\n".join(live))
        if legacy:
            out.append(f"\n**Imported history** ({len(legacy)}) — kept for reference, not posted\n"
                       + "\n".join(legacy))
        await interaction.followup.send("\n".join(out)[:1900], ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RolePanels(bot))
