from __future__ import annotations

import colorsys
import json
import logging
import re
from pathlib import Path
from typing import Optional

import discord
from discord import Interaction
from discord import app_commands
from discord.ext import commands


COLOR_ROLES_FILE = Path("data/color_roles.json")
logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 60.0
PALETTE_HUES = [i * 30 for i in range(12)]


def _rgb_distance(hex_a: int, hex_b: int) -> float:
    ra, ga, ba = (hex_a >> 16) & 0xFF, (hex_a >> 8) & 0xFF, hex_a & 0xFF
    rb, gb, bb = (hex_b >> 16) & 0xFF, (hex_b >> 8) & 0xFF, hex_b & 0xFF
    return ((ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2) ** 0.5


def _suggest_color(existing_hex_values: list[int]) -> int:
    """Return the hue-wheel color (from a 12-step palette) most distant from all existing colors."""
    best_candidate, best_dist = 0, -1.0
    for hue_deg in PALETTE_HUES:
        r, g, b = colorsys.hsv_to_rgb(hue_deg / 360, 1.0, 1.0)
        candidate = (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)
        min_dist = min((_rgb_distance(candidate, ex) for ex in existing_hex_values), default=999.0)
        if min_dist > best_dist:
            best_dist, best_candidate = min_dist, candidate
    return best_candidate


def _parse_hex(raw: str) -> Optional[int]:
    """Parse a hex color string like '#FF4500' or 'FF4500' into an integer. Returns None if invalid."""
    cleaned = raw.strip().lstrip("#")
    if re.fullmatch(r"[0-9A-Fa-f]{6}", cleaned):
        return int(cleaned, 16)
    return None


class ColorRoleModal(discord.ui.Modal, title="Set Your Color"):
    hex_input = discord.ui.TextInput(
        label="Hex color code",
        placeholder="#FF4500",
        min_length=6,
        max_length=7,
    )

    def __init__(self, cog: "ColorRoles") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return

        hex_int = _parse_hex(self.hex_input.value)
        if hex_int is None:
            await interaction.response.send_message(
                "Invalid hex code. Use a format like `#FF4500` or `FF4500`.", ephemeral=True
            )
            return

        member = interaction.user
        guild = interaction.guild

        existing = self.cog._get_existing_colors(guild, exclude_user_id=member.id)
        conflict = next(
            (ex for ex in existing if _rgb_distance(hex_int, ex["hex"]) < SIMILARITY_THRESHOLD),
            None,
        )

        if conflict:
            suggestion = _suggest_color([e["hex"] for e in existing])
            suggestion_str = f"#{suggestion:06X}"
            conflict_user = guild.get_member(conflict["user_id"])
            conflict_mention = conflict_user.mention if conflict_user else f"<user {conflict['user_id']}>"

            embed = discord.Embed(
                title="Similar color detected",
                description=(
                    f"Your color `#{hex_int:06X}` is very close to {conflict_mention}'s color "
                    f"`#{conflict['hex']:06X}`.\n\n"
                    f"**Suggested alternative:** `{suggestion_str}`\n\n"
                    "You can still use your color, or cancel and pick something different."
                ),
                color=discord.Color(hex_int),
            )
            view = ProceedView(cog=self.cog, member=member, hex_int=hex_int)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog._apply_color_role(interaction, member, hex_int, followup=True)


class ProceedView(discord.ui.View):
    def __init__(self, cog: "ColorRoles", member: discord.Member, hex_int: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.member = member
        self.hex_int = hex_int

    @discord.ui.button(label="Use this color anyway", style=discord.ButtonStyle.primary)
    async def proceed(self, interaction: Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("This isn't your picker.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog._apply_color_role(interaction, self.member, self.hex_int, followup=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("This isn't your picker.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()


class ColorRoles(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._config: dict = {}
        self._load_config()


    def _load_config(self) -> None:
        if not COLOR_ROLES_FILE.exists():
            self._config = {}
            return
        try:
            with COLOR_ROLES_FILE.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
                self._config = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to load color roles config")
            self._config = {}

    def _save_config(self) -> None:
        COLOR_ROLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with COLOR_ROLES_FILE.open("w", encoding="utf-8") as fp:
            json.dump(self._config, fp, indent=2)

    def _get_role_id(self, guild_id: int, user_id: int) -> Optional[int]:
        return self._config.get(str(guild_id), {}).get("user_roles", {}).get(str(user_id))

    def _set_role_id(self, guild_id: int, user_id: int, role_id: int) -> None:
        gkey = str(guild_id)
        self._config.setdefault(gkey, {}).setdefault("user_roles", {})[str(user_id)] = role_id
        self._save_config()

    def _del_role_id(self, guild_id: int, user_id: int) -> None:
        gkey = str(guild_id)
        self._config.get(gkey, {}).get("user_roles", {}).pop(str(user_id), None)
        self._save_config()

    def _get_existing_colors(self, guild: discord.Guild, exclude_user_id: int) -> list[dict]:
        """Return list of {user_id, hex} for all other users' current color roles in this guild."""
        result = []
        user_roles = self._config.get(str(guild.id), {}).get("user_roles", {})
        for uid_str, role_id in user_roles.items():
            uid = int(uid_str)
            if uid == exclude_user_id:
                continue
            role = guild.get_role(role_id)
            if role is None:
                continue
            hex_val = role.color.value
            if hex_val == 0:
                continue
            result.append({"user_id": uid, "hex": hex_val})
        return result


    async def _apply_color_role(
        self,
        interaction: Interaction,
        member: discord.Member,
        hex_int: int,
        *,
        followup: bool = False,
    ) -> None:
        guild = member.guild
        existing_role_id = self._get_role_id(guild.id, member.id)
        role: Optional[discord.Role] = None

        if existing_role_id is not None:
            role = guild.get_role(existing_role_id)

        try:
            if role is not None:
                await role.edit(color=discord.Color(hex_int), reason="Color role update")
            else:
                role = await guild.create_role(
                    name=f"clr-{member.id}",
                    color=discord.Color(hex_int),
                    reason="Personal color role",
                )
                self._set_role_id(guild.id, member.id, role.id)

            if role not in member.roles:
                await member.add_roles(role, reason="Color role assignment")

        except discord.Forbidden:
            msg = "I don't have permission to manage that role. Check my role hierarchy."
            if followup:
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        except discord.HTTPException:
            logger.exception("HTTP error applying color role for %s", member.id)
            msg = "Something went wrong. Please try again."
            if followup:
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return

        embed = discord.Embed(
            description=f"Your color has been set to **`#{hex_int:06X}`**.",
            color=discord.Color(hex_int),
        )
        if followup:
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


    @app_commands.command(name="rolecolor", description="Set a custom hex color for your username.")
    async def rolecolor(self, interaction: Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await interaction.response.send_modal(ColorRoleModal(cog=self))

    @app_commands.command(name="resetcolor", description="Remove your custom username color.")
    async def resetcolor(self, interaction: Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return

        member = interaction.user
        guild = interaction.guild
        role_id = self._get_role_id(guild.id, member.id)

        if role_id is None:
            await interaction.response.send_message("You don't have a color role set.", ephemeral=True)
            return

        role = guild.get_role(role_id)
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            if role is not None:
                await role.delete(reason="Color role reset by user")
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to delete your color role.", ephemeral=True
            )
            return
        except discord.HTTPException:
            logger.exception("HTTP error deleting color role for %s", member.id)
            await interaction.followup.send("Something went wrong. Please try again.", ephemeral=True)
            return

        self._del_role_id(guild.id, member.id)
        await interaction.followup.send("Your color role has been removed.", ephemeral=True)



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ColorRoles(bot))
