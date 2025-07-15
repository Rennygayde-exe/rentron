from discord import app_commands
import discord
import os
import json
import asyncio
from dotenv import load_dotenv

@app_commands.command(name="blackbird", description="Run Blackbird OSINT tool on a username")
@app_commands.describe(username="The username to scan")
async def blackbird(interaction: discord.Interaction, username: str):

    if not any(role.name in ("Staff", "S6 Professional") for role in interaction.user.roles):
        await interaction.followup.send("You don't have permission to run this command.", ephemeral=True)
        return
    await interaction.response.send_message(f"Running Blackbird on `{username}`...", ephemeral=True)

    try:
        # Run Blackbird and export the output as a json to our ticket channel
        process = await asyncio.create_subprocess_exec(
            "python", "blackbird.py", "-u", username, "--json",
            cwd=(os.getenv("BLACKBIRD_PATH")),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()
        output = stdout.decode()
        error_output = stderr.decode()

        if process.returncode != 0:
            await interaction.followup.send(
                f"Blackbird exited with code {process.returncode}:\n```{error_output.strip()}```",
                ephemeral=True
            )
            return

        if not output.strip() or output.strip() in ["{}", "[]"]:
            await interaction.followup.send("Blackbird returned no useful data, check the console.", ephemeral=True)
            return

        # Output processing
        output_path = f"/tmp/blackbird_{username}.json"
        with open(output_path, "w") as f:
            f.write(output)

        log_channel_id = int(os.getenv("BLACKBIRDLOGS_ID"))
        channel = interaction.client.get_channel(log_channel_id)
        if channel:
            await channel.send(
                content=f"Blackbird results for `{username}`",
                file=discord.File(output_path)
            )

        await interaction.followup.send("Scan complete. Your intelligence report is sent to the logging channel", ephemeral=True)

    except asyncio.TimeoutError:
        await interaction.followup.send("⏱Blackbird scan timed out.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"An error occurred: `{str(e)}`", ephemeral=True)
