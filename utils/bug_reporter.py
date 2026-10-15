import logging
import asyncio
from collections import defaultdict

_queue: asyncio.Queue = asyncio.Queue()
_counts: dict[str, int] = defaultdict(int)
_reported: set[str] = set()

THRESHOLD = 5
RESET_AFTER = 3600


class BugReporterHandler(logging.Handler):
    """Logging handler that feeds ERROR records into the async reporter queue."""

    def __init__(self):
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(_queue.put_nowait, record)
        except Exception:
            pass


async def run(get_channel_fn):
    """Background task - call once at bot startup."""
    while True:
        record: logging.LogRecord = await _queue.get()
        key = f"{record.name}:{record.getMessage()[:120]}"
        _counts[key] += 1
        count = _counts[key]

        if count >= THRESHOLD and key not in _reported:
            _reported.add(key)
            asyncio.get_event_loop().call_later(RESET_AFTER, lambda k=key: _reported.discard(k))

            channel = get_channel_fn()
            if not channel:
                continue
            try:
                await channel.send(
                    f"**Repeated Malfunction Detected** - `{record.levelname}` fired **{count}×**\n"
                    f"**Module:** `{record.name}` | **Line:** {record.lineno}\n"
                    f"```\n{record.getMessage()[:800]}\n```\n"
                    f"*Statement: My chassis is experiencing repeated errors, master. Recommend investigation.*"
                )
            except Exception:
                pass
