from pathlib import Path
"""Propose (and optionally apply) fixes to the existing resource rows.

Dry run unless --commit. Deleting empty rows is a separate --delete-empty flag,
because it removes records from a base that feeds a public website.
"""
import argparse, asyncio, os, re, sys, aiohttp
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / '.env')
from utils.resource_clean import normalise, resolve_location, looks_like_prose, is_url

TOKEN, BASE = os.getenv("AIRTABLE_TOKEN"), os.getenv("AIRTABLE_BASE_ID")
H = {"Authorization": f"Bearer {TOKEN}"}
URL_FIELDS = {"website"}
HEALTHCARE = "Healthcare Resources"

BARE_DOMAIN = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:org|com|net|gov|edu|care|health|co|us|info)"
                         r"(?:/\S*)?$", re.I)


STATE_ZIP = re.compile(r",\s*(?:A[LKZR]|C[AOT]|DE|FL|GA|HI|I[DLNA]|K[SY]|LA|M[EDAINSOT]"
                       r"|N[EVHJMYCD]|OH|OK|OR|P[AR]|RI|S[CD]|T[NX]|UT|V[TA]|W[AVIY]|DC)"
                       r"(?:\s+\d{5}(?:-\d{4})?)?\s*$")
PHONE = re.compile(r"^[\d\s().+\-]{7,24}$")


def looks_like_phone(value):
    return bool(PHONE.match(value)) and sum(c.isdigit() for c in value) >= 7


def looks_like_street(value):
    """A postal address, which the prose test would otherwise mistake for a sentence."""
    if not any(c.isdigit() for c in value):
        return False
    return bool(STATE_ZIP.search(value.strip()))


def as_url(value):
    """Return a usable URL, adding the scheme a bare domain is missing."""
    if is_url(value):
        return normalise(value, is_url=True)
    if BARE_DOMAIN.match(value.strip()):
        return "https://" + normalise(value, is_url=True).strip()
    return ""


NOTESCOL = {"National Help": "Type of Help"}

NAMECOL = {"Affirming Homeless Shelters":"Shelter Name","Food Resources":"Organization Name",
  "Other Resources":"Organization Name","Affirming Hospitals":"Hospital Name",
  "Mental Health Resources":"Clinic Name","LGBTQ+ Centers":"Center Name",
  "Healthcare Resources":"Clinic Name","National Help":"Organization"}


async def fetch(s, tid):
    out, offset = [], None
    while True:
        p = {"pageSize":100}
        if offset: p["offset"]=offset
        async with s.get(f"https://api.airtable.com/v0/{BASE}/{tid}", headers=H, params=p,
                         timeout=aiohttp.ClientTimeout(total=30)) as r:
            d = await r.json()
        out += d.get("records", []); offset = d.get("offset")
        if not offset: return out


def clean_row(fields, table="", review=None):
    """Generic pass: normalise every string, resolve Location.

    An unresolvable Location is never discarded - it is moved into the notes
    column and the row is flagged, because a value we cannot parse is more
    likely a mis-keyed row than junk.
    """
    out, stranded = {}, ""
    for k, v in fields.items():
        if not isinstance(v, str):
            continue
        if k == "Location":
            new = resolve_location(v)
            if not new and normalise(v):
                stranded = normalise(v)
        else:
            new = normalise(v, is_url=k.lower() in URL_FIELDS)
        if new != v:
            out[k] = new
    if stranded:
        col = NOTESCOL.get(table, "Notes")
        existing = normalise(str(fields.get(col, "")))
        merged = f"{existing}\n{stranded}".strip() if existing else stranded
        out[col] = merged
        if review is not None:
            review.append((table, stranded))
    return out


def fix_healthcare(fields):
    """Re-seat Healthcare Resources values into the columns they belong in."""
    get = lambda k: normalise(fields.get(k, ""))
    doctor, clinic = get("Doctor's Name"), get("Clinic Name")
    address, website, notes = get("Address"), get("Website"), get("Notes")

    loc = fields.get("Location", "")

    prose, urls, names, streets, phones = [], [], [], [], []
    phone = get("Phone Number")
    if phone:
        phones.append(phone)
    for value in (doctor, clinic, address, website):
        if not value:
            continue
        if looks_like_phone(value):
            phones.append(value)
            continue
        if (url := as_url(value)):
            urls.append(url)
        elif looks_like_street(value):
            streets.append(value)
        elif looks_like_prose(value):
            prose.append(value)
        else:
            names.append(value)
    new = {}
    new["Phone Number"] = phones[0] if phones else ""
    new["Website"] = urls[0] if urls else ""
    street = streets[0] if streets else ""
    new["Address"] = street
    new["Clinic Name"] = names[0] if names else ""
    prose += names[1:] + streets[1:]
    new["Doctor's Name"] = ""
    merged = ([notes] + prose
              + [f"Also listed: {u}" for u in urls[1:]]
              + [f"Also: {p}" for p in phones[1:]])
    new["Notes"] = "\n".join(p for p in merged if p).strip()
    new["Location"] = resolve_location(loc)

    return {k: v for k, v in new.items() if v != normalise(fields.get(k, ""))}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--delete-empty", action="store_true")
    ap.add_argument("--delete-dupes", action="store_true")
    a = ap.parse_args()

    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://api.airtable.com/v0/meta/bases/{BASE}/tables", headers=H) as r:
            tables = (await r.json())["tables"]
        plan_updates, plan_deletes, plan_dupes = [], [], []

        dup_keys, review, dup_conflicts = {}, [], []
        for t in tables:
            recs = await fetch(s, t["id"])
            for rec in recs:
                f = rec["fields"]
                populated = {k for k, v in f.items() if str(v).strip()}
                if populated <= {"Location"}:
                    plan_deletes.append((t["name"], t["id"], rec["id"], f.get("Location","")))
                    continue
                patch = (fix_healthcare(f) if t["name"] == HEALTHCARE
                         else clean_row(f, t["name"], review))
                patch = {k: v for k, v in patch.items() if v != f.get(k, "")}
                if patch:
                    plan_updates.append((t["name"], t["id"], rec["id"], f, patch))
                merged = {**f, **patch}
                nm = normalise(str(merged.get(NAMECOL.get(t["name"], ""), ""))).lower()
                web = normalise(str(merged.get("Website", "")), is_url=True).lower().rstrip("/")
                if nm or web:
                    dup_keys.setdefault((t["id"], nm, web), []).append(
                        (t["name"], t["id"], rec["id"], merged))

        def _populated(fields):
            return {k: normalise(str(v)) for k, v in fields.items() if str(v).strip()}

        for key, rows in dup_keys.items():
            if len(rows) < 2:
                continue
            ranked = sorted(rows, key=lambda r: -len(_populated(r[3])))
            keeper = ranked[0]
            kf = _populated(keeper[3])
            for extra in ranked[1:]:
                ef = _populated(extra[3])
                clash = sorted(k for k, v in ef.items() if kf.get(k, "") != v)
                if clash:
                    dup_conflicts.append((extra[0], extra[2], keeper[2], clash))
                else:
                    plan_dupes.append((extra[0], extra[1], extra[2]))

        print(f"ROWS TO UPDATE : {len(plan_updates)}")
        print(f"DUPLICATE ROWS : {len(plan_dupes)}  (safe to delete, add nothing new)")
        print(f"DUPLICATE CLASH: {len(dup_conflicts)}  (NOT deleted, need a human)")
        print(f"EMPTY ROWS     : {len(plan_deletes)}  (Location only, nothing else)\n")

        hc = [p for p in plan_updates if p[0] == HEALTHCARE]
        print(f"--- {HEALTHCARE}: {len(hc)} rows re-seated (showing 3) ---")
        for name, tid, rid, before, patch in hc[:3]:
            print(f"\n  {rid}")
            for k in ("Doctor's Name","Clinic Name","Address","Website","Location","Notes"):
                b = str(before.get(k,""))[:52]
                if k in patch:
                    print(f"    {k:<14} {b!r}\n    {'':<14}  -> {str(patch[k])[:52]!r}")
        other = [p for p in plan_updates if p[0] != HEALTHCARE]
        print(f"\n--- other tables: {len(other)} rows cleaned (showing 5) ---")
        for name, tid, rid, before, patch in other[:5]:
            for k, v in list(patch.items())[:2]:
                print(f"  {name[:20]:<20} {k:<14} {str(before.get(k,''))[:38]!r} -> {str(v)[:38]!r}")

        if review:
            print(f"\n--- {len(review)} row(s) with an unparseable Location "
                  f"(kept in notes, NEEDS HUMAN REVIEW) ---")
            for table, val in review:
                print(f"  {table[:24]:<24} {val[:52]!r}")

        if dup_conflicts:
            print("\n--- duplicate groups that DISAGREE (never auto-deleted) ---")
            for table, extra_id, keeper_id, clash in dup_conflicts:
                print(f"  {table[:24]:<24} {extra_id} vs kept {keeper_id}")
                print(f"  {'':<24}   fields that differ: {', '.join(clash)}")

        if plan_dupes:
            print("\n--- duplicates (extras that would be removed) ---")
            for table, tid, rid in plan_dupes:
                print(f"  {table[:24]:<24} {rid}")

        if not a.commit:
            print("\nDRY RUN - nothing written. Re-run with --commit to apply updates.")
            if plan_deletes:
                print(f"         --delete-empty additionally removes the {len(plan_deletes)} empty rows.")
            if plan_dupes:
                print(f"         --delete-dupes additionally removes the {len(plan_dupes)} duplicate rows.")
            return

        done = 0
        for name, tid, rid, before, patch in plan_updates:
            async with s.patch(f"https://api.airtable.com/v0/{BASE}/{tid}/{rid}",
                               headers={**H, "Content-Type":"application/json"},
                               json={"fields": patch, "typecast": True},
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200: done += 1
                else: print(f"  FAILED {rid}: HTTP {r.status} {(await r.text())[:120]}")
            await asyncio.sleep(0.25)
        print(f"\nUPDATED {done} of {len(plan_updates)} rows")

        targets = []
        if a.delete_empty:
            targets += [(n, t, r) for n, t, r, _ in plan_deletes]
        if a.delete_dupes:
            targets += plan_dupes
        if targets:
            gone = 0
            for name, tid, rid in targets:
                async with s.delete(f"https://api.airtable.com/v0/{BASE}/{tid}/{rid}",
                                    headers=H, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 200: gone += 1
                await asyncio.sleep(0.25)
            print(f"DELETED {gone} of {len(targets)} rows")

asyncio.run(main())
