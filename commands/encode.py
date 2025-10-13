import discord
from discord.ext import commands
from discord import app_commands
import gnupg
import os
import json
import io
import random
from cryptography.fernet import Fernet

GPG_HOME = os.path.expanduser("~/.gnupg")
gpg = gnupg.GPG(gnupghome=GPG_HOME)

KEY_FILE = "user_keys.json"
MAP_FILE = "gpg_users.json"

RUNESET = "ｧｨｩｪｫｬｭｮﾊﾐﾋﾌﾍﾎﾏ0123456789abcdef#$%&+=/"

class GPGEnc(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.user_keys = self._load_json(KEY_FILE)
        self.gpg_users = self._load_json(MAP_FILE)

    def _load_json(self, path):
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
        return {}

    def _save_json(self, path, data):
        with open(path, "w") as f:
            json.dump(data, f)

    def save_keys(self):
        self._save_json(KEY_FILE, self.user_keys)

    def save_gpgmap(self):
        self._save_json(MAP_FILE, self.gpg_users)

    def get_cipher(self, user_id: int) -> Fernet:
        if str(user_id) not in self.user_keys:
            key = Fernet.generate_key().decode()
            self.user_keys[str(user_id)] = key
            self.save_keys()
        return Fernet(self.user_keys[str(user_id)].encode())

    def get_recipient_for(self, member: discord.Member) -> str | None:
        return self.gpg_users.get(str(member.id))

    def _matrix_runes(self, w=48, h=10):
        lines = []
        for _ in range(h):
            lines.append("".join(random.choice(RUNESET) for _ in range(w)))
        return "```\n" + "\n".join(lines) + "\n```"

    # encode
    @app_commands.command(name="encode", description="Encrypt a message; returns matrix runes and attaches token.asc (PGP-wrapped)")
    async def encode(self, interaction: discord.Interaction, message: str):
        recipient = self.get_recipient_for(interaction.user)
        if not recipient:
            await interaction.response.send_message(
                "You do not have a GPG UID linked. Please ask an admin to run `/gpg_link @you <your-gpg-uid>` after importing your public key with `/gpg_import`.",
                ephemeral=True
            )
            return

        cipher = self.get_cipher(interaction.user.id)
        token = cipher.encrypt(message.encode()).decode()

        encrypted = gpg.encrypt(token, recipient, always_trust=True, armor=True)
        if not encrypted.ok:
            await interaction.response.send_message("Failed to PGP-encrypt token (GPG error).", ephemeral=True)
            return

        data = encrypted.data
        asc_bytes = data if isinstance(data, (bytes, bytearray)) else data.encode()
        asc_file = discord.File(io.BytesIO(asc_bytes), filename="token.asc")
        visual = self._matrix_runes()
        await interaction.response.send_message(
            content=f"Encrypted. token.asc attached.\n{visual}",
            file=asc_file,
            ephemeral=False
        )

    @app_commands.command(name="decode", description="Decrypt a Fernet token (pass raw Fernet token)")
    async def decode(self, interaction: discord.Interaction, token: str):
        cipher = self.get_cipher(interaction.user.id)
        try:
            msg = cipher.decrypt(token.encode()).decode()
            await interaction.response.send_message(f"```{msg}```", ephemeral=True)
        except Exception:
            await interaction.response.send_message("Invalid token or decryption failed.", ephemeral=True)

    # GPG management
    @app_commands.command(name="gpg_import", description="Import a friend's GPG public key")
    async def gpg_import(self, interaction: discord.Interaction, key_file: discord.Attachment):
        if not key_file.filename.endswith(".asc"):
            await interaction.response.send_message("Please upload a .asc public key file.", ephemeral=True)
            return
        data = await key_file.read()
        result = gpg.import_keys(data.decode())
        if result.count:
            await interaction.response.send_message("Key imported.", ephemeral=True)
        else:
            await interaction.response.send_message("Failed to import key.", ephemeral=True)

    @app_commands.command(name="gpg_list", description="List imported GPG public keys")
    async def gpg_list(self, interaction: discord.Interaction):
        keys = gpg.list_keys()
        if not keys:
            await interaction.response.send_message("No keys imported.", ephemeral=True)
            return
        text = "\n".join([f"{k['uids'][0]} (ID: {k['keyid']})" for k in keys])
        await interaction.response.send_message(f"```{text}```", ephemeral=True)

    @app_commands.command(name="gpg_link", description="Link a Discord user to an imported GPG UID/email")
    @app_commands.checks.has_permissions(administrator=True)
    async def gpg_link(self, interaction: discord.Interaction, member: discord.Member, gpg_uid: str):
        self.gpg_users[str(member.id)] = gpg_uid
        self.save_gpgmap()
        await interaction.response.send_message(f"Linked {member.mention} to GPG UID/email `{gpg_uid}`.", ephemeral=True)

    @app_commands.command(name="gpg_unlink", description="Remove a GPG link from a Discord user")
    @app_commands.checks.has_permissions(administrator=True)
    async def gpg_unlink(self, interaction: discord.Interaction, member: discord.Member):
        if str(member.id) in self.gpg_users:
            del self.gpg_users[str(member.id)]
            self.save_gpgmap()
            await interaction.response.send_message(f"Unlinked {member.mention} from any GPG key.", ephemeral=True)
        else:
            await interaction.response.send_message("That user does not have a GPG key linked.", ephemeral=True)

    # export/rotate
    @app_commands.command(name="exportkey", description="Export YOUR Fernet key encrypted with YOUR linked GPG key")
    async def exportkey(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        recipient = self.get_recipient_for(interaction.user)
        if not recipient:
            await interaction.response.send_message("You don't have a GPG key linked.", ephemeral=True)
            return
        if uid not in self.user_keys:
            await interaction.response.send_message("You don't have a Fernet key yet. Use `/rotatekey` first.", ephemeral=True)
            return

        key = self.user_keys[uid]
        encrypted = gpg.encrypt(key, recipient, always_trust=True, armor=True)
        if not encrypted.ok:
            await interaction.response.send_message("GPG encryption failed.", ephemeral=True)
            return

        data = encrypted.data
        asc_bytes = data if isinstance(data, (bytes, bytearray)) else data.encode()
        asc_file = discord.File(io.BytesIO(asc_bytes), filename="key.asc")

        await interaction.response.send_message(
            "Your Fernet key has been exported and posted as `key.asc` below.",
            ephemeral=True
        )
        await interaction.channel.send(
            f"{interaction.user.mention} here is your Fernet key (encrypted with your GPG UID `{recipient}`):",
            file=asc_file
        )

    @app_commands.command(name="rotatekey", description="Rotate YOUR Fernet key and get it encrypted with YOUR linked GPG key")
    async def rotatekey(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        recipient = self.get_recipient_for(interaction.user)
        if not recipient:
            await interaction.response.send_message("You don't have a GPG key linked.", ephemeral=True)
            return

        new_key = Fernet.generate_key().decode()
        self.user_keys[uid] = new_key
        self.save_keys()

        encrypted = gpg.encrypt(new_key, recipient, always_trust=True, armor=True)
        if not encrypted.ok:
            await interaction.response.send_message("Key rotation failed (GPG issue).", ephemeral=True)
            return

        data = encrypted.data
        asc_bytes = data if isinstance(data, (bytes, bytearray)) else data.encode()
        asc_file = discord.File(io.BytesIO(asc_bytes), filename="key.asc")

        await interaction.response.send_message(
            "Your Fernet key has been rotated. Check your DMs for the `.asc` file.",
            ephemeral=True
        )
        try:
            await interaction.user.send(
                f"Here is your new Fernet key, encrypted for your GPG identity `{recipient}`.",
                file=asc_file
            )
        except discord.Forbidden:
            await interaction.followup.send("I couldn't DM you your key file. Enable DMs.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(GPGEnc(bot))
