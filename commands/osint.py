from discord import app_commands
import discord
import os
import asyncio
import shlex
from dotenv import load_dotenv

BLACKBIRD_TIMEOUT = int(os.getenv("BLACKBIRD_TIMEOUT", "90"))

@app_commands.command(name="blackbird", description="Run Blackbird OSINT tool with raw arguments")
@app_commands.describe(arguments="Arguments to pass to Blackbird (e.g. -u target --json)")
async def blackbird(interaction: discord.Interaction, arguments: str):

    if not any(role.name in ("Staff", "S6 Professional") for role in interaction.user.roles):
        await interaction.response.send_message("You don't have permission to run this command.", ephemeral=True)
        return

    await interaction.response.send_message(f"Running Blackbird with args: `{arguments}`...", ephemeral=True)

    try:
        try:
            args = shlex.split(arguments)
        except ValueError:
            await interaction.followup.send("Invalid arguments (unmatched quotes).", ephemeral=True)
            return

        process = await asyncio.create_subprocess_exec(
            "python", "blackbird.py", *args,
            cwd=os.getenv("BLACKBIRD_PATH"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=BLACKBIRD_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            await interaction.followup.send(
                f"Blackbird scan timed out after {BLACKBIRD_TIMEOUT}s.", ephemeral=True
            )
            return
        raw_output = stdout.decode()
        error_output = stderr.decode()

        if process.returncode != 0:
            await interaction.followup.send(
                f"Blackbird exited with code {process.returncode}:\n```{error_output.strip()}```",
                ephemeral=True
            )
            return

        links_only = "\n".join(
            line for line in raw_output.splitlines()
            if "http://" in line or "https://" in line
        )

        if not links_only.strip():
            await interaction.followup.send("Blackbird couldnt find any links", ephemeral=True)
            return

        chunks = [links_only[i:i + 1900] for i in range(0, len(links_only), 1900)]
        for chunk in chunks:
            await interaction.followup.send(f"```{chunk}```", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"An error occurred: `{str(e)}`", ephemeral=True)
