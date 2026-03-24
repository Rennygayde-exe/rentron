import io
import os
import time
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks

ALLOWED_USER_ID = int(os.getenv("SHELL_ALLOWED_USER_ID", "669626735385640993"))
SESSION_TIMEOUT_SECONDS = int(os.getenv("SHELL_SESSION_TIMEOUT", "300"))  # 5 min default


class ShellSession(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # sessions: user_id -> {"proc": ..., "last_active": float, "dm": TextChannel}
        self.sessions: dict[int, dict] = {}
        self._timeout_task.start()

    def cog_unload(self):
        self._timeout_task.cancel()

    @tasks.loop(seconds=60)
    async def _timeout_task(self):
        now = time.monotonic()
        stale = [
            uid for uid, s in self.sessions.items()
            if now - s["last_active"] > SESSION_TIMEOUT_SECONDS
        ]
        for uid in stale:
            session = self.sessions.pop(uid, None)
            if not session:
                continue
            proc = session["proc"]
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            dm = session.get("dm")
            if dm:
                try:
                    await dm.send(
                        f"Your shell session was closed after {SESSION_TIMEOUT_SECONDS // 60} min of inactivity."
                    )
                except Exception:
                    pass

    @_timeout_task.before_loop
    async def _before_timeout_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="shellstart", description="Start a local shell session")
    async def shellstart(self, interaction: discord.Interaction):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("Permission denied", ephemeral=True)
            return
        if interaction.user.id in self.sessions:
            await interaction.response.send_message("You already have an active session.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        proc = await asyncio.create_subprocess_exec(
            "bash", "-l",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        dm = await interaction.user.create_dm()
        self.sessions[interaction.user.id] = {
            "proc": proc,
            "last_active": time.monotonic(),
            "dm": dm,
        }
        await dm.send(
            f"Shell session started. Send commands here. Use /shellclose to end.\n"
            f"Sessions auto-close after {SESSION_TIMEOUT_SECONDS // 60} min of inactivity."
        )
        await interaction.followup.send("Session started; check your DMs", ephemeral=True)

    @app_commands.command(name="shellclose", description="Close your shell session")
    async def shellclose(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        session = self.sessions.pop(interaction.user.id, None)
        if session:
            proc = session["proc"]
            proc.kill()
            await proc.wait()
            await interaction.followup.send("Session closed", ephemeral=True)
        else:
            await interaction.followup.send("No active session", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not isinstance(message.channel, discord.DMChannel):
            return
        session = self.sessions.get(message.author.id)
        if not session:
            return
        session["last_active"] = time.monotonic()
        proc = session["proc"]
        cmd = message.content.strip()
        proc.stdin.write((cmd + "\n").encode())
        await proc.stdin.drain()
        await asyncio.sleep(0.1)
        output = b""
        while True:
            chunk = await proc.stdout.read(1024)
            if not chunk:
                break
            output += chunk
            if len(chunk) < 1024:
                break
        text = output.decode(errors="replace").strip()
        if len(text) < 1900:
            await message.channel.send(f"```bash\n{text}\n```")
        else:
            buf = io.BytesIO(text.encode())
            buf.name = "output.txt"
            await message.channel.send(file=discord.File(buf))


async def setup(bot: commands.Bot):
    await bot.add_cog(ShellSession(bot))