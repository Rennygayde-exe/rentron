import os
import subprocess
import discord
from discord import app_commands

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

        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if result.returncode == 0:
            await interaction.followup.send("Message sent successfully.")
        else:
            error_msg = result.stderr.strip() or "Unknown error."
            await interaction.followup.send(f"Failed to send: `{error_msg}`")

    except Exception as e:
        await interaction.followup.send(f"Exception: {e}")
