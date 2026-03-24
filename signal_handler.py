import os
import asyncio
import discord
from discord import app_commands

SIGNAL_TIMEOUT = int(os.getenv("SIGNAL_TIMEOUT", "30"))

@app_commands.command(
    name="send_signal",
    description="Send a Signal message to a person or group."
)
@app_commands.describe(
    recipient="Phone number or group ID",
    message="The message to send",
    is_group="Is the recipient a group ID?"
)
async def signal_command(
    interaction: discord.Interaction,
    recipient: str,
    message: str,
    is_group: bool = False
):
    await interaction.response.defer(ephemeral=True)
    number = os.getenv("SIGNAL_PHONE_NUMBER")
    if not number:
        await interaction.followup.send("Signal integration via CLI is not configured.")
        return

    try:
        args = ["signal-cli", "-u", number, "send", "-m", message]
        if is_group:
            args += ["-g", recipient]
        else:
            args.append(recipient)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SIGNAL_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await interaction.followup.send("Signal CLI timed out.")
            return

        if proc.returncode == 0:
            await interaction.followup.send("Message sent successfully.")
        else:
            # Show only the last non-empty line to avoid exposing system paths
            lines = [l for l in stderr.decode().splitlines() if l.strip()]
            error_msg = lines[-1] if lines else "Unknown error."
            await interaction.followup.send(f"Failed to send: `{error_msg}`")

    except Exception as e:
        await interaction.followup.send(f"Exception: {type(e).__name__}")
