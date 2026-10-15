from pathlib import Path
"""Assert the remediation never drops content: every alphanumeric token present
before a row is patched must still be present somewhere in the row after."""
import asyncio,os,sys,re,importlib.util,aiohttp
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv; load_dotenv(Path(__file__).resolve().parents[1] / '.env')
spec=importlib.util.spec_from_file_location("rem",
  os.path.join(os.path.dirname(os.path.abspath(__file__)), "remediate_resources.py"))
sys.argv=["rem"]
rem=importlib.util.module_from_spec(spec)
import unittest.mock as m
with m.patch("asyncio.run", lambda *a, **k: None):
    spec.loader.exec_module(rem)
TOKEN,BASE=os.getenv("AIRTABLE_TOKEN"),os.getenv("AIRTABLE_BASE_ID")
H={"Authorization":f"Bearer {TOKEN}"}
TOK=re.compile(r"[a-z0-9]+")
SEARCH=re.compile(r"(google|bing|duckduckgo|yahoo)\.[a-z.]+/(search|url)\?",re.I)
DIGITS=re.compile(r"\d{7,}")
IGNORE={"https","http","www","tel","n","a","na","none","null","tbd","unknown"}
def toks(s): return {t for t in TOK.findall(str(s).lower()) if t not in IGNORE and len(t)>1}
async def fetch(s,tid):
    out,off=[],None
    while True:
        p={"pageSize":100}
        if off:p["offset"]=off
        async with s.get(f"https://api.airtable.com/v0/{BASE}/{tid}",headers=H,params=p) as r: d=await r.json()
        out+=d.get("records",[]); off=d.get("offset")
        if not off: return out
NAMECOL={"Affirming Homeless Shelters":"Shelter Name","Food Resources":"Organization Name",
 "Other Resources":"Organization Name","Affirming Hospitals":"Hospital Name",
 "Mental Health Resources":"Clinic Name","LGBTQ+ Centers":"Center Name",
 "Healthcare Resources":"Clinic Name","National Help":"Organization"}


async def main():
    bad=0; checked=0; groups={}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://api.airtable.com/v0/meta/bases/{BASE}/tables",headers=H) as r:
            tables=(await r.json())["tables"]
        for t in tables:
            for rec in await fetch(s,t["id"]):
                f=rec["fields"]
                if {k for k,v in f.items() if str(v).strip()}<={"Location"}: continue
                patch=rem.fix_healthcare(f) if t["name"]==rem.HEALTHCARE else rem.clean_row(f)
                patch={k:v for k,v in patch.items() if v!=f.get(k,"")}
                if not patch: continue
                checked+=1
                before=set().union(*(toks(v) for v in f.values())) if f else set()
                after=set().union(*(toks(v) for v in {**f,**patch}.values()))
                lost=before-after
                raw="  ".join(str(v) for v in f.values())
                after_digits={re.sub(r"\D","",d)[-10:] for d in
                              DIGITS.findall(" ".join(str(v) for v in {**f,**patch}.values()))}
                after_digits|={re.sub(r"\D","",str(v))[-10:] for v in {**f,**patch}.values()
                               if sum(c.isdigit() for c in str(v))>=7}
                lost={t for t in lost if not (t.isdigit() and t[-10:] in after_digits)}
                if SEARCH.search(raw):
                    junk=set()
                    for v in f.values():
                        for m in SEARCH.finditer(str(v)):
                            seg=str(v)[m.start():]
                            junk|=toks(seg.split(")")[0])
                    lost-=junk
                lost={t for t in lost
                      if not any(a != t and a.startswith(t) and len(a)-len(t) <= 2
                                 for a in after)}
                if lost:
                    bad+=1
                    print(f"  LOST in {t['name']}/{rec['id']}: {sorted(lost)[:12]}")
                merged={**f,**patch}
                nm=rem.normalise(str(merged.get(NAMECOL.get(t["name"],""),""))).lower()
                web=rem.normalise(str(merged.get("Website","")),is_url=True).lower().rstrip("/")
                if nm or web:
                    groups.setdefault((t["id"],nm,web),[]).append((t["name"],rec["id"],merged))
    dele=0; dbad=0
    for key,rows in groups.items():
        if len(rows)<2: continue
        ranked=sorted(rows,key=lambda r:-len([k for k,v in r[2].items() if str(v).strip()]))
        keep=ranked[0]
        keep_toks=set().union(*(toks(v) for v in keep[2].values()))
        for extra in ranked[1:]:
            extra_toks=set().union(*(toks(v) for v in extra[2].values()))
            lost=extra_toks-keep_toks
            if lost:
                print(f"  CONFLICT {extra[0]}/{extra[1]} holds {sorted(lost)[:8]} "
                      f"absent from kept {keep[1]} - must NOT be auto-deleted")
                dbad+=1
            else:
                dele+=1

    print(f"\nchecked {checked} patched rows; {bad} with lost tokens")
    print(f"checked {dele+dbad} duplicate extras; {dele} safe to delete, {dbad} conflicting")
    print("PASS - remediation is lossless" if not bad else "FAIL")
asyncio.run(main())
