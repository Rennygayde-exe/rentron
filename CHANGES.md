# Rentron — Changelog

Changes since last GitHub push.

---

## New Files

### `commands/captcha.py`
Human verification system triggered on member join.
- On `on_member_join`, bot sends a DM with a simple math CAPTCHA via a Discord button/modal
- Correct answer marks the user as verified in the `captcha_verified` SQLite table
- Application submission is blocked until the user passes verification
- Staff can manually verify users if DMs are disabled

### `commands/dblookup.py`
`/dblookup` — Look up a user's record in the TMHNET database directly from Discord.

### `commands/deadweight.py`
`/deadweight` command group for identifying and sunsetting stale bot features.
- `/deadweight scan` — scan for unused commands/content
- `/deadweight sunset` — mark a command for soft deprecation
- `/deadweight report` — summarise deadweight candidates and outcomes


### `commands/member_metrics.py`
Member join/leave tracking.
- `on_member_join` / `on_member_remove` events log timestamps to `member_history.db`
- `/unique_users` — count unique members between two dates
- `/backfill_mee6_logs` — import historical join/leave data from MEE6 log channel

### `commands/responses.py`
Keyword auto-response trigger management (extracted from bot core into its own module).

### `commands/scheduler.py`
`/schedulemsg` — schedule a message to be sent at a specific time and channel.
`!remind` — prefix reminder command.

### `utils/usage_log.py`
Command usage analytics logger. Records which commands are used, by whom, and when.

---

## Modified Files

### `bot.py`
- Added `CaptchaView` import and `on_member_join` handler that DMs new members a CAPTCHA
- Added `handle_control_submit` API endpoint (`POST /applications/submit`) — allows the web dashboard to submit applications directly through the bot
- Added Pentagon Pizza Index web routes and static asset serving under `/pentagon-pizza/`
- Added `_top_role_info()` helper for extracting a member's highest role
- Added `utils.usage_log` and `utils.pentagon_pizza_index` imports
- Added `mark_as_submitted` import from `commands.application`
- Added `html` stdlib import

### `commands/application.py`
- Added `captcha_verified` table to the database schema
- Added `is_captcha_verified()` and `mark_captcha_verified()` helper functions
- Application submit button now checks captcha status and blocks unverified users with a clear error message
- Added `mark_as_submitted()` function for the web API submission flow
- Refactored branch role ID loading into `_load_branch_role_ids()` and `get_branch_role_id()` with a normalised env-var map
- Added `APPLICATION_FOLLOWUP_CATEGORY_ID` and `VERIFIED_ROLE_ID` env var reads

### `commands/general.py`
- Rewrote `/gitissue` — now opens a modal with title/description fields and a label dropdown instead of a simple text command
- GitHub issue creation is now async via `aiohttp`
- Added `IssueModal` and label select view
- Misc imports cleanup (`asyncio`, `json`, `datetime`)

### `commands/keyword_alerts.py`
- Added `STAFF_ROLE_NAME` env var for configuring which role receives keyword alerts
- Improved error handling and logging throughout alert dispatch
- Store loading now logs a warning on failure rather than silently failing

### `commands/mod_notes.py`
- Added timestamp formatting for note display (ISO → human-readable UTC)
- Minor import additions (`json`, `io`, `asyncio`, `math`)

### `commands/music.py`
- Added `/cookies` command — upload a Netscape-format cookies `.txt` file for yt-dlp authentication (fixes age-restricted and region-locked YouTube playback)
- Added multi-strategy yt-dlp extractor fallback (`tv_embedded` → `tv` → `web_creator`) to improve YouTube compatibility
- Cookies are persisted to `data/yt_cookies.txt` and automatically picked up on next play


### `commands/pruning_logic.py`
- Added `non_pinned_only` and `include_recent` options to the prune attachment command
- New `_purge_non_pinned_throttled()` method handles bulk deletion with rate-limit sleep between batches
- Purge now generates and posts a CSV log of deleted messages to the channel
- Added safety guard: messages from the last 24 hours are skipped unless `include_recent:true` is passed
- Full purge log includes message ID, channel, author, timestamp, content, and attachment URLs

### `commands/regexsearch.py`
- Significant expansion (+170 lines) — details in diff

### `commands/tts.py`
- Minor additions and import updates

### `commands/vsp.py`
- VSP calculation updates

### `commands/xp.py`
- Added the ability to opt-out

### `signal_handler.py`
- Added graceful shutdown improvements

### `utils/responses.py`
- Minor response matching improvements

---

## Data / Config

### `responses.json`
- Removed 6 stale auto-response triggers

### `data/last_commit.txt`
- Updated commit reference

### `README.md`
- Updated with new feature descriptions
