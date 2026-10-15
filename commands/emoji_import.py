"""/importemoji - copy custom emoji out of a message into this server.

Works on emoji from any server, because a custom emoji's image is served from
Discord's public CDN by ID. The bot does not need to be a member of the origin
server, and does not need to be able to *use* the emoji.

Practical limits this has to respect:
  * guild emoji cap depends on boost tier (50/100/150/250), counted separately
    for static and animated
  * an emoji image must be <= 256 KB
  * names are 2-32 chars, letters/digits/underscore only
  * emoji creation is rate limited hard, so failures are reported per emoji
    rather than aborting the batch
"""

import asyncio
import logging
import re

import aiohttp
import discord
from discord import Interaction, app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>")
LINK_RE = re.compile(r"/channels/(\d+|@me)/(\d+)/(\d+)")
CDN = "https://cdn.discordapp.com/emojis/{id}.{ext}"
MAX_BYTES = 256 * 1024

TIME_BUDGET = 120.0
MAX_RATE_WAIT = 20.0


def extract_emojis(message: discord.Message) -> list[dict]:
    """Every custom emoji in the message body, its embeds, and its reactions."""
    found: dict[int, dict] = {}

    def scan(text: str | None):
        for animated, name, eid in EMOJI_RE.findall(text or ""):
            found.setdefault(int(eid), {"id": int(eid), "name": name,
                                        "animated": bool(animated)})

    scan(message.content)
    for embed in message.embeds:
        scan(embed.title)
        scan(embed.description)
        for field in embed.fields:
            scan(field.name)
            scan(field.value)
    for reaction in message.reactions:
        emoji = reaction.emoji
        if isinstance(emoji, (discord.Emoji, discord.PartialEmoji)) and emoji.id:
            found.setdefault(emoji.id, {"id": emoji.id, "name": emoji.name or "emoji",
                                        "animated": bool(emoji.animated)})
    return list(found.values())


def safe_name(raw: str, taken: set[str]) -> str:
    """Discord accepts 2-32 chars of [A-Za-z0-9_]. Make one that fits and is free."""
    name = re.sub(r"[^A-Za-z0-9_]", "", raw or "") or "emoji"
    name = name[:32]
    while len(name) < 2:
        name += "_"
    if name.lower() not in taken:
        return name
    stem = name[:28]
    for n in range(2, 100):
        candidate = f"{stem}_{n}"
        if candidate.lower() not in taken:
            return candidate
    return f"{stem}_{discord.utils.utcnow():%f}"[:32]


def _is_animated_gif(data: bytes) -> bool:
    """True only if the bytes really are a multi-frame GIF."""
    if not data[:6] in (b"GIF87a", b"GIF89a"):
        return False
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as img:
            return getattr(img, "n_frames", 1) > 1
    except Exception:
        return b"NETSCAPE2.0" in data[:1024] or data.count(b"\x00\x21\xf9\x04") > 1


async def _get(session: aiohttp.ClientSession, emoji_id: int, ext: str) -> bytes | None:
    try:
        async with session.get(CDN.format(id=emoji_id, ext=ext),
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception:
        pass
    return None


async def download(session: aiohttp.ClientSession, emoji: dict) -> tuple[bytes, bool] | None:
    """Returns (image bytes, is_animated), deciding animation from the DATA.

    The `a` flag in `<a:name:id>` cannot be trusted: the same emoji is often
    written as `<:name:id>` (Discord's client animates it regardless), and a
    reaction's animated attribute is not always populated. Taking the text at
    face value imported animated emoji as stills.

    So always try .gif first and confirm it really is multi-frame; only fall
    back to .png when it is not.
    """
    data = await _get(session, emoji["id"], "gif")
    if data and _is_animated_gif(data):
        return data, True
    png = await _get(session, emoji["id"], "png")
    if png:
        return png, False
    if data:
        return data, False
    return None


class EmojiImport(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(self, interaction: Interaction, error: Exception):
        """Always close the loop on a deferred interaction.

        Without this, any unhandled exception after defer() leaves the user
        staring at "Rennybot is thinking..." indefinitely, with the reason
        buried in a log they cannot see. Better to surface that something broke.
        """
        log.exception("importemoji failed", exc_info=error)
        message = ("Something went wrong running that. The error has been logged.\n"
                   f"`{type(getattr(error, 'original', error)).__name__}: "
                   f"{str(getattr(error, 'original', error))[:150]}`")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _resolve_message(self, interaction: Interaction, ref: str,
                               channel: discord.TextChannel | None):
        """Accept a full message link or a bare message ID."""
        ref = ref.strip()
        link = LINK_RE.search(ref)
        if link:
            _, channel_id, message_id = link.groups()
            target = self.bot.get_channel(int(channel_id))
            if target is None:
                try:
                    target = await self.bot.fetch_channel(int(channel_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    raise RuntimeError("I can't see the channel that link points to.")
            return await target.fetch_message(int(message_id))

        if not ref.isdigit():
            raise RuntimeError("Give me a message ID or a message link.")
        target = channel or interaction.channel
        try:
            return await target.fetch_message(int(ref))
        except discord.NotFound:
            raise RuntimeError(
                f"No message with that ID in {target.mention}. "
                "Either run this in the right channel, pass `channel:`, or paste the full link "
                "(right click the message, Copy Message Link).")

    @app_commands.command(name="importemoji",
                          description="Copy custom emoji from a message into this server")
    @app_commands.default_permissions(manage_expressions=True)
    @app_commands.checks.has_permissions(manage_expressions=True)
    @app_commands.describe(
        message="Message link, or a message ID if it's in this channel",
        channel="Where that message is, if you gave an ID and it's elsewhere",
    )
    async def importemoji(self, interaction: Interaction, message: str,
                          channel: discord.TextChannel | None = None):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Run this in the server.", ephemeral=True)
            return

        me = guild.me
        if me and not me.guild_permissions.manage_expressions:
            await interaction.followup.send(
                "I need the **Manage Expressions** permission to add emoji.", ephemeral=True)
            return

        try:
            msg = await self._resolve_message(interaction, message, channel)
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.followup.send("I can't read that channel.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Couldn't fetch that message: {exc}", ephemeral=True)
            return

        emojis = extract_emojis(msg)
        if not emojis:
            await interaction.followup.send(
                "No custom emoji in that message. Default emoji like 😀 are built into "
                "Discord and can't be imported.", ephemeral=True)
            return

        existing_ids = {e.id for e in guild.emojis}
        taken_names = {e.name.lower() for e in guild.emojis}
        limit = guild.emoji_limit
        used_static = sum(1 for e in guild.emojis if not e.animated)
        used_animated = sum(1 for e in guild.emojis if e.animated)

        outcomes: list[tuple[str, str, str]] = []
        added_display = []

        started = asyncio.get_running_loop().time()
        async with aiohttp.ClientSession() as session:
            for emoji in emojis:
                if asyncio.get_running_loop().time() - started > TIME_BUDGET:
                    outcomes.append((emoji["name"], "skip", "ran out of time, run the command again"))
                    continue
                if emoji["id"] in existing_ids:
                    outcomes.append((emoji["name"], "skip", "already in this server"))
                    continue
                got = await download(session, emoji)
                if got is None:
                    outcomes.append((emoji["name"], "fail", "image could not be downloaded"))
                    continue
                data, animated = got

                slot_used = used_animated if animated else used_static
                if slot_used >= limit:
                    kind = "animated" if animated else "static"
                    outcomes.append((emoji["name"], "skip", f"{kind} slots full ({limit}/{limit})"))
                    continue
                if len(data) > MAX_BYTES:
                    outcomes.append((emoji["name"], "fail",
                                     f"{len(data)//1024} KB, over the 256 KB limit"))
                    continue

                name = safe_name(emoji["name"], taken_names)
                created = None
                for attempt in range(3):
                    try:
                        created = await guild.create_custom_emoji(
                            name=name, image=data,
                            reason=f"Imported by {interaction.user} from message {msg.id}")
                        break
                    except discord.Forbidden:
                        outcomes.append((emoji["name"], "fail", "I lack permission"))
                        break
                    except discord.HTTPException as exc:
                        retry = getattr(exc, "retry_after", None)
                        if retry and attempt < 2 and float(retry) <= MAX_RATE_WAIT:
                            await asyncio.sleep(float(retry) + 1)
                            continue
                        if retry:
                            outcomes.append((emoji["name"], "fail",
                                             f"rate limited, Discord asked for {int(float(retry))}s"))
                        else:
                            outcomes.append((emoji["name"], "fail",
                                             f"rejected: {exc.text or exc}"[:80]))
                        break
                if created is None:
                    continue

                taken_names.add(name.lower())
                existing_ids.add(emoji["id"])
                if animated:
                    used_animated += 1
                else:
                    used_static += 1
                notes = []
                if name != emoji["name"]:
                    notes.append(f"added as :{name}:")
                if animated:
                    notes.append("animated")
                outcomes.append((emoji["name"], "ok", ", ".join(notes)))
                added_display.append(str(created))
                await asyncio.sleep(1)

        log.info("%s imported %s emoji from message %s",
                 interaction.user, len(added_display), msg.id)

        n_ok = sum(1 for _, r, _ in outcomes if r == "ok")
        n_bad = sum(1 for _, r, _ in outcomes if r == "fail")

        parts = [f"**Found {len(emojis)} emoji in that message.** "
                 f"{n_ok} added, {len(outcomes) - n_ok - n_bad} skipped, {n_bad} failed."]
        if added_display:
            parts.append(" ".join(added_display))

        marks = {"ok": "✅", "skip": "➖", "fail": "❌"}
        lines = [f"{marks[result]} `:{src}:`" + (f" — {detail}" if detail else "")
                 for src, result, detail in outcomes]
        parts.append("\n".join(lines))
        parts.append(f"\n*Slots: {used_static}/{limit} static, {used_animated}/{limit} animated.*")

        body = "\n".join(parts)
        if len(body) > 1900:
            body = body[:1850].rsplit("\n", 1)[0] + "\n*…truncated.*"
        await interaction.followup.send(body, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EmojiImport(bot))
