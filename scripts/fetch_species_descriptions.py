#!/usr/bin/env python3
"""
Fetch a one-paragraph description and a default photo URL for every species
in our pool, via the iNaturalist /v1/taxa endpoint.

Why this exists: the picker needs a fallback leadLine when there is no
observer quote or manual note — currently it surfaces internal seasonality
category labels ("Effectively year-round at the creek") which read poorly.
iNat returns the Wikipedia first paragraph as `wikipedia_summary`, which
gives us an appearance / range / behaviour line suitable for surfacing.
Same call returns `default_photo.medium_url` for thumbnails on bedrock
species that have no observation photos of their own.

Pool: union of canonical bird names from species_canonical.json,
non-bird taxa from seasonality.json (which already carries iNat taxonIds
from records), bedrock plants, bedrock animals.

Strategy:
  - For species we already have an iNat taxonId for (any iNat-recorded
    species): bulk lookup by ID (up to 30 per request).
  - For species without (most birds, all bedrock): search by scientific
    name, take the species-rank match.
  - Rate limit: 1.0 s between requests by default (iNat's published cap is
    ≤1/s unauthenticated).

Args:
    --seasonality FILE   (default: data/seasonality.json)
    --canonical FILE     (default: data/species_canonical.json)
    --bedrock-plants FILE  (default: data/saltmarsh_bedrock.json)
    --bedrock-animals FILE (default: data/saltmarsh_bedrock_animals.json)
    --inat FILE          (default: data/inaturalist_obs.json)
    --delay SECONDS      (default: 1.0)
    --batch INT          (default: 30 — max IDs per bulk lookup)
    --out FILE           (default: data/species_descriptions.json)

Example:
    python3 scripts/fetch_species_descriptions.py
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime

UA = "ashlett-almanac-research/0.1 (personal; contact kirsty.mcnaught@gmail.com)"
API = "https://api.inaturalist.org/v1/taxa"


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def clean_summary(s: str | None) -> str | None:
    """iNat returns wikipedia_summary as HTML-ish — strip tags and trim."""
    if not s:
        return None
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def extract(taxon: dict) -> dict:
    photo = taxon.get("default_photo") or {}
    return {
        "inatTaxonId": taxon.get("id"),
        "inatUrl": f"https://www.inaturalist.org/taxa/{taxon['id']}" if taxon.get("id") else None,
        "scientificName": taxon.get("name"),
        "commonName": taxon.get("preferred_common_name"),
        "rank": taxon.get("rank"),
        "iconicTaxon": taxon.get("iconic_taxon_name"),
        "wikipediaUrl": taxon.get("wikipedia_url"),
        "wikipediaSummary": clean_summary(taxon.get("wikipedia_summary")),
        "defaultPhotoSquare": photo.get("square_url"),
        "defaultPhotoMedium": photo.get("medium_url"),
        "defaultPhotoAttribution": photo.get("attribution"),
    }


def search_by_sciname(sci_name: str) -> dict | None:
    q = urllib.parse.urlencode({"q": sci_name, "rank": "species", "per_page": 5, "is_active": "true"})
    data = fetch(f"{API}?{q}")
    results = data.get("results", [])
    # Prefer an exact sciName match if present.
    for r in results:
        if r.get("name", "").lower() == sci_name.lower():
            return r
    return results[0] if results else None


def bulk_by_ids(ids: list[int]) -> dict[int, dict]:
    """Use the path-based /v1/taxa/{ids} endpoint — returns FULL records
    including wikipedia_summary. The query-based /v1/taxa?taxon_id=... returns
    abbreviated records (no summary) which is why we don't use it here."""
    if not ids:
        return {}
    path = ",".join(str(i) for i in ids)
    data = fetch(f"{API}/{path}")
    return {r["id"]: r for r in data.get("results", []) if r.get("id")}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seasonality", default="data/seasonality.json")
    p.add_argument("--canonical", default="data/species_canonical.json")
    p.add_argument("--bedrock-plants", default="data/saltmarsh_bedrock.json")
    p.add_argument("--bedrock-animals", default="data/saltmarsh_bedrock_animals.json")
    p.add_argument("--inat", default="data/inaturalist_obs.json")
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--batch", type=int, default=30)
    p.add_argument("--out", default="data/species_descriptions.json")
    args = p.parse_args()

    seas = json.load(open(args.seasonality))
    canon = json.load(open(args.canonical))
    bp = json.load(open(args.bedrock_plants))
    ba = json.load(open(args.bedrock_animals))
    inat = json.load(open(args.inat))

    # Build the work list: { canonical: {sciName, knownInatId} }
    # Prefer the canonical name from species_canonical.json for birds; fall back
    # to the seasonality canonical name otherwise.
    work: dict[str, dict] = {}

    # Birds: canonical entries — sci name from eBird taxonomy via species_canonical.
    for s in canon["species"]:
        if s.get("exclude"):
            continue
        sci = s.get("scientificName")
        if not sci:
            # GB_SCI_FALLBACK additions live only in seasonality script; copy them here.
            fallback = {"Dartford Warbler": "Curruca undata", "Northern Goshawk": "Accipiter gentilis", "Lesser Redpoll": "Acanthis cabaret"}
            sci = fallback.get(s["canonical"])
        if sci:
            work[s["canonical"]] = {"scientificName": sci, "knownInatId": None}

    # Non-bird species from seasonality (these came from iNat, so we already have taxonId).
    canonical_taxon_id: dict[str, int] = {}
    for o in inat["records"]:
        c = o.get("commonName") or o.get("scientificName")
        if c and o.get("taxonId") and c not in canonical_taxon_id:
            canonical_taxon_id[c] = o["taxonId"]
    for s in seas["species"]:
        if s.get("iconicTaxon") == "Aves":
            continue
        if s["canonical"] in work:
            continue
        sci = s.get("scientificName")
        tid = canonical_taxon_id.get(s["canonical"])
        if sci or tid:
            work[s["canonical"]] = {"scientificName": sci, "knownInatId": tid}

    # Bedrock plants
    for s in bp["species"]:
        if s["canonical"] not in work and s.get("scientificName"):
            work[s["canonical"]] = {"scientificName": s["scientificName"], "knownInatId": None}

    # Bedrock animals
    for s in ba["species"]:
        if s["canonical"] not in work and s.get("scientificName"):
            work[s["canonical"]] = {"scientificName": s["scientificName"], "knownInatId": None}

    print(f"Work list: {len(work)} species", file=sys.stderr)
    print(f"  with known iNat taxonId: {sum(1 for v in work.values() if v['knownInatId'])}", file=sys.stderr)
    print(f"  needing sci-name search: {sum(1 for v in work.values() if not v['knownInatId'])}", file=sys.stderr)

    out: dict[str, dict] = {}
    fail: list[str] = []

    # Phase 1 — bulk fetch by known ID, batched.
    known_id_canonicals = [k for k, v in work.items() if v["knownInatId"]]
    id_to_canonicals: dict[int, list[str]] = defaultdict(list)
    for k in known_id_canonicals:
        id_to_canonicals[work[k]["knownInatId"]].append(k)
    all_ids = list(id_to_canonicals.keys())
    n = 0
    for i in range(0, len(all_ids), args.batch):
        chunk = all_ids[i:i + args.batch]
        n += 1
        print(f"[phase 1 batch {n}] bulk fetch {len(chunk)} IDs", file=sys.stderr)
        results = bulk_by_ids(chunk)
        for tid in chunk:
            t = results.get(tid)
            if t:
                ex = extract(t)
                for canonical in id_to_canonicals[tid]:
                    out[canonical] = ex
            else:
                for canonical in id_to_canonicals[tid]:
                    fail.append(canonical)
        if i + args.batch < len(all_ids):
            time.sleep(args.delay)

    # Phase 2 — sci-name search for everything else (gets taxon IDs only).
    to_search = [k for k in work if k not in out]
    print(f"[phase 2] sci-name search for {len(to_search)} species", file=sys.stderr)
    phase2_canonical_id: dict[str, int] = {}
    for i, canonical in enumerate(to_search):
        time.sleep(args.delay)
        sci = work[canonical]["scientificName"]
        if not sci:
            fail.append(canonical)
            continue
        try:
            t = search_by_sciname(sci)
        except Exception as e:
            print(f"  [{i+1}/{len(to_search)}] {canonical} ({sci}): ERROR {e}", file=sys.stderr)
            fail.append(canonical)
            continue
        if not t:
            print(f"  [{i+1}/{len(to_search)}] {canonical} ({sci}): no result", file=sys.stderr)
            fail.append(canonical)
            continue
        phase2_canonical_id[canonical] = t["id"]
        if (i + 1) % 30 == 0:
            print(f"  [{i+1}/{len(to_search)}] {canonical} → id={t['id']}", file=sys.stderr)

    # Phase 3 — bulk-fetch phase-2 IDs via path-based endpoint for full records.
    phase2_ids = list(set(phase2_canonical_id.values()))
    print(f"[phase 3] enriching {len(phase2_ids)} phase-2 IDs for wikipedia_summary", file=sys.stderr)
    id_to_full: dict[int, dict] = {}
    for i in range(0, len(phase2_ids), args.batch):
        chunk = phase2_ids[i:i + args.batch]
        n_batch = i // args.batch + 1
        print(f"  [batch {n_batch}] {len(chunk)} IDs", file=sys.stderr)
        results = bulk_by_ids(chunk)
        id_to_full.update(results)
        if i + args.batch < len(phase2_ids):
            time.sleep(args.delay)
    for canonical, tid in phase2_canonical_id.items():
        t = id_to_full.get(tid)
        if t:
            out[canonical] = extract(t)
        else:
            fail.append(canonical)

    payload = {
        "_meta": {
            "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "source": "iNaturalist /v1/taxa (wikipedia_summary, default_photo)",
            "totalRequested": len(work),
            "successCount": len(out),
            "failureCount": len(fail),
            "failures": fail[:50],
        },
        "byCanonical": out,
    }
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"Wrote {args.out}: {len(out)} ok / {len(fail)} failed", file=sys.stderr)


if __name__ == "__main__":
    main()
