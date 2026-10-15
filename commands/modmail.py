import discord
from discord.ext import commands
import json
import os

MODMAIL_CHANNEL_ID = int(os.getenv("MODMAIL_CHANNEL_ID", "1437490579213910169"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))
HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", os.getenv("GUILD_ID", "0")))
MODMAIL_MAP_FILE = "modmail_threads.json"
MODMAIL_MAP_MAX_ENTRIES = 2000
MODMAIL_TEST_ALLOW_STAFF = os.getenv("MODMAIL_TEST_ALLOW_STAFF") == "1"


class ModMail(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.thread_map: dict[int, int] = self._load_map()

    def _load_map(self) -> dict[int, int]:
        if not os.path.exists(MODMAIL_MAP_FILE):
            return {}
        try:
            with open(MODMAIL_MAP_FILE, "r") as f:
                raw = json.load(f)
            return {int(k): int(v) for k, v in raw.items()}
        except (json.JSONDecodeError, ValueError):
            return {}

    def _save_map(self):
        if len(self.thread_map) > MODMAIL_MAP_MAX_ENTRIES:
            self.thread_map = dict(list(self.thread_map.items())[-MODMAIL_MAP_MAX_ENTRIES:])
        with open(MODMAIL_MAP_FILE, "w") as f:
            json.dump({str(k): v for k, v in self.thread_map.items()}, f)

    async def _is_staff_member(self, user_id: int) -> bool:
        if MODMAIL_TEST_ALLOW_STAFF:
            return False
        if not HOME_GUILD_ID or not STAFF_ROLE_ID:
            return False
        guild = self.bot.get_guild(HOME_GUILD_ID)
        if not guild:
            return False
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.HTTPException):
                return False
        return any(role.id == STAFF_ROLE_ID for role in member.roles)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if isinstance(message.channel, discord.DMChannel):
            if not message.content and not message.attachments:
                return
            if await self._is_staff_member(message.author.id):
                return
            await self._forward_to_staff(message)
            return

        if message.channel.id == MODMAIL_CHANNEL_ID and message.reference:
            target_user_id = self.thread_map.get(message.reference.message_id)
            if not target_user_id:
                return
            await self._relay_to_member(message, target_user_id)

    async def _forward_to_staff(self, message: discord.Message):
        channel = self.bot.get_channel(MODMAIL_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(MODMAIL_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        embed = discord.Embed(
            description=message.content or "*(no text content)*",
            color=discord.Color.blurple(),
            timestamp=message.created_at,
        )
        embed.set_author(name=f"{message.author} ({message.author.id})", icon_url=message.author.display_avatar.url)
        embed.set_footer(text="Reply to this message to respond to them")

        files = []
        for att in message.attachments:
            try:
                files.append(await att.to_file())
            except Exception:
                pass

        try:
            sent = await channel.send(
                content=f"<@&{STAFF_ROLE_ID}>" if STAFF_ROLE_ID else None,
                embed=embed,
                files=files,
            )
        except (discord.Forbidden, discord.HTTPException):
            return

        self.thread_map[sent.id] = message.author.id
        self._save_map()

        try:
            await message.channel.send("Your message has been sent to staff. They'll get back to you soon.")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _relay_to_member(self, message: discord.Message, target_user_id: int):
        user = self.bot.get_user(target_user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(target_user_id)
            except (discord.NotFound, discord.HTTPException):
                await message.add_reaction("❌")
                return

        embed = discord.Embed(
            description=message.content or "*(no text content)*",
            color=discord.Color.green(),
            timestamp=message.created_at,
        )
        embed.set_author(name=f"Reply from {message.author.display_name}", icon_url=message.author.display_avatar.url)

        files = []
        for att in message.attachments:
            try:
                files.append(await att.to_file())
            except Exception:
                pass

        try:
            await user.send(embed=embed, files=files)
        except (discord.Forbidden, discord.HTTPException):
            await message.add_reaction("❌")
            return

        self.thread_map[message.id] = target_user_id
        self._save_map()
        try:
            await message.add_reaction("✅")
        except (discord.Forbidden, discord.HTTPException):
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ModMail(bot))
