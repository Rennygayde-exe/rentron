from __future__ import annotations
import math
import discord
from discord import app_commands
from discord.ext import commands

def _fmt_currency(n: float) -> str:
    return f"${n:,.2f}"

class VSP(commands.Cog):
    """Voluntary Separation Pay calculator commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="vsp",
        description="Calculate Army Voluntary Separation Pay."
    )
    @app_commands.describe(
        monthly_pay="Monthly base pay",
        yos_years="YOS",
        yos_months="YMS",
        multiplier="Army VSP multiplier",
        tax_rate="Taxes "
    )
    async def vsp(
        self,
        interaction: discord.Interaction,
        monthly_pay: app_commands.Range[float, 0.01, 1_000_000.0],
        yos_years: app_commands.Range[int, 0, 40],
        yos_months: app_commands.Range[int, 0, 11],
        multiplier: app_commands.Range[float, 0.01, 4.0],
        tax_rate: app_commands.Range[float, 0.0, 1.0] = 0.22,
    ):

        yos = yos_years + (yos_months / 12.0)
        isp_baseline = 0.10 * (12.0 * monthly_pay) * yos

        vsp_gross = multiplier * isp_baseline
        vsp_net = vsp_gross * (1.0 - tax_rate)
        notes = []
        if multiplier > 1.0:
            notes.append("Check your mutiplier memo.")
        if multiplier == 4.0:
            notes.append("4.0× is the max allowed by policy.")
        if tax_rate == 0.22:
            notes.append("22% is the common federal supplemental withholding rate (actual tax may differ).")

        embed = discord.Embed(
            title="Army VSP Calculator",
            color=discord.Color.green(),
            description="**Formula**\n"
                        "• ISP baseline = 0.10 × (12 × monthly pay) × YOS\n"
                        "• VSP = multiplier × ISP baseline"
        )
        embed.add_field(name="Inputs",
                        value=(
                            f"Monthly Pay: {_fmt_currency(monthly_pay)}\n"
                            f"YOS: {yos_years}y {yos_months}m ({yos:.2f} yrs)\n"
                            f"Multiplier: {multiplier:.2f}×\n"
                            f"Withholding: {tax_rate:.2%}"
                        ),
                        inline=False)
        embed.add_field(name="Results",
                        value=(
                            f"ISP Baseline: **{_fmt_currency(isp_baseline)}**\n"
                            f"VSP (Gross): **{_fmt_currency(vsp_gross)}**\n"
                            f"VSP (After Withholding): **{_fmt_currency(vsp_net)}**"
                        ),
                        inline=False)

        if notes:
            embed.add_field(name="Notes", value="\n".join(f"• {n}" for n in notes), inline=False)

        embed.set_footer(text="Reminder: VA/retired pay offsets may recoup gross VSP.")

        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(VSP(bot))
