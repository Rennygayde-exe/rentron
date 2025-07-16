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

- **Application** – Uses buttons, dropdowns, and modals in DMs
- **Blackbird Integration** – Slash command to run OSINT scans on usernames
- **Application Review System** – Staff can approve, deny, or open tickets
- **Ticket System** – Auto-creates private channels with transcript exports
- **Modular Command Structure** – All commands are split across the `commands/` file structure

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
DISCORD_TOKEN=your_discord_token
REVIEW_CHANNEL_ID=123456789012345678
TICKET_CATEGORY_ID=123456789012345678
TRANSCRIPT_CHANNEL_ID=123456789012345678
BLACKBIRD_PATH=/path/to/blackbird/if/required

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


