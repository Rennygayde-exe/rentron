import math
import matplotlib.pyplot as plt
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands

active_form_data = {
    "e2": {
        "name": "Estradiol",
        "short_name": "E2",
        "molecular_weight": 272.38,
    }
}

ester_data = {
    "eb":  {"name":"Estradiol benzoate",  "dose_form":"oil",  "model":"v3c",
            "params":{"fit_dose":5,"D":1.7050e+08,"k1":3.22397192,"k2":0.58870148,"k3":70721.4018}},
    "edp": {"name":"Estradiol dipropionate","dose_form":"oil","model":"v3c",
            "params":{"fit_dose":5,"D":5288.35292,"k1":0.59848665,"k2":2.51794147,"k3":2.51820476}},
    "ev":  {"name":"Estradiol valerate",   "dose_form":"oil","model":"v3c",
            "params":{"fit_dose":5,"D":2596.05956,"k1":2.38229125,"k2":0.23345814,"k3":1.37642769}},
    "ec_o":{"name":"Estradiol cypionate","short_name":"EC oil","dose_form":"oil","model":"v3c",
            "params":{"fit_dose":5,"D":1920.89671,"k1":0.10321089,"k2":0.89854779,"k3":0.89359759}},
    "ec_s":{"name":"Estradiol cypionate","short_name":"EC sus.","dose_form":"susp.","model":"v3c",
            "params":{"fit_dose":5,"D":1.5669e+08,"k1":0.13586726,"k2":2.51772731,"k3":74768.1493}},
    "een": {"name":"Estradiol enanthate","dose_form":"oil","model":"v3c",
            "params":{"fit_dose":5,"D":333.874181,"k1":0.42412968,"k2":0.43452980,"k3":0.15291485}},
    "eu":  {"name":"Estradiol undecylate","dose_form":"oil","model":"v3c",
            "params":{"fit_dose":5,"D":65.9493374,"k1":0.29634323,"k2":4799337.57,"k3":0.03141554}},
    "pep": {"name":"Polyestradiol phosphate","dose_form":"","model":"v3c",
            "params":{"fit_dose":5,"D":34.46836875,"k1":0.02456035,"k2":135643.711,"k3":0.10582368}},
}

labels = {
    key: f"{d['name']} in {d['dose_form']}" if d['dose_form'] else d['name']
    for key,d in ester_data.items()
}


class E2Simulator(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="e2sim",
        description="Simulate injectable E2 levels over time"
    )
    @app_commands.describe(
        injection="Which injectable ester",
        dose="Dose per injection (mg)",
        interval="Dosing interval (hours)",
        duration="Total simulation time (days, default=14)"
    )
    @app_commands.choices(injection=[
        app_commands.Choice(name="EB (benzoate)", value="eb"),
        app_commands.Choice(name="EDP (dipropionate)", value="edp"),
        app_commands.Choice(name="EV (valerate)", value="ev"),
        app_commands.Choice(name="EC oil", value="ec_o"),
        app_commands.Choice(name="EC susp.", value="ec_s"),
        app_commands.Choice(name="EEn (enanthate)", value="een"),
        app_commands.Choice(name="EU (undecylate)", value="eu"),
        app_commands.Choice(name="PEP", value="pep"),
    ])
    async def e2sim(
        self,
        interaction: discord.Interaction,
        injection: str,
        dose: float,
        interval: float,
        duration: float = 14.0,
    ):
        """Runs either a 1-compartment OR the V3C triple-exp model and returns a plot."""
        await interaction.response.defer()

        data = ester_data[injection]
        model = data["model"]
        times, conc = (
            self._sim_v3c(data["params"], dose, interval, duration)
            if model == "v3c"
            else self._sim_first_order(data["params"], dose, interval, duration)
        )

        fig, ax = plt.subplots()
        ax.plot(times, conc, lw=2)
        ax.set_title(f"{labels[injection]} — {dose} mg q{interval} h")
        ax.set_xlabel("Time (days)")
        ax.set_ylabel("Concentration (pg/mL)")
        ax.grid(True)

        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        buf.seek(0)
        plt.close(fig)

        await interaction.followup.send(
            file=discord.File(buf, filename="e2sim.png")
        )

    def _sim_first_order(self, params, dose, interval, duration):
        t_half_abs = params.get("k1") or 1.0  # dummy
        t_half_elim = params.get("k2") or 24.0
        k_abs = math.log(2)/t_half_abs
        k_elim = math.log(2)/t_half_elim
        dt = 0.1; steps = int((duration*24)/dt)+1

        A = C = 0.0
        next_dose = 0.0
        times, conc = [], []
        for i in range(steps):
            t = i*dt
            if t >= next_dose:
                A += dose
                next_dose += interval
            dA = -k_abs*A*dt
            dC = (k_abs*A - k_elim*C)*dt
            A += dA; C += dC
            mg_per_mL = (C/1.0)/1000  # Vd=1 L
            times.append(t/24); conc.append(mg_per_mL)
        return times, conc

    def _sim_v3c(self, p, dose, interval, duration):
        D, k1, k2, k3 = p["D"], p["k1"], p["k2"], p["k3"]
        dt = 0.1; steps = int((duration*24)/dt)+1

        times, conc = [], []
        dose_times = [n*interval for n in range(int((duration*24)//interval)+1)]

        for i in range(steps):
            t = i*dt
            C = 0.0
            for td in dose_times:
                if t >= td:
                    τ = t - td
                    C += D * dose * (
                        math.exp(-k1*τ)
                        + math.exp(-k2*τ)
                        + math.exp(-k3*τ)
                    )
            pg_per_mL = C * 1e6
            times.append(t/24); conc.append(pg_per_mL)
        return times, conc


async def setup(bot: commands.Bot):
    await bot.add_cog(E2Simulator(bot))