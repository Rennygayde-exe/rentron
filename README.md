<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-blue?style=for-the-badge&logo=python&logoColor=white">
  <img src="https://img.shields.io/badge/platform-linux--friendly-brightgreen?style=for-the-badge&logo=linux&logoColor=white">
  <img src="https://img.shields.io/badge/license-MIT-purple?style=for-the-badge">
</p>

<h1 align="center" style="color:white;">Rentron</h1>
<p align="center">
  Discord bot for Moderation, applications, tickets, and OSINT.
</p>

---

## Features

- **Application** – Modular and easy to use applications handled via DM's, stored in sql
- **Blackbird Integration** – OSINT tools with the ability to pass arguements
- **Application Review System** – Staff can approve and deny applications, or open tickets with users
- **Ticket System** – Auto-creates private channels with transcript exports
- **Modular Command Structure** – All commands are split across the `commands/` file structure
- **SIGNAL CLI* – Send text messages to Signal Groupchats!

---

## Setup

### Requirements

- Python 3.11+
- Git
- Discord OAUTH Token
- Blackbird (optional for OSINT)

### Installation

```bash
# Clone and pull the repo
git clone https://github.com/yourusername/rentronbot.git
cd rentronbot

# Create a virtual environment if required
python3 -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt

# Fill out env file and save
DISCORD_BOT_TOKEN=
SIGNAL_PHONE_NUMBER=
TICKET_LOG_CHANNEL_ID=
BLACKBIRDLOGS_ID=
TICKET_CATEGORY_ID=
STAFF_REVIEW_CHANNEL_ID=
GUILD_ID=
BLACKBIRD_PATH=

# Run the bot
python bot.py

# Create Service to run on startup and on crashs

# /etc/systemd/system/rentronbot.service
[Unit]
Description=Rentron
After=network.target

[Service]
User=USERNAME
WorkingDirectory=/home/USERNAME/rentron/rentron
ExecStart=/usr/bin/python3 /home/USERNAME/rentron/rentron
Restart=on-failure

[Install]
WantedBy=multi-user.target


