import re, discord
from discord import app_commands, Interaction
from discord.ext import commands

LINK_RX = re.compile(r"/channels/(\d{15,25})/(\d{15,25})/(\d{15,25})$")

class Say(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _allowed(self, i: Interaction) -> bool:
        u = i.user
        return getattr(u.guild_permissions, "manage_messages", False) or any(r.name == "Staff" for r in getattr(u, "roles", []))

    @app_commands.command(name="say", description="Send a message as the bot.")
    @app_commands.describe(
        channel="Target channel",
        text="Message content (optional if files)",
        reply_link="Optional message link to reply to",
        suppress_embeds="Disable embeds on the message",
        silent="Do not ping users/roles",
        file1="Attachment 1",
        file2="Attachment 2",
        file3="Attachment 3",
        file4="Attachment 4",
        file5="Attachment 5",
    )
    async def say(
        self,
        i: Interaction,
        channel: discord.TextChannel,
        text: str | None = None,
        reply_link: str | None = None,
        suppress_embeds: bool = False,
        silent: bool = True,
        file1: discord.Attachment | None = None,
        file2: discord.Attachment | None = None,
        file3: discord.Attachment | None = None,
        file4: discord.Attachment | None = None,
        file5: discord.Attachment | None = None,
    ):
        if not self._allowed(i):
            await i.response.send_message("Denied", ephemeral=True); return
        if not text and not any((file1, file2, file3, file4, file5)):
            await i.response.send_message("Provide text or at least one file.", ephemeral=True); return

        await i.response.defer(ephemeral=True)
        ref = None
        if reply_link:
            m = LINK_RX.search(reply_link)
            if m and int(m.group(1)) == i.guild.id and int(m.group(2)) == channel.id:
                try:
                    msg = await channel.fetch_message(int(m.group(3)))
                    ref = msg.to_reference(fail_if_not_exists=False)
                except Exception:
                    pass

        am = discord.AllowedMentions.none() if silent else discord.AllowedMentions.all()
        files = []
        for a in (file1, file2, file3, file4, file5):
            if a:
                try:
                    files.append(await a.to_file())
                except Exception:
                    pass

        try:
            sent = await channel.send(content=text or "", files=files or None, reference=ref, allowed_mentions=am)
            if suppress_embeds:
                try: await sent.edit(suppress=True)
                except Exception: pass
            await i.followup.send("Sent", ephemeral=True)
        except discord.Forbidden:
            await i.followup.send("No permission to send in that channel.", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"Error: {e.__class__.__name__}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Say(bot))
