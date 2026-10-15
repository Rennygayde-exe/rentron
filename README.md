<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-blue?style=for-the-badge&logo=python&logoColor=white">
  <img src="https://img.shields.io/badge/discord.py-2.7.1-5865F2?style=for-the-badge&logo=discord&logoColor=white">
  <img src="https://img.shields.io/badge/platform-linux-brightgreen?style=for-the-badge&logo=linux&logoColor=white">
  <img src="https://img.shields.io/badge/license-MIT-purple?style=for-the-badge">
</p>

<h1 align="center">Rentron</h1>

<p align="center">
  A Discord bot for member onboarding, moderation, ticketing and OSINT.
</p>

---

## Features

| Area | What it does |
|---|---|
| **Applications** | DM-based application flow with staff review, approve / deny / open-ticket actions and pending tracking |
| **Tickets** | Private channels created on demand, with transcript export |
| **Verification** | Captcha gate on join, backed by a persistent view |
| **Moderation** | Kick, ban, timeout, audit log access, mass actions, keyword alerts, regex history search |
| **Roles** | Self-service role panels, reaction role menus and per-member colour roles |
| **XP** | Message-based levelling with cooldowns, leaderboard and opt-out |
| **Feedback** | Anonymous submission panels |
| **OSINT** | Optional [Blackbird](https://github.com/p1ngul1n0/blackbird) integration with argument passthrough |
| **Media** | Music playback (yt-dlp + FFmpeg) and TTS via edge-tts |
| **Encryption** | Fernet encode / decode with PGP-wrapped output and per-user GPG key linking |
| **Scheduling** | Scheduled messages, content pruning and guild snapshots |
| **Calendar** | Google Calendar view via a service account |
| **Minecraft** | Whitelist management and top-balance role sync |
| **Resources** | Airtable-backed resource directory with a submission flow |

## Layout

```
bot.py        Core: client, startup wiring, control API
commands/     Cogs, one per feature area
utils/        Shared helpers
tools/        Standalone maintenance scripts
data/         Runtime state and config (gitignored)
```

## Requirements

- Python 3.11+
- FFmpeg (for music)
- GPG (for encode / decode)
- Blackbird (optional, for OSINT)

## Setup

```bash
git clone https://github.com/Rennygayde-exe/rentron.git
cd rentron

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env_sample .env
```

Fill in `.env`. Every setting is documented inline in `.env_sample`. The minimum to start is
`DISCORD_BOT_TOKEN`, `HOME_GUILD_ID` and `STAFF_ROLE_ID`.

Set roles by ID rather than name. Without an ID the bot falls back to matching on role name, and it
refuses to act when the name is ambiguous.

```bash
python bot.py
```

### As a service

```ini
# /etc/systemd/system/rentron.service
[Unit]
Description=Rentron
After=network.target

[Service]
User=YOUR_USER
WorkingDirectory=/path/to/rentron
ExecStart=/path/to/rentron/.venv/bin/python /path/to/rentron/bot.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Notes

**Startup is not fault tolerant.** Command sync and persistent view registration happen during
startup with no per-step error handling. If something raises early, slash commands and button
handlers go dead while the bot still appears online. The control API binds last, so a bound socket is
the only reliable proof startup finished.

**Discord caps global slash commands at 100.** Check headroom before adding a flat command. An
`app_commands.Group` costs one slot regardless of how many subcommands it holds.

**Never run `git add -A` here.** Databases, logs and credentials live alongside the source and a
blanket add will stage them. Use explicit paths.

## Troubleshooting

**Music is broken.** Check the environment before the code. Every outage so far has been one of
three things: FFmpeg missing, `yt-dlp` out of date, or `discord.py-self` installed over `discord.py`
(clobbers the voice gateway, surfaces as WebSocket 4017).

**Buttons stopped working after a restart.** Persistent views were not re-registered, which usually
means startup died early.

## License

MIT.
