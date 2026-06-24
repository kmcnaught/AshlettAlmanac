#!/usr/bin/env python3
"""
Pull eBird per-hotspot species lists for the Ashlett-cluster hotspots, plus
the eBird taxonomy needed to resolve scientific names and stable species-page
URLs (https://ebird.org/species/{speciesCode}).

Why this exists: goingbirding gives us record-level monthly tallies, but eBird
is the canonical source for the per-hotspot species checklist (lifetime list,
curated). Used as cross-reference for the goingbirding data ("yes, this bird
is on record at Ashlett Green") and to provide species-page links with photos
distinct from goingbirding's species pages.

eBird API key is loaded from /workspace/.env (EBIRD_API_KEY=…).

Args:
    --hotspots ID,ID,... eBird locIds (default: Ashlett Green + Calshot pair)
    --delay SECONDS      inter-request delay (default: 1.0)
    --out FILE           output JSON (default: data/ebird_hotspots.json)

Example:
    python3 scripts/scrape_ebird.py
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

UA = "ashlett-almanac-research/0.1 (personal; contact kirsty.mcnaught@gmail.com)"
API = "https://api.ebird.org/v2"

DEFAULT_HOTSPOTS = {
    "L12499113": "New Forest NP — Ashlett Green and Creek",
    "L2719654": "Calshot Marshes LNR",
    "L28743923": "Calshot Spit",
}


def load_env(path: str = "/workspace/.env") -> dict:
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def fetch(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "X-eBirdApiToken": token,
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hotspots", default=",".join(DEFAULT_HOTSPOTS))
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--out", default="data/ebird_hotspots.json")
    args = p.parse_args()

    env = load_env()
    token = env.get("EBIRD_API_KEY") or os.environ.get("EBIRD_API_KEY")
    if not token:
        print("EBIRD_API_KEY not found in /workspace/.env or environment", file=sys.stderr)
        sys.exit(2)

    loc_ids = [x.strip() for x in args.hotspots.split(",") if x.strip()]

    # Per-hotspot species lists. Each call returns a list of eBird species codes.
    per_hotspot: dict[str, list[str]] = {}
    all_codes: set[str] = set()
    for i, lid in enumerate(loc_ids):
        url = f"{API}/product/spplist/{lid}"
        print(f"[{i+1}/{len(loc_ids)}] spplist {lid}", file=sys.stderr)
        body = fetch(url, token)
        codes = json.loads(body)
        per_hotspot[lid] = codes
        all_codes.update(codes)
        print(f"  → {len(codes)} species", file=sys.stderr)
        if i < len(loc_ids) - 1:
            time.sleep(args.delay)

    # One taxonomy call, scoped to just the species we saw, gives common+scientific names.
    print(f"[taxonomy] resolving {len(all_codes)} species codes", file=sys.stderr)
    time.sleep(args.delay)
    tax_url = f"{API}/ref/taxonomy/ebird?fmt=json&species={','.join(sorted(all_codes))}"
    tax_body = fetch(tax_url, token)
    tax_entries = json.loads(tax_body)
    tax_map = {t["speciesCode"]: t for t in tax_entries}

    species_out = []
    for code in sorted(all_codes):
        t = tax_map.get(code, {})
        species_out.append({
            "speciesCode": code,
            "commonName": t.get("comName"),
            "scientificName": t.get("sciName"),
            "family": t.get("familyComName"),
            "url": f"https://ebird.org/species/{code}",
            "hotspots": sorted([lid for lid, codes in per_hotspot.items() if code in codes]),
        })

    out = {
        "meta": {
            "source": "ebird.org",
            "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "hotspots": [{"locId": lid, "name": DEFAULT_HOTSPOTS.get(lid, "?"), "url": f"https://ebird.org/hotspot/{lid}", "speciesCount": len(per_hotspot.get(lid, []))} for lid in loc_ids],
            "totalSpecies": len(all_codes),
        },
        "species": species_out,
        "byHotspot": per_hotspot,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Wrote {args.out}: {len(all_codes)} species across {len(loc_ids)} hotspots", file=sys.stderr)


if __name__ == "__main__":
    main()
