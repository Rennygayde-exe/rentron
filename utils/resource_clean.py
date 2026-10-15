"""Normalisation for values written into the resources Airtable base.

Shared deliberately between the Discord write path (commands/resource_submit.py)
and the one-off remediation of existing rows, so both agree on what "clean"
means. If the rules change, they change in one place.

The base is the sole source for the public state resource map on
themilitaryhub.org - a Cloudflare Worker renders it directly, with no
hand-maintained fallback. Anything written here goes straight onto the site.
"""

import re

_EMPHASIS = re.compile(r"(\*\*|__|~~|`)")
_SUPPRESSED = re.compile(r"<\s*(https?://[^>\s]+)\s*>")
_MDLINK = re.compile(r"\[([^\]]*)\]\(\s*([^)\s]*)\s*\)")
_MULTINL = re.compile(r"\n{2,}")
_SPACES = re.compile(r"[ \t]{2,}")

PLACEHOLDERS = {"n/a", "na", "none", "null", "-", "--", "tbd", "unknown", "n\\a", "?"}


def normalise(value, *, is_url: bool = False) -> str:
    """Clean one value. Returns "" when nothing meaningful survives.

    `is_url` decides how a markdown link resolves: a URL field keeps the
    target, any other field keeps the link text. Some targets are Google
    search URLs rather than tel: URIs, so they must never end up in a phone
    field.
    """
    if value is None:
        return ""
    text = str(value)

    text = _EMPHASIS.sub("", text)
    text = _SUPPRESSED.sub(r"\1", text)
    text = _MDLINK.sub(lambda m: (m.group(2) if is_url else m.group(1)).strip(), text)

    if is_url:
        text = text.replace("<", "").replace(">", "").replace("[", "").replace("]", "")

    text = _MULTINL.sub("\n", text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n")).strip()

    if text.lower() in PLACEHOLDERS:
        return ""
    return text


_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR", "guam": "GU",
    "american samoa": "AS", "us virgin islands": "VI", "northern mariana islands": "MP",
}
_BY_ABBR = {v: k.title() for k, v in _STATES.items()}
_BY_ABBR["DC"] = "District of Columbia"
_BY_ABBR["VI"] = "US Virgin Islands"
_ALIASES = {
    "washington dc": "District of Columbia", "washington d.c.": "District of Columbia",
    "d.c.": "District of Columbia", "dc": "District of Columbia",
    "puerto rico (pr)": "Puerto Rico", "usvi": "US Virgin Islands",
    "virgin islands": "US Virgin Islands", "n. carolina": "North Carolina",
    "s. carolina": "South Carolina",
}
_NATIONAL = {
    "national", "nationwide", "nation wide", "usa", "us", "u.s.", "united states",
    "virtual", "online", "remote", "telehealth", "anywhere", "everywhere", "all states",
    "all 50 states", "worldwide", "global",
}


def _one_state(part: str) -> str | None:
    p = normalise(part).strip(" .").lower()
    if not p:
        return None
    if p in _NATIONAL:
        return "National"
    if p in _ALIASES:
        return _ALIASES[p]
    if p in _STATES:
        return p.title() if p != "district of columbia" else "District of Columbia"
    up = p.upper().replace(".", "")
    if up in _BY_ABBR:
        return _BY_ABBR[up]
    return None


def resolve_location(value) -> str:
    """Map a free-text location onto US state names, or "".

    Location drives which state a resource appears under on the site, so a
    value that is not a state is worse than no value: an unknown location is
    filed under Nationwide, but free text silently breaks the grouping.
    "Lacey, WA" must become "Washington", not stay a city.
    """
    text = normalise(value)
    if not text:
        return ""
    if len(text) > 120:
        return ""

    found, seen = [], set()
    for chunk in re.split(r"[,/;&]| and | \| ", text):
        s = _one_state(chunk)
        if s and s not in seen:
            seen.add(s)
            found.append(s)

    if not found:
        return ""
    if "National" in found and len(found) > 1:
        found = [f for f in found if f != "National"]
    return ", ".join(found)


_PROSE_MARKERS = frozenset("""
will provides provide offers offer gives give helps help serves serve
is are was were has have had can cannot must should does do
if you your we they their them this these those
based includes include accepts accept requires require need needs
available eligible please
""".split())


def looks_like_prose(value, limit: int = 90) -> bool:
    """True if a value belongs in Notes rather than a name column.

    The brief's rule of thumb is ">90 characters or ends in a full stop". That
    misses at least one live row: "Will providie GAC for 16 years of age or
    older with parental consent" is 68 characters with no terminator, yet is
    obviously not a clinic name.

    A raw word count was tried as the extra signal and is wrong: real names run
    long too, and ">10 words" classified "The Lesbian, Gay, Bisexual and
    Transgender Community Center of New York City" as prose, which would strand
    a legitimate submission with an empty name column. The signal that actually
    separates the two is a verb. Require a few words alongside it so a short
    name that happens to contain one ("Food Is Free Project") is left alone.
    """
    text = normalise(value)
    if not text:
        return False
    if len(text) > limit or text.rstrip().endswith((".", "!", "?")):
        return True
    words = text.split()
    hits = 0
    for i, raw in enumerate(words):
        bare = raw.strip(",;:()\"'")
        if bare.lower() in _PROSE_MARKERS and (i == 0 or bare == bare.lower()):
            hits += 1
    return (len(words) > 4 and hits >= 1) or (len(words) >= 3 and hits >= 2)


def is_url(value) -> bool:
    text = normalise(value, is_url=True).strip().lower()
    return text.startswith(("http://", "https://", "www."))


_BARE_URL = re.compile(r"(?:https?://|www\.)[^\s,;|]+", re.I)
_SEARCHY = ("google.com/search", "bing.com/search", "duckduckgo.com/?q", "search?q=")


def split_contact(raw) -> tuple[str, str]:
    """Split a combined "phone and/or website" entry into (phone, website).

    Markdown links are resolved by looking at the TARGET, not by position:
      [(612) 235-6743](tel:6122356743)            -> phone "(612) 235-6743"
      [(617) 227-4313](https://google.com/search) -> phone, target discarded
      [example.org](https://example.org)          -> website "https://example.org"
    A search-engine target is never kept - it is an artefact of someone pasting
    from a phone, not a real link for the resource.
    """
    text = _EMPHASIS.sub("", str(raw or ""))
    text = _SUPPRESSED.sub(r"\1", text)

    phone_bits, site = [], ""

    def take_link(m):
        nonlocal site
        label, target = m.group(1).strip(), m.group(2).strip()
        low = target.lower()
        if low.startswith("tel:") or any(k in low for k in _SEARCHY) or not low.startswith(("http", "www.")):
            phone_bits.append(label)
        elif not site:
            site = target
        return " "

    text = _MDLINK.sub(take_link, text)

    if not site:
        found = _BARE_URL.search(text)
        if found:
            site = found.group(0)
    text = _BARE_URL.sub(" ", text)

    phone_bits.append(text)
    phone = normalise(" ".join(p for p in phone_bits if p).strip(" ,;|/"))
    return phone, normalise(site, is_url=True)
