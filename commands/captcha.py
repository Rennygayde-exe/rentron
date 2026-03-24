import random
import discord
from commands.application import is_captcha_verified, mark_captcha_verified


def _gen_question() -> tuple[str, int]:
    """Generate a simple arithmetic captcha. Returns (question_label, answer)."""
    a = random.randint(2, 15)
    b = random.randint(2, 15)
    op = random.choice(['+', '+', '*'])                             
    if op == '+':
        return f"What is {a} + {b}?", a + b
    else:
        return f"What is {a} x {b}?", a * b


class CaptchaModal(discord.ui.Modal):
    def __init__(self, question: str, answer: int):
        super().__init__(title="Human Verification")
        self._answer = answer
        self.answer_field = discord.ui.TextInput(
            label=question,
            placeholder="Enter the number",
            min_length=1,
            max_length=6,
        )
        self.add_item(self.answer_field)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.answer_field.value.strip()
        try:
            given = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "Please enter a whole number.", ephemeral=True
            )
            return

        if given == self._answer:
            mark_captcha_verified(interaction.user.id)
            await interaction.response.send_message(
                "Verification complete! You may now submit an application by clicking **Apply** in the server.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Incorrect answer. Click **Verify** again to try a new question.",
                ephemeral=True,
            )


class CaptchaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.primary, custom_id="captcha:verify")
    async def verify_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if is_captcha_verified(interaction.user.id):
            await interaction.response.send_message(
                "You are already verified! Go ahead and submit your application.", ephemeral=True
            )
            return
        question, answer = _gen_question()
        await interaction.response.send_modal(CaptchaModal(question=question, answer=answer))
