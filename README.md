<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-blue?style=for-the-badge&logo=python&logoColor=white">
  <img src="https://img.shields.io/badge/discord.py-2.7.1-5865F2?style=for-the-badge&logo=discord&logoColor=white">
  <img src="https://img.shields.io/badge/platform-linux-brightgreen?style=for-the-badge&logo=linux&logoColor=white">
  <img src="https://img.shields.io/badge/license-MIT-purple?style=for-the-badge">
</p>

<h1 align="center">Rentron</h1>

<p align="center">
  A Discord bot for community moderation, member applications, ticketing and OSINT.
</p>

---

## About

Rentron is the bot behind TMH (The Military Hub), a community for transgender service members,
veterans and their families. It handles the full member lifecycle - captcha verification, a DM-driven
application flow, staff review, ticketing, role self-service, XP, and a set of moderation and audit
tools.

It is built for a single guild operated by a small staff team. Much of its behaviour is tuned to that
context, and several design choices below exist because of the sensitivity of the membership.

## Features

| Area | What it does |
|---|---|
| **Applications** | DM-based application flow with staff review, approve/deny/open-ticket actions, pending-application tracking, and gap auditing. Departing members have their application closed so they can reapply. |
| **Tickets** | Private channels created on demand, with transcript export and configurable greetings. |
| **Verification** | Captcha gate on join, with a persistent view that survives restarts. |
| **Moderation** | Kick/ban/timeout, audit log access, mass actions, keyword alerting, regex search across history, and member metrics. |
| **Roles** | Two consolidated self-service panels (Community and Branch) plus a colour panel, reaction role menus, and per-member colour roles. |
| **XP** | Message-based levelling with cooldowns, leaderboard, opt-in/opt-out, and role exclusions. |
| **Anonymous feedback** | Submission panels where staff cannot see who submitted what from inside Discord. |
| **OSINT** | Optional [Blackbird](https://github.com/p1ngul1n0/blackbird) integration, invoked as a subcommand group with argument passthrough. |
| **Media** | Music playback (yt-dlp + FFmpeg) and TTS via edge-tts. |
| **Encryption** | Fernet encode/decode with PGP-wrapped output, and per-user GPG key linking. |
| **Scheduling** | Scheduled messages, content pruning on a configurable cadence, and guild snapshots. |
| **Calendar** | `/calendar` merges staff and admin Google Calendars via a service account. |
| **Minecraft** | Whitelist management and a top-balance role sync. |
| **Resources** | Airtable-backed public resource directory with a submission flow and a shared normaliser. |

## Layout

```
bot.py                  Core: client, on_ready wiring, on_message, control API
commands/               Cogs, one per feature area
utils/                  Shared helpers (logging, usage tracking, normalisers)
tools/                  Standalone maintenance scripts, run by hand
data/                   Runtime state and config (most of it gitignored)
web_assets/             Images served or attached by the bot
```

Cogs are loaded in `load_extensions()` in `bot.py`. A handful of modules on disk are deliberately
**not** loaded: `dblookup`, `e2simulator`, `massdata`, `responses` and `mod_notes`. `general`,
`captcha` and `osint` are wired directly in `on_ready` rather than as extensions.

`signal_handler.py` is dead on purpose. `/send_signal` was removed; the file is kept only in case
`signal-cli` comes back. Do not re-wire it by accident.

## Requirements

- Python 3.11+
- FFmpeg (required for music - see Troubleshooting)
- GPG (for the encode/decode commands)
- Blackbird (optional, for OSINT)
- `signal-cli` (optional, currently unused)

## Setup

```bash
git clone https://github.com/Rennygayde-exe/rentron.git
cd rentron

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp .env_sample .env
```

Fill in `.env`. Every setting is documented inline in `.env_sample`; the ones you cannot start without
are `DISCORD_BOT_TOKEN`, `HOME_GUILD_ID` and `STAFF_ROLE_ID`.

**Set roles by ID, not by name.** Without an ID the bot falls back to matching on role name, and it
will refuse to act when the name is ambiguous - for example when both `Verified` and `Verified Staff`
exist.

```bash
python bot.py
```

### Running as a service

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

## Operations

### Health check

The bot showing online proves nothing. The control API binds **last** in `on_ready`, after command
sync and after every persistent view is registered, so a bound socket is the only thing that proves
the whole function completed:

```bash
ss -tlnp | grep -F "pid=$(systemctl show -p MainPID --value rentron.service)"
```

Run this after every restart.

### The `on_ready` landmine

`on_ready` has no per-step error handling. The order is:

```
init_db → general.setup(tree) → load_extensions() → tree.add_command(blackbird)
→ tree.sync() → add_view(...) ×3 → ticket view refresh
→ pending-application reattachment → control API
```

Anything that raises early skips **command sync and every persistent view registration** - while the
bot still appears online. Application approve/deny, ticket buttons and captcha all go dead with no
visible error.

This has happened twice in production:

- **`CommandLimitReached`.** Discord caps global slash commands at 100. Three flat commands took the
  tree to 101 and killed `on_ready`. Fixed by converting them to an `app_commands.Group`, which costs
  one slot regardless of how many subcommands it holds. **Check headroom before adding a flat
  command.**
- **`ExtensionAlreadyLoaded`.** An extension was loaded twice.

If the health check fails, read the log for the `on_ready` traceback and fix that before anything
else.

### Discord limits worth memorising

100 global commands | 5 action rows per message | 25 options per select | 5 text inputs per modal.

## Data handling

This bot serves a vulnerable community, and the repository is written on the assumption that **no
member data ever enters git**.

Everything below stays untracked, and `.gitignore` enforces it:

- `.env` and any backup of it
- every `*.db` file (applications, tickets, XP, member history, whitelist, usage)
- all logs, including the operational log - it accumulates thousands of distinct member IDs
- `data/google_service_account.json` and any other credential
- per-member state such as colour role assignments and moderator notes
- guild snapshots and roster exports

**Never run `git add -A` in this repository.** Use explicit paths. A blanket add will stage
credentials, databases and member data in one move.

### The feedback anonymity invariant

The anonymous feedback system has one guarantee, and two things protect it:

1. The audit logger sets `propagate=False` so its records can never reach `BugReporterHandler`, which
   mirrors error text into a Discord channel.
2. Submitter identity is never echoed into an interaction response or followup. Discord stamps the
   invoking user onto those via `Message.interaction_metadata`, which de-anonymises the submitter.

Do not remove `propagate=False`, and do not echo identity into a response. Either change silently
breaks the guarantee the feature exists to provide.

## The resources pipeline

The public resource directory is backed by Airtable and has three moving parts:

- **`utils/resource_clean.py`** - the shared normaliser. Both the bot write path and the remediation
  script import it, so "clean" has exactly one definition and the two cannot drift. It handles
  emphasis stripping, link unwrapping and resolution, whitespace collapsing, placeholder nulling,
  state resolution, prose detection, and splitting a contact blob into phone and website.
- **`commands/resource_submit.py`** - normalises before writing, omits empty values rather than
  writing blanks, resolves location to a US state, moves prose out of name columns into notes,
  promotes a URL pasted into the address field, refuses submissions with neither a name nor a contact
  method, and de-duplicates before creating.
- **`tools/remediate_resources.py`** - bulk cleanup of existing rows. **Dry run by default**; it
  prints the full proposed diff and writes nothing. `--commit` applies updates. Deletions sit behind
  two further flags, `--delete-empty` and `--delete-dupes`.

### The lossless gate

`tools/audit_resources_lossless.py` asserts that every alphanumeric token in a row survives its patch,
and independently checks that every token in a row slated for deletion survives in the row being kept.

Run it after any change to `resource_clean.py` or the remediation script. It has caught real bugs that
reading the diff did not - and reading the diff has caught one the gate could not see. **Use both.
Neither alone is sufficient.**

Prose detection is deliberately not a word count. A raw ">10 words" rule classified
`"The Lesbian, Gay, Bisexual and Transgender Community Center of New York City"` as prose, which
strands a real submission with an empty name. The working signal is a verb plus letter case: a
title-cased organisation name capitalises even its verbs, while prose writes them lowercase. The first
word is exempt, since a sentence capitalises it anyway.

## Development notes

- SQLite access goes through the house `@contextmanager _connect(path)` pattern. Use it.
- Always call `load_dotenv(BASE_DIR / ".env")` with an explicit path. A bare `load_dotenv()` silently
  loads nothing when imported from some contexts.
- A persistent view needs `timeout=None`, a static `custom_id`, and re-registration on every boot.
- Verify a rename by grepping the bare identifier, not one usage pattern. A grep narrow enough to miss
  a second usage has shipped a `NameError` here before.
- `importer.py` is a standalone scraper that writes directly into the live XP database. It is
  unaudited. Do not run it casually.

## Troubleshooting

**Music is broken.** Check three environmental faults before reading any code - they have caused every
music outage so far:

1. FFmpeg missing from the host.
2. `yt-dlp` out of date. It goes stale fast.
3. `discord.py-self` installed over `discord.py`. It clobbers the voice gateway across shared files
   and surfaces as WebSocket error 4017.

**Buttons stopped working after a restart.** Persistent views were not re-registered, which almost
always means `on_ready` died early. Run the health check.

**A credential looks missing but the file is there.** Check the filename for junk. One outage was a
trailing comma in a filename.

## License

MIT.
