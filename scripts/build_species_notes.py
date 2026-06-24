#!/usr/bin/env python3
"""
Build a per-species notes table: short, picker-friendly tidbits that the
almanac can surface alongside a species line. Three sources of notes:

  1. AUTO from seasonality — derives a category (resident / spring passage /
     wintering / bimodal passage / etc) and a human-readable window line.
  2. AUTO from observer remarks — mines goingbirding's notes column for
     notable strings ("Never heard 3 singing here before", "Usual pair in
     reed bed", "Seems to be an influx of them", "pair breeding").
  3. MANUAL tidbits — hand-curated facts that recur (migration origins,
     creek-specific behaviours, Solent context). Persisted in this script.

Bimodality is detected by finding circular local maxima on the smoothed
weekly distribution (≥40% of global max, ≥6 weeks apart). Whimbrel's
spring+autumn passage is the canonical test case.

Args:
    --seasonality FILE   (default: data/seasonality.json)
    --goingbirding FILE  (default: data/goingbirding_birds.json)
    --canonical FILE     (default: data/species_canonical.json)
    --overrides FILE     (default: data/observer_notes_overrides.json — false-positive
                          exclusions for the auto-observer miner; survives re-runs)
    --out FILE           (default: data/species_notes.json)

Example:
    python3 scripts/build_species_notes.py
"""
import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import date, datetime

N_BINS = 52


# =============================================================================
# Manual tidbits — hand-curated. Keep one or two strong notes per species.
# Anything you've personally observed about a bird that the data alone won't
# tell us (creek-specific behaviour, migration origin, conservation context).
# =============================================================================
MANUAL_NOTES = {
    # Anything that duplicates an auto-observer quote has been pulled — let the
    # raw observer quote speak for itself. These are external context / Solent
    # facts that the records don't contain.
    "Eurasian Curlew": [
        "Roosts in numbers near Calshot swingbridge at low tide — 25 birds counted on a single tide.",
    ],
    "Dark-bellied Brent Goose": [
        "Wintering birds reach the Solent from the Taymyr Peninsula in Arctic Siberia, ~3,000 miles. About a tenth of the world population winters along this shore.",
    ],
    "Light-bellied Brent Goose": [
        "Scarcer race that breeds in Arctic Canada and Greenland — most Solent Brents are dark-bellied.",
    ],
    "Cetti's Warbler": [
        "Year-round skulker in dense creek-side cover; you almost never see it, but the explosive song carries across the saltmarsh from April onwards.",
    ],
    "Common Cuckoo": [
        "Only here for a few weeks of spring — late May is essentially the entire UK calling window.",
    ],
    "Little Ringed Plover": [
        "Freshwater-edge breeder that turns up on Ashlett's drying mud in passage — listen for the 'cree-ah' flight call.",
    ],
    "Eurasian Whimbrel": [
        "Seven-note flight whistle — the giveaway. Heads up Southampton Water in passage in April, and again as scattered birds in July and August.",
    ],
    "Common Kingfisher": [
        "Year-round at the creek but most visible in late summer when juveniles disperse along the freshwater feed.",
    ],
}


# =============================================================================
# Observer-note miners — patterns that flag a noteworthy goingbirding note.
# =============================================================================
NOTABLE_PATTERNS = [
    (re.compile(r"\bnever\b", re.I), "first-observation"),
    # "first" — broad but excludes idioms (first thing/time/light/aid).
    (re.compile(r"\b(?:my\s+)?first\b(?!\s+(?:thing|time|light|aid))", re.I), "first-observation"),
    (re.compile(r"\b(usual|same)\s+pair\b", re.I), "resident-pair"),
    (re.compile(r"\bpair\s+breeding\b", re.I), "breeding"),
    (re.compile(r"\b(juvenile|juv|young|fledged)\b", re.I), "breeding"),
    (re.compile(r"\binflux\b", re.I), "irruption"),
    (re.compile(r"\bpassage\b", re.I), "passage"),
    (re.compile(r"\b(arrived?|departing|moving)\b", re.I), "phenology"),
]


# =============================================================================
# Helpers: re-derive smoothed circular distribution to detect bimodality.
# =============================================================================
def circ_dist(i: int, j: int, n: int) -> int:
    d = abs(i - j)
    return min(d, n - d)


def wrapped_gaussian(sigma: float, n: int = N_BINS) -> list[float]:
    k = [math.exp(-0.5 * (circ_dist(0, i, n) / sigma) ** 2) for i in range(n)]
    s = sum(k)
    return [x / s for x in k]


def smooth(bins: list[float], kernel: list[float]) -> list[float]:
    n = len(bins)
    out = [0.0] * n
    for i in range(n):
        acc = 0.0
        for j in range(n):
            acc += bins[j] * kernel[(i - j) % n]
        out[i] = acc
    return out


def find_peaks(smoothed: list[float], frac_of_max: float = 0.55, min_sep_weeks: int = 8) -> list[int]:
    n = len(smoothed)
    mx = max(smoothed) if smoothed else 0.0
    if mx == 0.0:
        return []
    thresh = mx * frac_of_max
    candidates = []
    for i in range(n):
        prev = smoothed[(i - 1) % n]
        nxt = smoothed[(i + 1) % n]
        if smoothed[i] >= prev and smoothed[i] >= nxt and smoothed[i] >= thresh:
            candidates.append(i)
    # Suppress near-neighbours: keep the higher of any pair within min_sep_weeks.
    candidates.sort(key=lambda i: -smoothed[i])
    kept: list[int] = []
    for c in candidates:
        if all(circ_dist(c, k, n) >= min_sep_weeks for k in kept):
            kept.append(c)
    return sorted(kept)


def week_to_month(w: int) -> int:
    doy_center = int((w + 0.5) * 365 / N_BINS)
    return date.fromordinal(date(2025, 1, 1).toordinal() + doy_center - 1).month


def week_to_label(w: int) -> str:
    doy_center = int((w + 0.5) * 365 / N_BINS)
    d = date.fromordinal(date(2025, 1, 1).toordinal() + doy_center - 1)
    third = (d.day - 1) // 10
    qual = ["early", "mid", "late"][min(2, third)]
    return f"{qual} {d.strftime('%B')}"


def doy(iso: str) -> int:
    return date.fromisoformat(iso).timetuple().tm_yday


def week_bin(d: int) -> int:
    return min(N_BINS - 1, (d - 1) * N_BINS // 365)


# =============================================================================
# Category classifier: derives 'resident' / 'wintering' / 'spring passage' etc.
# =============================================================================
def classify(peak_month: int, peak_adj: float, window_days: int | None, bimodal: bool) -> tuple[str, str]:
    """Return (slug, human-readable phrase)."""
    if bimodal:
        return ("bimodal-passage", "Two-pulse passage — peaks in spring and autumn")
    if peak_adj is None:
        return ("unknown", "Too few records to characterise")
    if peak_adj < 0.10:
        return ("year-round", "Effectively year-round at the creek")
    if peak_adj < 0.18:
        return ("wide-ranging", "Present most of the year with a soft seasonal peak")
    # Concentrated species — bucket by peak month.
    if peak_month in (12, 1, 2):
        return ("winter-visitor", "Winter visitor")
    if peak_month in (3, 4, 5):
        if window_days is not None and window_days < 45:
            return ("spring-passage", "Tight spring passage moment")
        return ("spring-arrival", "Spring arrival or early breeder")
    if peak_month in (6, 7):
        return ("summer-presence", "Summer presence — breeding-season window")
    if peak_month in (8, 9, 10):
        if window_days is not None and window_days < 45:
            return ("autumn-passage", "Tight autumn passage moment")
        return ("autumn-presence", "Autumn presence — passage or arrival of winterers")
    if peak_month == 11:
        return ("late-autumn", "Late-autumn arrival of winterers")
    return ("seasonal", "Seasonal peak")


# =============================================================================
# Build window line: "80% of records fall in a 28-day window centred on late May"
# =============================================================================
def window_line(window_days: int | None, peak_label: str | None, n: int) -> str | None:
    if window_days is None or peak_label is None:
        return None
    # Phrase the window humanly.
    if window_days <= 14:
        phrase = "in a 2-week window"
    elif window_days <= 35:
        phrase = f"in a {round(window_days/7)}-week window"
    elif window_days <= 90:
        phrase = f"in a {round(window_days/30.4)}-month window"
    elif window_days <= 250:
        phrase = f"across about {round(window_days/30.4)} months"
    else:
        return None  # Too broad to be informative.
    return f"4 in 5 records {phrase} around {peak_label} (n={n})."


# =============================================================================
# Main
# =============================================================================
def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seasonality", default="data/seasonality.json")
    p.add_argument("--goingbirding", default="data/goingbirding_birds.json")
    p.add_argument("--canonical", default="data/species_canonical.json")
    p.add_argument("--overrides", default="data/observer_notes_overrides.json")
    p.add_argument("--out", default="data/species_notes.json")
    args = p.parse_args()

    seas = json.load(open(args.seasonality))
    gb = json.load(open(args.goingbirding))
    canon = json.load(open(args.canonical))
    try:
        overrides = json.load(open(args.overrides))
    except FileNotFoundError:
        overrides = {"excludes": [], "manualQuotes": []}

    # (species_canonical, lower-cased quote prefix) → reason. Match is by prefix
    # so a stray punctuation/quote-mark wrap doesn't break the lookup.
    exclude_set: set[tuple[str, str]] = {
        (e["species"], e["quotePrefix"].lower().strip())
        for e in overrides.get("excludes", [])
    }

    # gb_name → canonical name for note attribution.
    gb_to_canon = {s["goingbirding"]["name"]: s["canonical"] for s in canon["species"]}

    # Group goingbirding records by canonical name for observer-note mining.
    notes_by_canon: dict[str, list[dict]] = defaultdict(list)
    for r in gb["records"]:
        c = gb_to_canon.get(r["species"])
        if not c or not r.get("notes"):
            continue
        n = r["notes"].strip()
        if not n or n == "&nbsp;":
            continue
        notes_by_canon[c].append({"iso": r["iso"], "site": r["site"], "observer": r["observer"], "count": r["count"], "text": n})

    kernel = wrapped_gaussian(1.0)

    out_species = []
    for s in seas["species"]:
        canonical = s["canonical"]
        n = s["n"]
        notes: list[dict] = []

        # --- AUTO 1: seasonality category + window line ---
        bimodal = False
        peak_count = 0
        peaks: list[int] = []
        if s.get("stable") and s.get("peakWeek") is not None:
            # Reconstruct smoothed distribution to detect bimodality.
            days = [doy(r["iso"]) for r in gb["records"] if gb_to_canon.get(r["species"]) == canonical]
            bins = [0.0] * N_BINS
            for d in days:
                bins[week_bin(d)] += 1
            sm = smooth(bins, kernel)
            peaks = find_peaks(sm)
            peak_count = len(peaks)
            # Bimodal claim is strict: only on species already concentrated overall
            # (peakAdj > 0.15 — residents need not apply), exactly 2 peaks, and
            # peaks ≥ 12 weeks apart (real spring-vs-autumn split, not noise).
            peak_adj = s.get("peakinessAdj") or 0
            if peak_adj > 0.15 and peak_count == 2:
                sep = circ_dist(peaks[0], peaks[1], N_BINS)
                bimodal = sep >= 12

            peak_month = week_to_month(s["peakWeek"])
            slug, phrase = classify(peak_month, s.get("peakinessAdj"), s.get("minWindowDays"), bimodal)
            notes.append({"source": "auto-seasonality", "category": slug, "text": phrase})

            wl = window_line(s.get("minWindowDays"), s.get("peakWeekLabel"), n)
            if wl:
                notes.append({"source": "auto-window", "category": "window", "text": wl})

            if bimodal:
                peak_labels = [week_to_label(p) for p in peaks]
                notes.append({
                    "source": "auto-bimodal",
                    "category": "bimodal",
                    "text": f"Two-pulse passage: peaks in {peak_labels[0]} and {peak_labels[1]}.",
                })

        # --- AUTO 2: mine observer notes for noteworthy strings ---
        obs_notes = notes_by_canon.get(canonical, [])
        seen_categories: set[str] = set()
        for o in obs_notes:
            for pat, cat in NOTABLE_PATTERNS:
                if pat.search(o["text"]) and cat not in seen_categories:
                    quote = re.sub(r"\s+", " ", o["text"]).strip(" .;,&nbsp;").strip()
                    if len(quote) > 140:
                        continue
                    # Skip anything matching an overrides exclude prefix.
                    quote_lower = quote.lower()
                    if any(canonical == sp and quote_lower.startswith(pref)
                           for (sp, pref) in exclude_set):
                        continue
                    notes.append({
                        "source": "auto-observer",
                        "category": f"observer-{cat}",
                        "text": f'"{quote}" — {o["observer"]}, {o["site"]} {o["iso"]}.',
                    })
                    seen_categories.add(cat)
                    break

        # --- MANUAL ---
        for t in MANUAL_NOTES.get(canonical, []):
            notes.append({"source": "manual", "category": "tidbit", "text": t})

        if not notes:
            continue

        out_species.append({
            "canonical": canonical,
            "scientificName": s.get("scientificName"),
            "iconicTaxon": s.get("iconicTaxon"),
            "n": n,
            "peakinessAdj": s.get("peakinessAdj"),
            "peakWeekLabel": s.get("peakWeekLabel"),
            "minWindowDays": s.get("minWindowDays"),
            "bimodal": bimodal,
            "peakCount": peak_count,
            "links": s.get("links"),
            "notes": notes,
        })

    # Sort: species with manual notes first, then by peakinessAdj.
    def sort_key(sp):
        has_manual = any(n["source"] == "manual" for n in sp["notes"])
        return (-int(has_manual), -(sp.get("peakinessAdj") or 0), sp["canonical"])

    out_species.sort(key=sort_key)

    counts_by_source = defaultdict(int)
    for sp in out_species:
        for n_ in sp["notes"]:
            counts_by_source[n_["source"]] += 1

    out = {
        "_meta": {
            "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "speciesWithNotes": len(out_species),
            "manualNotesCount": sum(1 for sp in out_species for n_ in sp["notes"] if n_["source"] == "manual"),
            "notesBySource": dict(counts_by_source),
        },
        "species": out_species,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(
        f"Wrote {args.out}: {len(out_species)} species, "
        f"{sum(len(sp['notes']) for sp in out_species)} notes "
        f"({dict(counts_by_source)})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
