import discord
from discord.ext import commands
from discord import app_commands
import io
import json
import csv
import re
import asyncio
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

class RegexScan(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="regexscan", description="Scan server messages for matches from a regex JSON file")
    async def regexscan(self, interaction: discord.Interaction, regex_file: discord.Attachment):
        await interaction.response.defer(ephemeral=False, thinking=True)

        if not regex_file.filename.endswith(".json"):
            await interaction.followup.send("Please upload a `.json` file containing regex patterns.")
            return

        try:
            data = await regex_file.read()
            loaded = json.loads(data.decode())

            if isinstance(loaded, list):
                patterns = loaded
            elif isinstance(loaded, dict):
                if "patterns" in loaded and isinstance(loaded["patterns"], list):
                    patterns = loaded["patterns"]
                else:
                    patterns = [v for v in loaded.values() if isinstance(v, str)]
            else:
                await interaction.followup.send("Regex file must be a JSON array or object with regex strings.")
                return

            compiled = [re.compile(p) for p in patterns]
        except Exception as e:
            await interaction.followup.send(f"Failed to load regex file: {e}")
            return

        channels = [c for c in interaction.guild.text_channels if c.permissions_for(interaction.guild.me).read_message_history]
        total = len(channels)
        matches = []

        progress_msg = await interaction.followup.send(f"Scanning {total} channels... [----------] 0%")
        last_edit = 0.0
        EDIT_INTERVAL = 2.0

        for i, channel in enumerate(channels, 1):
            try:
                async for msg in channel.history(limit=None):
                    for pat in compiled:
                        if pat.search(msg.content or ""):
                            snippet = msg.content.replace("\n", " ")[:200]
                            link = f"https://discord.com/channels/{interaction.guild.id}/{channel.id}/{msg.id}"
                            matches.append([
                                msg.id,
                                channel.id,
                                msg.author.id,
                                msg.created_at.isoformat(),
                                snippet,
                                pat.pattern,
                                link
                            ])
            except discord.Forbidden:
                continue
            except discord.HTTPException:
                await asyncio.sleep(1)
                continue

            now = asyncio.get_event_loop().time()
            if now - last_edit > EDIT_INTERVAL:
                pct = int((i / total) * 100)
                bar_len = 10
                filled = int(bar_len * pct / 100)
                bar = "█" * filled + "-" * (bar_len - filled)
                await progress_msg.edit(content=f"Scanning {total} channels... [{bar}] {pct}%")
                last_edit = now

        if not matches:
            await progress_msg.edit(content="Scan complete. No matches found.")
            return

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["message_id", "channel_id", "author_id", "created_at", "content", "pattern", "link"])
        writer.writerows(matches)
        output.seek(0)

        file = discord.File(io.BytesIO(output.getvalue().encode()), filename="regex_matches.csv")
        found_channels = len(set(m[1] for m in matches))
        summary = f"Scan complete. Found {len(matches)} matches in {found_channels} channels."

        await progress_msg.edit(content=summary)
        await interaction.followup.send(file=file)
LOG_CHANNEL_ID = int(os.getenv("BLACKBIRDLOGS_ID", "0"))
BEGIN_AGAIN_VIDEO_PATH = os.getenv("PURGE_VIDEO_PATH", "vhs_dead_money_sc_5mb.mp4")

class PurgeChannel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="purgechannel",
        description="Delete ALL messages in a channel by cloning---> deleting (preserves pins)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def purgechannel(
        self,
        interaction: discord.Interaction,
        confirm: bool,
        channel: discord.TextChannel = None
    ):
        await interaction.response.defer(ephemeral=True)

        if not confirm:
            await interaction.followup.send(
                "You must pass `confirm:true` to purge a channel. This action **cannot be undone**.",
                ephemeral=True
            )
            return

        target = channel or interaction.channel

        try:
            pinned = await target.pins()

            await interaction.followup.send(
                f"Purging `{target.name}` now…",
                ephemeral=True
            )

            new_channel = await target.clone(reason=f"Purged by {interaction.user}")
            await target.delete(reason=f"Purged by {interaction.user}")

            if pinned:
                await new_channel.send("**Pinned Posts!:**")
                for p in pinned:
                    embed = discord.Embed(
                        description=p.content or "",
                        color=discord.Color.gold(),
                        timestamp=p.created_at
                    )
                    embed.set_author(name=str(p.author), icon_url=p.author.display_avatar.url)
                    embed.set_footer(text=f"Originally pinned in #{target.name}")

                    files = []
                    for a in p.attachments:
                        fp = await a.to_file()
                        files.append(fp)

                    await new_channel.send(embed=embed, files=files)

            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            embed = discord.Embed(
                title="**Begin Again**",
                description=(
                    f"- Old Channel: `{target.name}` (ID: `{target.id}`)\n"
                    f"- New Channel: {new_channel.mention} (ID: `{new_channel.id}`)\n"
                    f"- By: {interaction.user.mention} (ID: `{interaction.user.id}`)\n"
                    f"- At: {ts}"
                ),
                color=discord.Color.red()
            )

            if os.path.exists(BEGIN_AGAIN_VIDEO_PATH):
                video_file = discord.File(BEGIN_AGAIN_VIDEO_PATH, filename="begin_again.mp4")

                if LOG_CHANNEL_ID:
                    log_ch = self.bot.get_channel(LOG_CHANNEL_ID)
                    if log_ch:
                        await log_ch.send(embed=embed, file=video_file)

                await new_channel.send("** Begin Again... **", file=discord.File(BEGIN_AGAIN_VIDEO_PATH, filename="begin_again.mp4"))
            else:
                if LOG_CHANNEL_ID:
                    log_ch = self.bot.get_channel(LOG_CHANNEL_ID)
                    if log_ch:
                        await log_ch.send(embed=embed)

                await new_channel.send("** Begin Again... **")

        except Exception as e:
            if LOG_CHANNEL_ID:
                log_ch = self.bot.get_channel(LOG_CHANNEL_ID)
                if log_ch:
                    await log_ch.send(f"Purge failed: {e}")

USERLOGCHANNEL = int(os.getenv("USEREXITLOGS", "0"))

class MemberTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        now = datetime.now(timezone.utc)
        joined_at = member.joined_at

        if joined_at:
            if joined_at.tzinfo is None:
                joined_at = joined_at.replace(tzinfo=timezone.utc)

            duration = now - joined_at
            days = duration.days
            hours = duration.seconds // 3600
            time_in_server = f"{days} days, {hours} hours"
        else:
            time_in_server = "Unknown"

        display_name = member.nick if member.nick else member.name

        embed = discord.Embed(
            title="Member has left the server",
            description=(
                f"**Display Name:** {display_name}\n"
                f"**User:** [{member}](https://discord.com/users/{member.id})\n"
                f"**User ID:** `{member.id}`\n"
                f"**Time in Server:** {time_in_server}\n"
                f"**Left At:** {now.strftime('%Y-%m-%d %H:%M:%S UTC')}"
            ),
            color=discord.Color.orange(),
            timestamp=now
        )

        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="Member removal logged")

        channel = member.guild.get_channel(USERLOGCHANNEL)
        if channel:
            await channel.send(embed=embed)
        else:
            print(f"[WARN] USEREXITLOGS channel not found (ID: {USERLOGCHANNEL})")
async def setup(bot: commands.Bot):
    await bot.add_cog(RegexScan(bot))
    await bot.add_cog(PurgeChannel(bot))
    await bot.add_cog(MemberTracker(bot))
