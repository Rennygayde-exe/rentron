import io
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

ALLOWED_USER_ID = 669626735385640993

class ShellSession(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions = {}

    @app_commands.command(name="shellstart", description="Start a local shell session")
    async def shellstart(self, interaction: discord.Interaction):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("Permission denied", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        proc = await asyncio.create_subprocess_exec(
            "bash", "-l",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        self.sessions[interaction.user.id] = proc
        dm = await interaction.user.create_dm()
        await dm.send("Shell session started. Send commands here. Use /shellclose to end.")
        await interaction.followup.send("Session started; check your DMs", ephemeral=True)

    @app_commands.command(name="shellclose", description="Close your shell session")
    async def shellclose(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        proc = self.sessions.pop(interaction.user.id, None)
        if proc:
            proc.kill()
            await proc.wait()
            await interaction.followup.send("Session closed", ephemeral=True)
        else:
            await interaction.followup.send("No active session", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not isinstance(message.channel, discord.DMChannel):
            return
        proc = self.sessions.get(message.author.id)
        if not proc:
            return
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