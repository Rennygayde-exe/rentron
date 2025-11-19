from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, List

import discord
from discord import Interaction, TextChannel
from discord import app_commands
from discord.ext import commands


ROLE_MENU_FILE = Path("data/role_menus.json")
logger = logging.getLogger(__name__)


class RoleMenuSelect(discord.ui.Select):
    def __init__(self, cog: "RoleMenu", menu_id: str, entries: List[Dict]):
        self.cog = cog
        self.menu_id = menu_id
        options: List[discord.SelectOption] = []
        for entry in entries:
            emoji_str = entry.get("emoji")
            emoji = discord.PartialEmoji.from_str(emoji_str) if emoji_str else None
            options.append(
                discord.SelectOption(
                    label=entry.get("label", "Role"),
                    value=str(entry.get("role_id")),
                    emoji=emoji,
                )
            )
        max_values = min(len(options), 25)
        super().__init__(
            placeholder="Select roles to toggle",
            min_values=0,
            max_values=max_values,
            options=options[:25],
            custom_id=f"rolemenu:{menu_id}",
        )

    async def callback(self, interaction: Interaction) -> None:  
        await self.cog.handle_selection(self.menu_id, interaction, {int(v) for v in self.values})


class RoleMenuView(discord.ui.View):
    def __init__(self, cog: "RoleMenu", menu_id: str, entries: List[Dict]):
        super().__init__(timeout=None)
        self.add_item(RoleMenuSelect(cog, menu_id, entries))


class RoleSelection(discord.ui.RoleSelect):
    def __init__(self, builder: "RoleMenuBuilderView"):
        super().__init__(placeholder="Select roles for this menu", max_values=25)
        self.builder = builder

    async def callback(self, interaction: Interaction) -> None: 
        self.builder.selected_role_ids = [role.id for role in self.values]
        
        self.builder.emoji_map = {
            role_id: emoji
            for role_id, emoji in self.builder.emoji_map.items()
            if role_id in self.builder.selected_role_ids
        }
        await self.builder.refresh(interaction)


class EmojiConfigModal(discord.ui.Modal):
    def __init__(self, builder: "RoleMenuBuilderView"):
        super().__init__(title="Role Emojis")
        self.builder = builder
        default_lines = []
        for role in builder.current_roles:
            emoji = builder.emoji_map.get(role.id, "")
            default_lines.append(f"{role.name} (ID:{role.id}): {emoji}")
        preset = "\n".join(default_lines)
        self.entries = discord.ui.TextInput(
            label="Enter emojis after each colon",
            style=discord.TextStyle.paragraph,
            default=preset,
            required=False,
            max_length=1900,
        )
        self.add_item(self.entries)

    async def on_submit(self, interaction: Interaction) -> None:
        text = self.entries.value or ""
        updated: dict[int, str] = {}
        for line in text.splitlines():
            match = re.match(r".+\(ID:(\d+)\):\s*(.*)", line)
            if not match:
                continue
            role_id = int(match.group(1))
            emoji = match.group(2).strip()
            if emoji:
                updated[role_id] = emoji
        self.builder.emoji_map = {
            role_id: emoji
            for role_id, emoji in updated.items()
            if role_id in self.builder.selected_role_ids
        }
        await interaction.response.send_message("Saved emoji assignments.", ephemeral=True)
        await self.builder.refresh()


class RoleMenuBuilderView(discord.ui.View):
    def __init__(
        self,
        cog: "RoleMenu",
        interaction: Interaction,
        title: str,
        description: str,
        channel: TextChannel,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.interaction = interaction
        self.guild = interaction.guild
        self.channel = channel
        self.title = title
        self.description = description
        self.selected_role_ids: List[int] = []
        self.emoji_map: Dict[int, str] = {}
        self.role_select = RoleSelection(self)
        self.add_item(self.role_select)

    @property
    def current_roles(self) -> List[discord.Role]:
        if not self.guild:
            return []
        roles: List[discord.Role] = []
        for role_id in self.selected_role_ids:
            role = self.guild.get_role(role_id)
            if role:
                roles.append(role)
        return roles

    def build_status(self) -> str:
        lines = [
            f"**Channel:** {self.channel.mention}",
            "**Title:** " + self.title,
            "",
            "Select roles using the menu below. Optional: click 'Configure Emojis' to customize icons.",
            "",
        ]
        if not self.current_roles:
            lines.append("No roles selected yet.")
        else:
            lines.append("**Selected roles:**")
            for role in self.current_roles:
                emoji = self.emoji_map.get(role.id) or "•"
                lines.append(f"{emoji} {role.mention}")
        return "\n".join(lines)

    async def refresh(self, interaction: Interaction | None = None) -> None:
        content = self.build_status()
        self.role_select.disabled = not bool(self.guild)
        if interaction:
            await interaction.response.edit_message(content=content, view=self)
        else:
            await self.interaction.edit_original_response(content=content, view=self)

    async def on_timeout(self) -> None:  
        self.stop()
        try:
            await self.interaction.edit_original_response(
                content="Role menu builder timed out.", view=None
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Configure Emojis", style=discord.ButtonStyle.primary)
    async def configure_emojis(self, interaction: Interaction, button: discord.ui.Button) -> None:  
        if not self.current_roles:
            await interaction.response.send_message("Select roles first.", ephemeral=True)
            return
        await interaction.response.send_modal(EmojiConfigModal(self))

    @discord.ui.button(label="Publish Menu", style=discord.ButtonStyle.success)
    async def publish(self, interaction: Interaction, button: discord.ui.Button) -> None: 
        if not self.current_roles:
            await interaction.response.send_message("Select at least one role.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        entries = []
        for role in self.current_roles:
            entries.append(
                {
                    "emoji": self.emoji_map.get(role.id),
                    "role_id": role.id,
                    "label": role.name,
                }
            )
        guild = self.guild or getattr(self.channel, "guild", None)
        try:
            await self.cog.create_menu_message(
                guild=guild,
                channel=self.channel,
                title=self.title,
                description=self.description,
                entries=entries,
            )
        except Exception as exc:
            logger.exception("Failed to publish role menu", exc_info=exc)
            await interaction.followup.send("Failed to publish the role menu.", ephemeral=True)
            return
        await interaction.followup.send(
            f"Role menu posted in {self.channel.mention}.", ephemeral=True
        )
        self.stop()
        await self.interaction.edit_original_response(
            content="Role menu created successfully.", view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled role menu setup.", view=None)


class RoleMenu(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.menus: Dict[str, Dict] = {}
        self._load_menus()
        self._register_persistent_views()

    def _load_menus(self) -> None:
        if not ROLE_MENU_FILE.exists():
            self.menus = {}
            return
        try:
            with ROLE_MENU_FILE.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
                self.menus = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to load role menus file")
            self.menus = {}

    def _save_menus(self) -> None:
        ROLE_MENU_FILE.parent.mkdir(parents=True, exist_ok=True)
        with ROLE_MENU_FILE.open("w", encoding="utf-8") as fp:
            json.dump(self.menus, fp, indent=2)

    def _register_persistent_views(self) -> None:
        dirty = False
        for message_id, data in self.menus.items():
            if self._normalize_entries(data):
                dirty = True
            view = self._build_view(message_id, data)
            if view is not None:
                self.bot.add_view(view, message_id=int(message_id))
        if dirty:
            self._save_menus()

    def _normalize_entries(self, data: Dict) -> bool:
        raw_entries = data.get("entries")
        if isinstance(raw_entries, list):
            return False
        if not isinstance(raw_entries, dict):
            data["entries"] = []
            return True

        guild = self.bot.get_guild(data.get("guild_id")) if data.get("guild_id") else None
        converted: List[Dict] = []
        for emoji, role_id in raw_entries.items():
            role_name = "Role"
            if guild:
                role = guild.get_role(role_id)
                if role:
                    role_name = role.name
            converted.append({
                "emoji": emoji,
                "role_id": role_id,
                "label": role_name,
            })
        data["entries"] = converted
        return True

    def _build_view(self, menu_id: str, data: Dict) -> RoleMenuView | None:
        if self._normalize_entries(data):
            logger.info("Migrated role menu %s to dropdown format", menu_id)
        entries = data.get("entries") or []
        if not entries:
            return None
        return RoleMenuView(self, menu_id, entries)

    async def handle_selection(self, menu_id: str, interaction: Interaction, selected_role_ids: set[int]) -> None:
        data = self.menus.get(menu_id)
        if data is None:
            await interaction.response.send_message("This role menu is no longer active.", ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This can only be used inside the server.", ephemeral=True)
            return

        member: discord.Member = interaction.user
        guild = interaction.guild
        menu_role_ids = {entry["role_id"] for entry in data.get("entries", [])}
        selected_role_ids &= menu_role_ids

        await interaction.response.defer(thinking=True, ephemeral=True)

        to_add: List[discord.Role] = []
        for role_id in selected_role_ids:
            role = guild.get_role(role_id)
            if role and role not in member.roles:
                to_add.append(role)

        to_remove: List[discord.Role] = []
        for role_id in (menu_role_ids - selected_role_ids):
            role = guild.get_role(role_id)
            if role and role in member.roles:
                to_remove.append(role)

        added_names: List[str] = []
        removed_names: List[str] = []

        if to_add:
            try:
                await member.add_roles(*to_add, reason="Role menu selection")
                added_names = [role.name for role in to_add if role is not None]
            except discord.Forbidden:
                logger.warning("Missing permissions to add roles via role menu for user %s", member.id)
            except discord.HTTPException:
                logger.exception("HTTP failure when adding roles via role menu")

        if to_remove:
            try:
                await member.remove_roles(*to_remove, reason="Role menu selection")
                removed_names = [role.name for role in to_remove if role is not None]
            except discord.Forbidden:
                logger.warning("Missing permissions to remove roles via role menu for user %s", member.id)
            except discord.HTTPException:
                logger.exception("HTTP failure when removing roles via role menu")

        if added_names or removed_names:
            summary = []
            if added_names:
                summary.append(f"Added: {', '.join(added_names)}")
            if removed_names:
                summary.append(f"Removed: {', '.join(removed_names)}")
            await interaction.followup.send("\n".join(summary), ephemeral=True)
        else:
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass

    @app_commands.command(name="rolemenu", description="Start an interactive role menu builder.")
    @app_commands.describe(
        title="Title for the embed",
        description="Description shown above the dropdown",
        channel="Channel to post the menu (defaults to current)",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def rolemenu(
        self,
        interaction: Interaction,
        title: str,
        description: str,
        channel: TextChannel | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, TextChannel):
            await interaction.response.send_message("Select a text channel to post the menu.", ephemeral=True)
            return
        builder = RoleMenuBuilderView(self, interaction, title, description, target_channel)
        await interaction.response.send_message(builder.build_status(), view=builder, ephemeral=True)


    async def create_menu_message(
        self,
        guild: discord.Guild | None,
        channel: TextChannel,
        title: str,
        description: str,
        entries: List[Dict],
    ) -> None:
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        embed.add_field(
            name="Select the roles you want",
            value="\n".join(
                f"{entry['emoji'] or '•'}  <@&{entry['role_id']}>" for entry in entries
            ),
            inline=False,
        )

        message = await channel.send(embed=embed)
        menu_id = str(message.id)
        self.menus[menu_id] = {
            "guild_id": guild.id if guild else None,
            "channel_id": channel.id,
            "entries": entries,
        }
        self._save_menus()

        view = self._build_view(menu_id, self.menus[menu_id])
        if view is not None:
            await message.edit(view=view)
            self.bot.add_view(view, message_id=message.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoleMenu(bot))
