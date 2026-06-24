#!/usr/bin/env python3
"""
Build a precomputed wildlife.json — one entry per ISO date for the next N days,
ready to drop into the almanac's "in the saltmarsh now" slot.

Three things the picker takes a hard line on:
  1. Group balance: weighted random with a target taxonomic distribution so
     birds (which dominate the data 70:30) don't drown out plants and inverts.
  2. Date-deterministic: same date = same pick across re-runs (date-seeded RNG).
  3. Variety memory: each pick looks back at the previous days' picks already
     written into the output; recently-shown species get a usage-decay penalty
     so we don't see Eurasian Curlew two days running.

What it surfaces:
  - record-anchored entries with a quoted observer note + dated attribution,
    when a species has been logged near this date in the past 12 months
    (the J G Ross 'Never heard 3 singing here before' kind of line)
  - seasonal-anchored entries for bedrock species in flower / on the wing
  - year-round entries for the always-here bedrock animals when nothing
    seasonally specific dominates

Skipped: feral species (Black Swan, Egyptian Goose) and anything carrying
`exclude: true` in species_canonical.json (the grackles).

Args:
    --days N             Number of days forward to generate (default: 30)
    --start YYYY-MM-DD   Anchor date (default: today)
    --target-birds FLOAT   (default: 0.55)
    --target-plants FLOAT  (default: 0.20)
    --target-insects FLOAT (default: 0.10)
    --target-others FLOAT  (default: 0.15)
    --decay-halflife INT   Days. After which a recently-shown species recovers
                           to ~63% of its weight (default: 14).
    --out FILE             (default: data/wildlife.json)

Example:
    python3 scripts/build_wildlife.py
"""
import argparse
import hashlib
import json
import math
import random
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

# ----- circular smoothing reused from seasonality script -----
N_BINS = 52


def doy_of(d: date) -> int:
    return d.timetuple().tm_yday


def week_bin(d: int) -> int:
    return min(N_BINS - 1, (d - 1) * N_BINS // 365)


def circ_dist(i: int, j: int, n: int) -> int:
    return min(abs(i - j), n - abs(i - j))


def wrapped_gaussian_kernel(sigma: float, n: int = N_BINS) -> list[float]:
    k = [math.exp(-0.5 * (circ_dist(0, i, n) / sigma) ** 2) for i in range(n)]
    s = sum(k)
    return [x / s for x in k]


def smooth_at(days: list[int], target_week: int, kernel: list[float]) -> float:
    """Smoothed density at target_week for a species' days-of-year — wrapped Gaussian over 52 weekly bins."""
    if not days:
        return 0.0
    bins = [0.0] * N_BINS
    for d in days:
        bins[week_bin(d)] += 1
    acc = 0.0
    n = len(bins)
    for j in range(n):
        acc += bins[j] * kernel[(target_week - j) % n]
    # Normalise to [0, 1] by dividing by the species' max smoothed density.
    out = [0.0] * n
    for i in range(n):
        x = 0.0
        for j in range(n):
            x += bins[j] * kernel[(i - j) % n]
        out[i] = x
    mx = max(out)
    return acc / mx if mx > 0 else 0.0


# ----- group classification -----
GROUP_MAP = {
    "Aves": "birds",
    "Plantae": "plants",
    "Insecta": "insects",
}


def group_for(c: dict) -> str:
    return GROUP_MAP.get(c.get("iconicTaxon"), "others")


# ----- score components -----
def rarity_sweet(rarity: float | None) -> float:
    """Bell curve peaked at rarity=0.4. None = treat as in the sweet spot."""
    if rarity is None:
        return 1.0
    return math.exp(-((rarity - 0.4) / 0.25) ** 2)


def usage_decay(canonical: str, picks: dict, target_date: date, halflife: int) -> float:
    """1 - exp(-Δ/halflife) — recently-shown species recover smoothly."""
    last: date | None = None
    for iso, entry in picks.items():
        if entry["canonical"] == canonical:
            d = date.fromisoformat(iso)
            if d < target_date and (last is None or d > last):
                last = d
    if last is None:
        return 1.0
    delta = (target_date - last).days
    return 0.05 + 0.95 * (1 - math.exp(-delta / halflife))


def season_match_score(c: dict, target_doy: int, target_month: int, target_week: int, kernel: list[float]) -> float:
    """Cross-source season match in [0, 1]."""
    src = c["source"]
    if src == "records":
        # Smoothed density at target week, normalised to species max.
        return smooth_at(c.get("days", []), target_week, kernel)
    season = c.get("season") or {}
    if src == "bedrock-plant":
        if target_month in season.get("flowering", []):
            return 1.0
        if target_month in season.get("visible", []):
            return 0.4  # off-peak fallback — present and recognisable but not in flower
        return 0.0
    if src == "bedrock-animal":
        if target_month in season.get("active", []):
            return 1.0
        return 0.0
    return 0.0


# ----- framing / eyebrow / lead-line -----
MONTHS = ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"]

WARBLERS = {
    "Cetti's Warbler", "Common Reed Warbler", "Sedge Warbler",
    "Common Chiffchaff", "Willow Warbler", "Eurasian Blackcap",
    "Garden Warbler", "Lesser Whitethroat", "Greater Whitethroat",
    "Dartford Warbler", "Common Firecrest", "Common Grasshopper Warbler",
}


def pick_framing(c: dict, target_month: int) -> str:
    """Bedrock with current-month-in-flower/active = seasonal; bedrock off-peak = year-round; records = record-anchored."""
    src = c["source"]
    if src == "records":
        return "record-anchored"
    season = c.get("season") or {}
    # Bedrock plants stay seasonal-anchored even when visible-but-not-flowering
    # (matches the JS picker — "year-round" framing reserved for bedrock animals).
    if src == "bedrock-plant":
        return "seasonal-anchored"
    if src == "bedrock-animal":
        return "seasonal-anchored" if target_month in season.get("active", []) else "year-round"
    return "year-round"


def make_eyebrow(c: dict, framing: str, target_month: int) -> str:
    iconic = c.get("iconicTaxon")
    plant_zone = c.get("plant_zone")
    animal_zone = c.get("animal_zone")
    canonical = c.get("canonical")
    peakiness = c.get("peakinessAdj") or 0
    month_name = MONTHS[target_month - 1]

    if framing == "record-anchored":
        if peakiness > 0.20:
            return "What to spot passing through around now"
        if iconic == "Aves" and canonical in WARBLERS:
            return "What to listen for at the creek"
        if iconic == "Aves":
            return "What to look for at the creek"
        if iconic == "Mammalia":
            return "What to spot on the water"
        return "What to spot at the creek"

    if framing == "seasonal-anchored":
        if iconic == "Plantae":
            if plant_zone == "lower-marsh":
                return "What to spot on the lower marsh now"
            if plant_zone == "mid-marsh":
                return "What to spot in the saltmarsh now"
            if plant_zone == "upper-marsh":
                return "What to spot at the upper edge now"
            if plant_zone == "strandline":
                return "What to spot along the strandline now"
            return f"What to spot in flower in {month_name}"
        if iconic == "Insecta":
            return f"What to spot on the wing in {month_name}"
        if animal_zone == "open-water":
            return "What to spot on the water now"
        if animal_zone == "strandline":
            return "What to spot above the tideline now"
        if animal_zone == "mudflat":
            return "What to spot on the mud now"
        if animal_zone == "creek-channel":
            return "What to spot in the channel now"
        if animal_zone == "intertidal-hard":
            return "What to spot on the breakwater now"
        if animal_zone == "saltmarsh-surface":
            return f"What to spot in the saltmarsh in {month_name}"
        return f"What to spot in {month_name}"

    # year-round
    if animal_zone == "open-water":
        return "What to spot on the water"
    if animal_zone == "mudflat":
        return "What to spot on the mud at low water"
    if animal_zone == "creek-channel":
        return "What to spot in the channel"
    if animal_zone == "intertidal-hard":
        return "What to spot on the breakwater"
    if animal_zone == "strandline":
        return "What to spot at the tideline"
    return "What to spot at the creek"


# Same priority as the JS picker — auto-seasonality is NOT here; if no observer
# or manual note exists, we fall through to the Wikipedia summary instead.
NOTE_PRIORITY = {
    "record-anchored": ["auto-observer", "manual", "auto-bimodal"],
    "seasonal-anchored": ["manual", "auto-observer", "auto-bimodal"],
    "year-round": ["manual"],
}


def trim_summary(s: str | None, sentences: int) -> str:
    """Take the first N sentences from a Wikipedia summary, but first MASK
    abbreviation periods (Latin name abbrevs like 'T. bengalensis' in tern
    summaries; common honorifics) so they don't get treated as sentence ends."""
    if not s:
        return ""
    import re as _re
    PH = "\x00"
    masked = _re.sub(
        r'\b(?:[A-Z]|Mt|St|Mr|Mrs|Ms|Dr|Prof|Jr|Sr|ssp|subsp|var|cf|nr|aff)\.(\s)',
        lambda m: m.group(0).replace('.', PH),
        s,
    )
    parts = _re.findall(r'[^.!?]+[.!?]+(?=\s+[A-Z]|\s*$)', masked)
    if not parts:
        return s.strip().replace(PH, '.')
    cleaned = [p.strip() for p in parts]
    return ' '.join(cleaned[:sentences]).replace(PH, '.').strip()


def pick_lead(c: dict, framing: str) -> tuple[str, str | None]:
    """Return (leadLine, caption). Caption is None unless the note has an attribution."""
    desc = c.get("description") or {}
    summary = desc.get("wikipediaSummary")

    # Bedrock species: bedrock note wins; fall back to summary.
    if c["source"] in ("bedrock-plant", "bedrock-animal"):
        return (c.get("bedrock_note") or trim_summary(summary, 2), None)

    # Records: try the priority list, fall back to Wikipedia summary.
    ne = c.get("notes_entry")
    if ne and ne.get("notes"):
        for src in NOTE_PRIORITY.get(framing, []):
            for n in ne["notes"]:
                if n["source"] == src:
                    text = n["text"]
                    if src == "auto-observer":
                        parts = text.rsplit(" — ", 1)
                        if len(parts) == 2:
                            return (parts[0].strip(), parts[1].rstrip("."))
                    return (text, None)
    return (trim_summary(summary, 2), None)


# ----- main picker pool builders -----
def doy_iso(s: str) -> int:
    return doy_of(date.fromisoformat(s))


def build_pool(seas, notes, canon, bedrock_plants, bedrock_animals, gb, inat, inat_excludes, descriptions) -> dict[str, dict]:
    descs = (descriptions or {}).get("byCanonical", {}) if descriptions else {}
    feral_canon = {s["canonical"] for s in canon["species"] if s.get("feral")}
    excluded_inat = {x["taxonId"] for x in inat_excludes["excludedTaxa"]}
    notes_by_canon = {s["canonical"]: s for s in notes["species"]}
    gb_name_to_canon = {s["goingbirding"]["name"]: s["canonical"]
                        for s in canon["species"] if not s.get("exclude")}

    days_by_canon: dict[str, list[int]] = defaultdict(list)
    for r in gb["records"]:
        c = gb_name_to_canon.get(r["species"])
        if c and c not in feral_canon:
            days_by_canon[c].append(doy_iso(r["iso"]))
    for o in inat["records"]:
        if o.get("taxonId") in excluded_inat:
            continue
        canonical = o.get("commonName") or o.get("scientificName")
        if not canonical or not o.get("observed_on"):
            continue
        days_by_canon[canonical].append(doy_iso(o["observed_on"]))

    pool: dict[str, dict] = {}
    # Records-based: stable seasonality species with at least one note.
    for s in seas["species"]:
        if s["canonical"] in feral_canon:
            continue
        if not s.get("stable"):
            continue
        ne = notes_by_canon.get(s["canonical"])
        if not ne or not ne.get("notes"):
            continue
        pool[s["canonical"]] = {
            "source": "records",
            "canonical": s["canonical"],
            "scientificName": s.get("scientificName"),
            "iconicTaxon": s.get("iconicTaxon"),
            "peakinessAdj": s.get("peakinessAdj"),
            "localRarity": s.get("localRarity"),
            "links": s.get("links") or {},
            "notes_entry": ne,
            "days": days_by_canon.get(s["canonical"], []),
            "description": descs.get(s["canonical"]),
        }

    # Bedrock plants
    for s in bedrock_plants["species"]:
        if s["canonical"] in pool:
            continue
        pool[s["canonical"]] = {
            "source": "bedrock-plant",
            "canonical": s["canonical"],
            "scientificName": s.get("scientificName"),
            "iconicTaxon": "Plantae",
            "peakinessAdj": None,
            "localRarity": None,
            "links": s.get("links") or {},
            "plant_zone": s.get("zone"),
            "season": s.get("season") or {},
            "bedrock_note": (s.get("notes") or [None])[0],
            "description": descs.get(s["canonical"]),
        }

    # Bedrock animals
    for s in bedrock_animals["species"]:
        if s["canonical"] in pool:
            continue
        pool[s["canonical"]] = {
            "source": "bedrock-animal",
            "canonical": s["canonical"],
            "scientificName": s.get("scientificName"),
            "iconicTaxon": s.get("iconicTaxon", "Animalia"),
            "peakinessAdj": None,
            "localRarity": None,
            "links": s.get("links") or {},
            "animal_zone": s.get("ecologyZone"),
            "taxonGroup": s.get("taxonGroup"),
            "season": s.get("season") or {},
            "bedrock_note": (s.get("notes") or [None])[0],
            "description": descs.get(s["canonical"]),
        }

    return pool


def hash_seed(iso: str) -> int:
    return int(hashlib.md5(iso.encode()).hexdigest(), 16) & 0x7FFFFFFF


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seasonality", default="data/seasonality.json")
    p.add_argument("--notes", default="data/species_notes.json")
    p.add_argument("--canonical", default="data/species_canonical.json")
    p.add_argument("--bedrock-plants", default="data/saltmarsh_bedrock.json")
    p.add_argument("--bedrock-animals", default="data/saltmarsh_bedrock_animals.json")
    p.add_argument("--goingbirding", default="data/goingbirding_birds.json")
    p.add_argument("--inat", default="data/inaturalist_obs.json")
    p.add_argument("--inat-excludes", default="data/inaturalist_excludes.json")
    p.add_argument("--descriptions", default="data/species_descriptions.json")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--start", default=None)
    p.add_argument("--target-birds", type=float, default=0.55)
    p.add_argument("--target-plants", type=float, default=0.20)
    p.add_argument("--target-insects", type=float, default=0.10)
    p.add_argument("--target-others", type=float, default=0.15)
    p.add_argument("--decay-halflife", type=int, default=21)  # matches index.html WILDLIFE_DECAY_HALFLIFE
    p.add_argument("--out", default="/tmp/wildlife_preview.json", help="JSON dry-run output (default writes to /tmp because the live page does NOT consume this file — the JS picker in index.html does the same computation at runtime).")
    p.add_argument("--print", action="store_true", help="Also print a UI-styled preview to stdout (eyebrow / species / leadLine / caption / link) for each day.")
    args = p.parse_args()

    seas = json.load(open(args.seasonality))
    notes = json.load(open(args.notes))
    canon = json.load(open(args.canonical))
    bedrock_plants = json.load(open(args.bedrock_plants))
    bedrock_animals = json.load(open(args.bedrock_animals))
    gb = json.load(open(args.goingbirding))
    inat = json.load(open(args.inat))
    inat_excludes = json.load(open(args.inat_excludes))
    try:
        descriptions = json.load(open(args.descriptions))
    except FileNotFoundError:
        descriptions = None

    pool = build_pool(seas, notes, canon, bedrock_plants, bedrock_animals, gb, inat, inat_excludes, descriptions)
    by_group: dict[str, list[dict]] = defaultdict(list)
    for c in pool.values():
        by_group[group_for(c)].append(c)

    target = {
        "birds": args.target_birds,
        "plants": args.target_plants,
        "insects": args.target_insects,
        "others": args.target_others,
    }
    kernel = wrapped_gaussian_kernel(1.0)
    start = date.fromisoformat(args.start) if args.start else date.today()

    out_by_date: dict[str, dict] = {}
    for offset in range(args.days):
        d = start + timedelta(days=offset)
        iso = d.isoformat()
        target_doy = doy_of(d)
        target_month = d.month
        target_week = week_bin(target_doy)

        # Score every candidate, gather per-group eligibles.
        groups_with_options: dict[str, list[tuple[dict, float]]] = {}
        for g, candidates in by_group.items():
            scored: list[tuple[dict, float]] = []
            for c in candidates:
                s_match = season_match_score(c, target_doy, target_month, target_week, kernel)
                if s_match <= 0.01:
                    continue
                rarity = rarity_sweet(c.get("localRarity"))
                decay = usage_decay(c["canonical"], out_by_date, d, args.decay_halflife)
                score = s_match * rarity * decay
                if score > 0:
                    scored.append((c, score))
            if scored:
                groups_with_options[g] = scored

        if not groups_with_options:
            continue  # nothing surfaceable — let the page fall through to MARSH[].

        rng = random.Random(hash_seed(iso))
        groups = list(groups_with_options.keys())
        gweights = [target.get(g, 0.0) for g in groups]
        if sum(gweights) == 0:
            gweights = [1.0] * len(groups)
        chosen_group = rng.choices(groups, weights=gweights, k=1)[0]
        scored = groups_with_options[chosen_group]
        cands, weights = zip(*scored)
        chosen = rng.choices(cands, weights=weights, k=1)[0]

        framing = pick_framing(chosen, target_month)
        eyebrow = make_eyebrow(chosen, framing, target_month)
        lead, caption = pick_lead(chosen, framing)

        # Pick a click-through link, prefer the richest target.
        links = chosen.get("links") or {}
        primary = (
            links.get("ebird")
            or links.get("inat")
            or links.get("wikipedia")
            or links.get("goingbirding")
            or links.get("ukmoths")
        )

        out_by_date[iso] = {
            "canonical": chosen["canonical"],
            "scientificName": chosen.get("scientificName"),
            "iconicTaxon": chosen.get("iconicTaxon"),
            "group": chosen_group,
            "source": chosen["source"],
            "framing": framing,
            "eyebrow": eyebrow,
            "leadLine": lead,
            "caption": caption,
            "links": {k: v for k, v in links.items() if v},
            "primaryLink": primary,
        }

    out = {
        "_meta": {
            "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "startDate": start.isoformat(),
            "days": args.days,
            "targetDistribution": target,
            "decayHalflifeDays": args.decay_halflife,
            "poolSize": len(pool),
            "poolByGroup": {g: len(v) for g, v in by_group.items()},
        },
        "byDate": out_by_date,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    # Quick post-run group distribution check.
    actual = defaultdict(int)
    for v in out_by_date.values():
        actual[v["group"]] += 1
    print(f"Wrote {args.out}: {len(out_by_date)} days. Group counts: {dict(actual)}", file=sys.stderr)

    if args.print:
        weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        rule = "─" * 78
        print()
        for iso, v in out_by_date.items():
            d = date.fromisoformat(iso)
            wd = weekday[d.weekday()]
            sci = f"  ({v['scientificName']})" if v.get("scientificName") else ""
            print(rule)
            print(f"[{wd} {iso}]  {v['eyebrow'].upper()}")
            print(f"  {v['canonical']}{sci}")
            if v.get("leadLine"):
                # Mirror the page's flow: lead line wraps under the species name.
                print(f"  {v['leadLine']}")
            if v.get("caption"):
                print(f"    — {v['caption']}")
            if v.get("primaryLink"):
                print(f"  → {v['primaryLink']}")
        print(rule)


if __name__ == "__main__":
    main()
