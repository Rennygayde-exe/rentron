import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Modal, TextInput
import json, os

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
OPTOUT_FILE = os.path.join(DATA_DIR, "optout.json")

if os.path.exists(OPTOUT_FILE):
    with open(OPTOUT_FILE, "r") as f:
        OPT_OUT = set(json.load(f))
else:
    OPT_OUT = set()

def save_optouts():
    with open(OPTOUT_FILE, "w") as f:
        json.dump(list(OPT_OUT), f, indent=2)

class DataModal(Modal):
    def __init__(self, title, user_id):
        super().__init__(title=f"Submit info for {title}")
        self.user_id = user_id
        self.title_key = title
        self.info = TextInput(label="Enter your information", style=discord.TextStyle.paragraph, required=True)
        self.add_item(self.info)

    async def on_submit(self, interaction: discord.Interaction):
        filename = os.path.join(DATA_DIR, f"{self.title_key}.json")
        data = {}
        if os.path.exists(filename):
            with open(filename, "r") as f:
                try:
                    data = json.load(f)
                except Exception:
                    data = {}
        data[str(self.user_id)] = self.info.value
        with open(filename, "w") as f:
            json.dump(data, f, indent=2)
        await interaction.response.send_message("Your info has been recorded. Thank you!", ephemeral=True)

class MassData(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="massdata", description="Collect data from all members via DM (Admin only)")
    @app_commands.describe(info_title="Title of the info collection (used as filename)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def massdata(self, interaction: discord.Interaction, info_title: str):
        await interaction.response.defer(ephemeral=True)
        sent, skipped = 0, 0
        for member in interaction.guild.members:
            if member.bot:
                continue
            if member.id in OPT_OUT:
                skipped += 1
                continue
            try:
                dm = await member.create_dm()
                view = discord.ui.View()

                async def opt_out_callback(i: discord.Interaction, member_id=member.id):
                    OPT_OUT.add(member_id)
                    save_optouts()
                    await i.response.send_message("You have opted out of mass data requests. Thank you!", ephemeral=True)

                async def submit_callback(i: discord.Interaction, member_id=member.id):
                    modal = DataModal(info_title, member_id)
                    await i.response.send_modal(modal)

                opt_out_btn = discord.ui.Button(label="Opt Out", style=discord.ButtonStyle.danger)
                opt_out_btn.callback = opt_out_callback
                submit_btn = discord.ui.Button(label="Submit Info", style=discord.ButtonStyle.success)
                submit_btn.callback = submit_callback

                view.add_item(submit_btn)
                view.add_item(opt_out_btn)

                await dm.send(
                    f"Hello! We are collecting data to make your TMH experience better. This data requested is {info_title}.\n"
                    f"Please click Submit Info to provide it, or Opt Out to decline future requests.",
                    view=view
                )
                sent += 1
            except Exception as e:
                print(f"Failed to DM {member}: {e}")
                skipped += 1
        await interaction.followup.send(f"Sent {sent} DMs, skipped {skipped} members.", ephemeral=True)

    @massdata.error
    async def massdata_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.errors.MissingPermissions):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        else:
            raise error

async def setup(bot):
    await bot.add_cog(MassData(bot))
