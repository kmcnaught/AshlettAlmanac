#!/usr/bin/env python3
"""
Scrape Hampshire Ornithological Society sightings (goingbirding.co.uk/hants)
for a configurable set of west-shore Southampton Water sites, and produce
a per-month species tally suitable for the almanac's wildlife slot.

Why this exists: goingbirding has Ashlett Creek itself as site_id=15 with
day-stamped, observer-attributed records. NBN/eBird are useful but no other
source resolves to the actual creek. We pull a year per site, aggregate by
calendar month, and link each species back to its species.aspx page so the
recipient can tap through to photos/notes.

Politeness: one full-year request per site (no month-by-month fan-out),
configurable inter-site delay, identifying User-Agent.

Args:
    --sites ID,ID,...   site_ids to scrape (default: west-shore cluster)
    --start YYYY-MM-DD  range start (default: 12 months ago)
    --end YYYY-MM-DD    range end (default: today)
    --delay SECONDS     inter-request delay (default: 1.0)
    --chunk-days N      split range into N-day chunks (default: 90 — the site caps
                        a single response at ~200 records with no pagination param,
                        so dense sites need chunking to capture a full year)
    --out FILE          output JSON (default: data/goingbirding_birds.json)

Example:
    python3 scripts/scrape_goingbirding.py
    python3 scripts/scrape_goingbirding.py --sites 15,16,119 --delay 2
"""
import argparse
import html
import json
import re
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta

UA = "ashlett-almanac-research/0.1 (personal; contact kirsty.mcnaught@gmail.com)"
BASE = "https://www.goingbirding.co.uk/hants"
URL_TMPL = (
    BASE + "/birdnews.aspx?search=true&site_search=1&date_search=2"
    "&site_id={site_id}&date_from={start}&date_to={end}"
)

# West-shore Southampton Water cluster — same shore as Ashlett, broadly same habitat.
# Excludes Hamble-side / opposite-bank sites by design.
DEFAULT_SITES = {
    15: "Ashlett Creek",
    16: "Ashlett Mill Pond",
    119: "Calshot",
    120: "Calshot Spit",
    245: "Fawley Refinery",
}

# A record row is <tr> ... </tr> followed by an optional <tr class="...notes">
# carrying time + free-text notes. We capture both in one swoop.
RECORD_RE = re.compile(
    r'<tr[^>]*>\s*'
    r'<td>(?P<date>\d{2}/\d{2}/\d{2})</td>\s*'
    r'<td><a href="species\.aspx\?species_id=(?P<species_id>\d+)">(?P<species>[^<]+)</a></td>\s*'
    r'<td><a href="site\.aspx\?site_id=(?P<site_id>\d+)">(?P<site>[^<]+)</a></td>\s*'
    r'<td>(?P<count>[^<]*)</td>\s*'
    r'<td><a href="observer\.aspx\?observer_id=\d+"[^>]*>(?P<observer>[^<]+)</a></td>\s*'
    r'</tr>'
    r'(?:\s*<tr[^>]*[Cc]lass="[^"]*notes[^"]*"[^>]*>\s*'
    r'<td>(?P<time>.*?)</td>\s*<td[^>]*>(?P<notes>.*?)</td>\s*</tr>)?',
    re.DOTALL | re.IGNORECASE,
)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_records(body: str) -> list[dict]:
    out = []
    for m in RECORD_RE.finditer(body):
        d = m.group("date")  # DD/MM/YY
        dd, mm, yy = d.split("/")
        iso = f"20{yy}-{mm}-{dd}"
        # Count cell can be a bare number, blank, or "20+"-style — keep raw and parsed.
        raw_count = (m.group("count") or "").strip()
        try:
            count = int(re.match(r"\d+", raw_count).group(0))
        except (AttributeError, ValueError):
            count = None
        notes_raw = (m.group("notes") or "").strip()
        notes = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", notes_raw))).strip()
        time_str = (m.group("time") or "").strip().rstrip(" ")
        time_str = re.sub(r"<[^>]+>", "", time_str).strip()
        out.append({
            "iso": iso,
            "month": int(mm),
            "speciesId": int(m.group("species_id")),
            "species": html.unescape(m.group("species")).strip(),
            "siteId": int(m.group("site_id")),
            "site": html.unescape(m.group("site")).strip(),
            "count": count,
            "countRaw": raw_count or None,
            "observer": html.unescape(m.group("observer")).strip(),
            "time": time_str or None,
            "notes": notes or None,
        })
    return out


def aggregate(records: list[dict]) -> dict:
    """Group by calendar month → species → counts + sites + sample observers."""
    by_month: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in records:
        mkey = f"{r['month']:02d}"
        spp = by_month[mkey].setdefault(r["speciesId"], {
            "speciesId": r["speciesId"],
            "species": r["species"],
            "url": f"{BASE}/species.aspx?species_id={r['speciesId']}",
            "recordCount": 0,
            "totalCount": 0,
            "sites": defaultdict(int),
            "dates": set(),
            "observers": set(),
            "sampleNote": None,
        })
        spp["recordCount"] += 1
        spp["totalCount"] += r["count"] or 0
        spp["sites"][r["site"]] += 1
        spp["dates"].add(r["iso"])
        spp["observers"].add(r["observer"])
        if not spp["sampleNote"] and r["notes"]:
            spp["sampleNote"] = r["notes"]

    # Finalise: turn sets/defaultdicts into JSON-ready primitives.
    out = {}
    for mkey, spp_map in sorted(by_month.items()):
        out[mkey] = sorted(
            (
                {
                    **{k: v for k, v in s.items() if k not in ("sites", "dates", "observers")},
                    "sites": dict(s["sites"]),
                    "dateCount": len(s["dates"]),
                    "firstDate": min(s["dates"]),
                    "lastDate": max(s["dates"]),
                    "observerCount": len(s["observers"]),
                }
                for s in spp_map.values()
            ),
            key=lambda x: (-x["recordCount"], x["species"]),
        )
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sites", default=",".join(str(k) for k in DEFAULT_SITES))
    p.add_argument("--start", default=(date.today() - timedelta(days=365)).isoformat())
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--chunk-days", type=int, default=90)
    p.add_argument("--out", default="data/goingbirding_birds.json")
    args = p.parse_args()

    site_ids = [int(x) for x in args.sites.split(",") if x.strip()]
    all_records: list[dict] = []
    per_site_counts: dict[int, int] = {}
    seen_keys: set[tuple] = set()  # dedup across chunk boundaries

    range_start = date.fromisoformat(args.start)
    range_end = date.fromisoformat(args.end)

    chunks: list[tuple[date, date]] = []
    cur = range_start
    while cur <= range_end:
        end = min(cur + timedelta(days=args.chunk_days - 1), range_end)
        chunks.append((cur, end))
        cur = end + timedelta(days=1)

    total_requests = len(site_ids) * len(chunks)
    n = 0
    for sid in site_ids:
        per_site_counts[sid] = 0
        for (a, b) in chunks:
            n += 1
            url = URL_TMPL.format(site_id=sid, start=a.isoformat(), end=b.isoformat())
            print(f"[{n}/{total_requests}] site_id={sid} ({DEFAULT_SITES.get(sid, '?')}) {a} → {b}", file=sys.stderr)
            body = fetch(url)
            recs = parse_records(body)
            added = 0
            for r in recs:
                key = (r["iso"], r["speciesId"], r["siteId"], r["observer"], r["time"], r["countRaw"])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_records.append(r)
                added += 1
            per_site_counts[sid] += added
            if len(recs) >= 200:
                print(f"  → {len(recs)} records (HIT 200 CAP — narrow --chunk-days)", file=sys.stderr)
            else:
                print(f"  → {len(recs)} records, +{added} new", file=sys.stderr)
            if n < total_requests:
                time.sleep(args.delay)

    by_month = aggregate(all_records)

    out = {
        "meta": {
            "source": "goingbirding.co.uk/hants",
            "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "dateRange": [args.start, args.end],
            "sites": [{"id": sid, "name": DEFAULT_SITES.get(sid, "?"), "url": f"{BASE}/site.aspx?site_id={sid}", "recordCount": per_site_counts.get(sid, 0)} for sid in site_ids],
            "totalRecords": len(all_records),
        },
        "byMonth": by_month,
        "records": all_records,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Wrote {args.out}: {len(all_records)} records, {sum(len(v) for v in by_month.values())} species×month rows", file=sys.stderr)


if __name__ == "__main__":
    main()
