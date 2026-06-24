#!/usr/bin/env python3
"""
Scrape iNaturalist research-grade observations for the west shore of
Southampton Water around Ashlett Creek, and aggregate to per-month
species frequency.

Why this exists: goingbirding/eBird cover birds beautifully but no non-birds.
iNaturalist is the open-API plug for plants, insects, intertidal life, fungi —
the bits that give the wildlife slot variety. The tight 5 km box (50.79–50.87,
-1.40 to -1.32) deliberately excludes the Hamble/Warsash side of Southampton
Water; the eastern boundary -1.32 puts the line mid-channel.

Args:
    --start YYYY-MM-DD  range start (default: 12 months ago)
    --end YYYY-MM-DD    range end (default: today)
    --delay SECONDS     inter-request delay (default: 1.0)
    --out FILE          output JSON (default: data/inaturalist_obs.json)

Example:
    python3 scripts/scrape_inaturalist.py
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta

UA = "ashlett-almanac-research/0.1 (personal; contact kirsty.mcnaught@gmail.com)"
API = "https://api.inaturalist.org/v1/observations"

# Tight west-shore-only box around Ashlett Creek (50.83, -1.34).
# Eastern edge -1.32 puts the boundary mid-Southampton-Water, excluding Hamble/Warsash.
BOX = {"swlat": 50.79, "swlng": -1.40, "nelat": 50.87, "nelng": -1.32}


def fetch_page(start: str, end: str, page: int, per_page: int = 200) -> dict:
    params = {
        **BOX,
        "quality_grade": "research",
        "d1": start,
        "d2": end,
        "per_page": per_page,
        "page": page,
        "order_by": "observed_on",
        "order": "asc",
    }
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalise(obs: dict) -> dict:
    taxon = obs.get("taxon") or {}
    return {
        "id": obs["id"],
        "url": f"https://www.inaturalist.org/observations/{obs['id']}",
        "observed_on": obs.get("observed_on"),
        "month": int(obs["observed_on"].split("-")[1]) if obs.get("observed_on") else None,
        "taxonId": taxon.get("id"),
        "taxonUrl": f"https://www.inaturalist.org/taxa/{taxon['id']}" if taxon.get("id") else None,
        "scientificName": taxon.get("name"),
        "commonName": taxon.get("preferred_common_name"),
        "iconicTaxon": taxon.get("iconic_taxon_name"),
        "rank": taxon.get("rank"),
        "place_guess": obs.get("place_guess"),
        "lat": (obs.get("geojson") or {}).get("coordinates", [None, None])[1],
        "lng": (obs.get("geojson") or {}).get("coordinates", [None, None])[0],
        "user": (obs.get("user") or {}).get("login"),
        "photoUrl": (obs.get("photos") or [{}])[0].get("url"),
    }


def aggregate(records: list[dict]) -> dict:
    by_month: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in records:
        if not r["month"] or not r["taxonId"]:
            continue
        mkey = f"{r['month']:02d}"
        spp = by_month[mkey].setdefault(r["taxonId"], {
            "taxonId": r["taxonId"],
            "scientificName": r["scientificName"],
            "commonName": r["commonName"],
            "iconicTaxon": r["iconicTaxon"],
            "rank": r["rank"],
            "url": r["taxonUrl"],
            "recordCount": 0,
            "dates": set(),
            "places": set(),
            "samplePhotoUrl": None,
            "sampleObsUrl": None,
        })
        spp["recordCount"] += 1
        spp["dates"].add(r["observed_on"])
        if r["place_guess"]:
            spp["places"].add(r["place_guess"])
        if not spp["samplePhotoUrl"] and r["photoUrl"]:
            spp["samplePhotoUrl"] = r["photoUrl"]
            spp["sampleObsUrl"] = r["url"]

    out = {}
    for mkey, spp_map in sorted(by_month.items()):
        out[mkey] = sorted(
            (
                {
                    **{k: v for k, v in s.items() if k not in ("dates", "places")},
                    "dateCount": len(s["dates"]),
                    "places": sorted(s["places"])[:5],
                }
                for s in spp_map.values()
            ),
            key=lambda x: (-x["recordCount"], x["scientificName"] or ""),
        )
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default=(date.today() - timedelta(days=365)).isoformat())
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--out", default="data/inaturalist_obs.json")
    args = p.parse_args()

    all_records: list[dict] = []
    page = 1
    while True:
        print(f"[page {page}] fetching…", file=sys.stderr)
        data = fetch_page(args.start, args.end, page)
        total = data.get("total_results", 0)
        results = data.get("results", [])
        all_records.extend(normalise(o) for o in results)
        print(f"  → {len(results)} obs (running total {len(all_records)} / {total})", file=sys.stderr)
        if len(results) < 200 or len(all_records) >= total:
            break
        page += 1
        time.sleep(args.delay)

    by_month = aggregate(all_records)

    out = {
        "meta": {
            "source": "inaturalist.org",
            "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "dateRange": [args.start, args.end],
            "box": BOX,
            "totalRecords": len(all_records),
        },
        "byMonth": by_month,
        "records": all_records,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Wrote {args.out}: {len(all_records)} obs, {sum(len(v) for v in by_month.values())} species×month rows", file=sys.stderr)


if __name__ == "__main__":
    main()
