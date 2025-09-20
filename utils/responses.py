from __future__ import annotations
import json, re
from pathlib import Path
from typing import Iterable

RESPONSES_FILE = Path("responses.json")
RESPONSES: list[dict] = []

def load_responses(path: str | Path = RESPONSES_FILE) -> list[dict]:
    global RESPONSES
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        raw = []
    RESPONSES = raw if isinstance(raw, list) else raw.get("responses", [])
    compile_triggers()
    return RESPONSES

def compile_triggers() -> None:
    for e in RESPONSES:
        mode = (e.get("mode") or "word").lower()
        triggers: Iterable[str] = e.get("triggers") or []
        pats = []
        for t in triggers:
            s = (t or "").strip()
            if not s:
                continue
            if mode == "regex" or s.startswith("re:"):
                pat = s[3:] if s.startswith("re:") else s
                try:
                    pats.append(re.compile(pat, re.I))
                except re.error:
                    continue
            elif mode == "contains":
                pats.append(re.compile(re.escape(s), re.I))
            else:
                token = re.escape(s).replace(r"\ ", r"\s+")
                pats.append(re.compile(rf"(?<!\w){token}(?!\w)", re.I))
        e["_patterns"] = pats

def match_response(text: str, entry: dict) -> bool:
    for rx in entry.get("_patterns", []):
        if rx.search(text):
            return True
    return False
