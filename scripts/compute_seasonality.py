#!/usr/bin/env python3
"""
Merge goingbirding + iNaturalist records into one per-species table and
compute a circular-aware seasonality (peakiness) score for each species.

Why this exists: a wildlife picker needs to surface migration moments
("Whimbrel passage now") differently from year-round residents. We score
"peakiness" with wrapped-Gaussian Shannon entropy over 52 weekly bins —
circular convolution treats Dec/Jan as adjacent, smoothing keeps a species
seen on day 180 + 181 from being faked into a two-week bimodal split.
Bimodal migrants (spring + autumn passage) stay honest because entropy
doesn't have the peaks-cancel-out problem that the mean resultant length
R has.

Join key: scientific name (where known), else canonical common name.
Goingbirding species without an eBird taxonomy match (Dartford Warbler,
Northern Goshawk, Lesser Redpoll) get their scientific names from a small
hard-coded fallback so they can still join with any iNat records.

Args:
    --goingbirding FILE  (default: data/goingbirding_birds.json)
    --inat FILE          (default: data/inaturalist_obs.json)
    --canonical FILE     (default: data/species_canonical.json)
    --inat-excludes FILE (default: data/inaturalist_excludes.json)
    --sigma-weeks FLOAT  wrapped-Gaussian kernel width (default: 1.0)
    --window-pct FLOAT   min-window coverage threshold (default: 0.80)
    --min-records INT    min records to compute a stable score (default: 3)
    --out FILE           (default: data/seasonality.json)

Example:
    python3 scripts/compute_seasonality.py
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime

N_BINS = 52  # weekly

# Fallback scientific names for goingbirding species that have no eBird match
# in our hotspots — lets them still join with iNat by sciName if iNat sees them.
GB_SCI_FALLBACK = {
    "Dartford Warbler": "Curruca undata",
    "Northern Goshawk": "Accipiter gentilis",
    "Lesser Redpoll":   "Acanthis cabaret",
}


def doy(iso: str) -> int:
    """Day-of-year, 1..366."""
    return date.fromisoformat(iso).timetuple().tm_yday


def week_bin(d: int) -> int:
    """Map day-of-year (1..366) to weekly bin 0..51."""
    return min(N_BINS - 1, (d - 1) * N_BINS // 365)


def circ_dist(i: int, j: int, n: int) -> int:
    d = abs(i - j)
    return min(d, n - d)


def wrapped_gaussian_kernel(sigma: float, n: int = N_BINS) -> list[float]:
    k = [math.exp(-0.5 * (circ_dist(0, i, n) / sigma) ** 2) for i in range(n)]
    s = sum(k)
    return [x / s for x in k]


def circ_smooth(bins: list[float], kernel: list[float]) -> list[float]:
    n = len(bins)
    out = [0.0] * n
    for i in range(n):
        acc = 0.0
        for j in range(n):
            acc += bins[j] * kernel[(i - j) % n]
        out[i] = acc
    return out


def shannon(p: list[float]) -> float:
    return -sum(x * math.log(x) for x in p if x > 0)


def min_window_days(days: list[int], coverage: float) -> int | None:
    """Smallest circular window (in days) that contains >= coverage*N records."""
    n = len(days)
    if n == 0:
        return None
    k = max(1, math.ceil(coverage * n))
    s = sorted(days)
    s_dup = s + [d + 365 for d in s]  # circular duplication
    best = 365
    for i in range(n):  # start each window at each unique day position
        width = s_dup[i + k - 1] - s_dup[i] + 1
        if width < best:
            best = width
    return best


def peak_week(smoothed: list[float]) -> int:
    return max(range(len(smoothed)), key=lambda i: smoothed[i])


def week_to_label(w: int) -> str:
    """Bin centre → 'early/mid/late <Month>'."""
    doy_center = int((w + 0.5) * 365 / N_BINS)
    d = date.fromordinal(date(2025, 1, 1).toordinal() + doy_center - 1)
    third = (d.day - 1) // 10
    qual = ["early", "mid", "late"][min(2, third)]
    return f"{qual} {d.strftime('%B')}"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--goingbirding", default="data/goingbirding_birds.json")
    p.add_argument("--inat", default="data/inaturalist_obs.json")
    p.add_argument("--canonical", default="data/species_canonical.json")
    p.add_argument("--inat-excludes", default="data/inaturalist_excludes.json")
    p.add_argument("--sigma-weeks", type=float, default=1.0)
    p.add_argument("--window-pct", type=float, default=0.80)
    p.add_argument("--min-records", type=int, default=3)
    p.add_argument("--out", default="data/seasonality.json")
    args = p.parse_args()

    gb = json.load(open(args.goingbirding))
    inat = json.load(open(args.inat))
    canon = json.load(open(args.canonical))
    inat_ex = json.load(open(args.inat_excludes))

    excluded_taxa = {x["taxonId"] for x in inat_ex["excludedTaxa"]}
    gb_excluded_names = {s["goingbirding"]["name"] for s in canon["species"] if s["exclude"]}
    feral_canonical = {s["canonical"] for s in canon["species"] if s.get("feral")}

    # gb_name → canonical entry; injected sciName fallback where missing.
    canon_by_gb_name = {}
    for s in canon["species"]:
        sci = s.get("scientificName") or GB_SCI_FALLBACK.get(s["canonical"])
        canon_by_gb_name[s["goingbirding"]["name"]] = {
            **s,
            "scientificName": sci,
        }

    # Merge into one per-species accumulator. Key by sciName when known, else canonical name.
    species_acc: dict[str, dict] = {}

    def key_for(sci: str | None, canonical: str) -> str:
        return sci or f"name:{canonical}"

    # Goingbirding records
    skipped_gb = 0
    for r in gb["records"]:
        if r["species"] in gb_excluded_names:
            skipped_gb += 1
            continue
        c = canon_by_gb_name.get(r["species"])
        if not c:
            continue  # unmapped (shouldn't happen — builder warns on missing)
        sci = c["scientificName"]
        canonical = c["canonical"]
        k = key_for(sci, canonical)
        entry = species_acc.setdefault(k, {
            "canonical": canonical,
            "scientificName": sci,
            "iconicTaxon": "Aves",
            "days": [],
            "sources": defaultdict(int),
            "links": {
                "goingbirding": c["goingbirding"]["url"],
                "ebird": (c["ebird"] or {}).get("url"),
                "inat": None,
            },
        })
        entry["days"].append(doy(r["iso"]))
        entry["sources"]["goingbirding"] += 1

    # iNat records (join by sci name to merge with goingbirding birds)
    skipped_inat = 0
    for o in inat["records"]:
        if o.get("taxonId") in excluded_taxa:
            skipped_inat += 1
            continue
        sci = o.get("scientificName")
        canonical = o.get("commonName") or sci
        if not (canonical and o.get("observed_on")):
            continue
        k = key_for(sci, canonical)
        entry = species_acc.get(k)
        if entry is None:
            # New non-bird (or bird not in goingbirding cluster) — start a fresh entry.
            entry = species_acc[k] = {
                "canonical": canonical,
                "scientificName": sci,
                "iconicTaxon": o.get("iconicTaxon"),
                "days": [],
                "sources": defaultdict(int),
                "links": {
                    "goingbirding": None,
                    "ebird": None,
                    "inat": o.get("taxonUrl"),
                },
            }
        else:
            entry["links"]["inat"] = entry["links"].get("inat") or o.get("taxonUrl")
        entry["days"].append(doy(o["observed_on"]))
        entry["sources"]["inat"] += 1

    # Compute seasonality per species.
    kernel = wrapped_gaussian_kernel(args.sigma_weeks, N_BINS)
    H_max = math.log(N_BINS)

    out_species = []
    for k, e in species_acc.items():
        days = e["days"]
        n = len(days)
        bins = [0.0] * N_BINS
        for d in days:
            bins[week_bin(d)] += 1
        smoothed = circ_smooth(bins, kernel)
        total = sum(smoothed)
        p = [x / total for x in smoothed] if total > 0 else [0.0] * N_BINS
        H = shannon(p)
        peakiness = 1 - H / H_max if total > 0 else None
        # Bayesian shrinkage toward 0 for low-N species: ~5-record half-life.
        peakiness_adj = peakiness * n / (n + 5) if peakiness is not None else None
        pw = peak_week(smoothed) if total > 0 else None
        mw = min_window_days(days, args.window_pct) if n >= args.min_records else None

        out_species.append({
            "canonical": e["canonical"],
            "scientificName": e["scientificName"],
            "iconicTaxon": e["iconicTaxon"],
            "feral": e["canonical"] in feral_canonical,
            "n": n,
            "sources": dict(e["sources"]),
            "peakiness": round(peakiness, 4) if peakiness is not None else None,
            "peakinessAdj": round(peakiness_adj, 4) if peakiness_adj is not None else None,
            "entropy": round(H, 4) if peakiness is not None else None,
            "peakWeek": pw,
            "peakWeekLabel": week_to_label(pw) if pw is not None else None,
            "minWindowDays": mw,
            "minWindowCoverage": args.window_pct,
            "stable": n >= args.min_records,
            "links": e["links"],
        })

    # Local rarity: per-iconic-taxon, log-scaled. localRarity = 1 means rarest
    # in its group (n=1); 0 means as common as it gets. Birds compete with birds
    # so a goingbirding singleton can be "rare here" without iNat singletons
    # (which are mostly under-sampled non-birds) flooding the scale.
    by_taxon: dict[str, list[dict]] = defaultdict(list)
    for s in out_species:
        by_taxon[s["iconicTaxon"] or "?"].append(s)
    for taxon, lst in by_taxon.items():
        max_n = max(s["n"] for s in lst)
        denom = math.log(max_n + 1) or 1.0
        for s in lst:
            s["localRarity"] = round(1 - math.log(s["n"] + 1) / denom, 4)
            s["localRarityGroup"] = taxon

    out_species.sort(key=lambda s: (-(s["peakinessAdj"] or 0), s["canonical"]))

    out = {
        "_meta": {
            "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "sigmaWeeks": args.sigma_weeks,
            "windowPct": args.window_pct,
            "minRecordsForStable": args.min_records,
            "totalSpecies": len(out_species),
            "totalRecords": sum(s["n"] for s in out_species),
            "skippedExcluded": {"goingbirding": skipped_gb, "inat": skipped_inat},
            "method": (
                "Day-of-year binned into 52 weeks, convolved with a circular "
                "Gaussian kernel (mod-52 wrap), then normalised Shannon entropy. "
                "peakiness = 1 - H/log(52); peakinessAdj = peakiness * n/(n+5)."
            ),
        },
        "species": out_species,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Wrote {args.out}: {len(out_species)} species, {out['_meta']['totalRecords']} records", file=sys.stderr)


if __name__ == "__main__":
    main()
