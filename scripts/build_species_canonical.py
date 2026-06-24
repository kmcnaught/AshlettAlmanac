#!/usr/bin/env python3
"""
Build a canonical species table that joins goingbirding's HOS-style names
to eBird's IOC-style names, picking the "fancier" British-formal name for
display ("Eurasian Curlew" not "Curlew"; "Sand Martin" not "Bank Swallow").

Why this exists: goingbirding uses short British colloquial names ("Curlew",
"Buzzard"); eBird's API uses IOC formal names ("Eurasian Curlew", "Common
Buzzard"). To deduplicate and cross-link between the two we need a manual
join — there is no clean algorithmic mapping because of British/American
naming clashes (Skua/Jaeger, Guillemot/Murre, Diver/Loon, Brent/Brant,
Goosander/Common Merganser, Grey Plover/Black-bellied Plover, Slavonian
Grebe/Horned Grebe, Sand Martin/Bank Swallow, Great White Egret/Great Egret).

The "fancier" rule:
  - When both sources share the formal name: keep it.
  - When eBird adds a meaningful prefix (Eurasian/Common/Northern/European/
    Western/Black-legged/etc.) over a bare goingbirding name: prefer eBird.
  - When eBird uses an American name and goingbirding the British one
    (the Anglo-American clashes above): keep British.
  - Spelling: prefer UK forms (Grey/Greylag/Goosander).

Two species are flagged exclude=true (escaped/feral): Boat-tailed Grackle
and Great-tailed Grackle — both single residential-garden birds in Holbury,
not the wider environment the almanac is meant to evoke.

Args:
    --goingbirding FILE  (default: data/goingbirding_birds.json)
    --ebird FILE         (default: data/ebird_hotspots.json)
    --out FILE           (default: data/species_canonical.json)

Example:
    python3 scripts/build_species_canonical.py
"""
import argparse
import json
import sys
from datetime import datetime

# (goingbirding name, eBird common name OR None if no eBird entry) → canonical
# Exclude=True means: present in records but not wild — skip in picker.
MANUAL_MAP = {
    # gb_name: {"canonical": ..., "ebird": ..., "exclude": False, "note": ...}
    "Arctic Skua":           {"canonical": "Arctic Skua",           "ebird": "Parasitic Jaeger",      "note": "UK 'Skua' kept over American 'Jaeger'"},
    "Arctic Tern":           {"canonical": "Arctic Tern",           "ebird": "Arctic Tern"},
    "BOAT-TAILED GRACKLE":   {"canonical": "Boat-tailed Grackle",   "ebird": None, "exclude": True, "note": "Escaped/feral — Holbury garden bird"},
    "Bar-tailed Godwit":     {"canonical": "Bar-tailed Godwit",     "ebird": "Bar-tailed Godwit"},
    "Barnacle Goose":        {"canonical": "Barnacle Goose",        "ebird": "Barnacle Goose"},
    "Black Redstart":        {"canonical": "Black Redstart",        "ebird": "Black Redstart"},
    "Black Swan":            {"canonical": "Black Swan",            "ebird": "Black Swan",            "feral": True, "note": "Feral in UK but recordable — escapee population, not a true wild bird"},
    "Black-tailed Godwit":   {"canonical": "Black-tailed Godwit",   "ebird": "Black-tailed Godwit"},
    "Blackcap":              {"canonical": "Eurasian Blackcap",     "ebird": "Eurasian Blackcap"},
    "Buzzard":               {"canonical": "Common Buzzard",        "ebird": "Common Buzzard"},
    "Cetti's Warbler":       {"canonical": "Cetti's Warbler",       "ebird": "Cetti's Warbler"},
    "Chiffchaff":            {"canonical": "Common Chiffchaff",     "ebird": "Common Chiffchaff"},
    "Common Guillemot":      {"canonical": "Common Guillemot",      "ebird": "Common Murre",          "note": "UK 'Guillemot' kept over American 'Murre'"},
    "Common Gull":           {"canonical": "Common Gull",           "ebird": "Common Gull"},
    "Common Sandpiper":      {"canonical": "Common Sandpiper",      "ebird": "Common Sandpiper"},
    "Common Tern":           {"canonical": "Common Tern",           "ebird": "Common Tern"},
    "Common Whitethroat":    {"canonical": "Greater Whitethroat",   "ebird": "Greater Whitethroat"},
    "Cormorant":             {"canonical": "Great Cormorant",       "ebird": "Great Cormorant"},
    "Crossbill":             {"canonical": "Red Crossbill",         "ebird": "Red Crossbill"},
    "Cuckoo":                {"canonical": "Common Cuckoo",         "ebird": "Common Cuckoo"},
    "Curlew":                {"canonical": "Eurasian Curlew",       "ebird": "Eurasian Curlew"},
    "Dark-bellied Brent Goose": {"canonical": "Dark-bellied Brent Goose", "ebird": "Brant",          "note": "UK subspecies-level kept over eBird's generic 'Brant'"},
    "Dartford Warbler":      {"canonical": "Dartford Warbler",      "ebird": None,                    "note": "No eBird record in our hotspots"},
    "Dunlin":                {"canonical": "Dunlin",                "ebird": "Dunlin"},
    "Egyptian Goose":        {"canonical": "Egyptian Goose",        "ebird": "Egyptian Goose",        "feral": True, "note": "Feral in UK but recordable — established escapee population, originally from Africa"},
    "Eider":                 {"canonical": "Common Eider",          "ebird": "Common Eider"},
    "Eurasian Whimbrel":     {"canonical": "Eurasian Whimbrel",     "ebird": "Eurasian Whimbrel"},
    "Eurasian Wigeon":       {"canonical": "Eurasian Wigeon",       "ebird": "Eurasian Wigeon"},
    "European Stonechat":    {"canonical": "European Stonechat",    "ebird": "European Stonechat"},
    "Fieldfare":             {"canonical": "Fieldfare",             "ebird": "Fieldfare"},
    "Firecrest":             {"canonical": "Common Firecrest",      "ebird": "Common Firecrest"},
    "Gadwall":               {"canonical": "Gadwall",               "ebird": "Gadwall"},
    "Gannet":                {"canonical": "Northern Gannet",       "ebird": "Northern Gannet"},
    "Garden Warbler":        {"canonical": "Garden Warbler",        "ebird": "Garden Warbler"},
    "Goosander":             {"canonical": "Goosander",             "ebird": "Common Merganser",      "note": "UK 'Goosander' kept over American 'Common Merganser'"},
    "Goshawk":               {"canonical": "Northern Goshawk",      "ebird": None,                    "note": "No eBird record in our hotspots"},
    "Grasshopper Warbler":   {"canonical": "Common Grasshopper Warbler", "ebird": "Common Grasshopper Warbler"},
    "Great Crested Grebe":   {"canonical": "Great Crested Grebe",   "ebird": "Great Crested Grebe"},
    "Great Northern Diver":  {"canonical": "Great Northern Diver",  "ebird": "Common Loon",           "note": "UK 'Diver' kept over American 'Loon'"},
    "Great White Egret":     {"canonical": "Great White Egret",     "ebird": "Great Egret",           "note": "UK 'Great White Egret' kept"},
    "Green Woodpecker":      {"canonical": "Eurasian Green Woodpecker", "ebird": "Eurasian Green Woodpecker"},
    "Greenshank":            {"canonical": "Common Greenshank",     "ebird": "Common Greenshank"},
    "Grey Plover":           {"canonical": "Grey Plover",           "ebird": "Black-bellied Plover",  "note": "UK 'Grey Plover' kept over American 'Black-bellied Plover'"},
    "Grey Wagtail":          {"canonical": "Grey Wagtail",          "ebird": "Gray Wagtail",          "note": "UK 'Grey' spelling kept"},
    "Greylag Goose":         {"canonical": "Greylag Goose",         "ebird": "Graylag Goose",         "note": "UK 'Greylag' spelling kept"},
    "House Martin":          {"canonical": "Western House-Martin",  "ebird": "Western House-Martin"},
    "Jay":                   {"canonical": "Eurasian Jay",          "ebird": "Eurasian Jay"},
    "Kestrel":               {"canonical": "Eurasian Kestrel",      "ebird": "Eurasian Kestrel"},
    "Kingfisher":            {"canonical": "Common Kingfisher",     "ebird": "Common Kingfisher"},
    "Kittiwake":             {"canonical": "Black-legged Kittiwake","ebird": "Black-legged Kittiwake"},
    "Knot":                  {"canonical": "Red Knot",              "ebird": "Red Knot"},
    "Lapwing":               {"canonical": "Northern Lapwing",      "ebird": "Northern Lapwing"},
    "Lesser Black-backed Gull": {"canonical": "Lesser Black-backed Gull", "ebird": "Lesser Black-backed Gull"},
    "Lesser Redpoll":        {"canonical": "Lesser Redpoll",        "ebird": None,                    "note": "Treated as subsp of Redpoll by eBird"},
    "Lesser Whitethroat":    {"canonical": "Lesser Whitethroat",    "ebird": "Lesser Whitethroat"},
    "Light-bellied Brent Goose": {"canonical": "Light-bellied Brent Goose", "ebird": "Brant",        "note": "UK subspecies-level kept over eBird's generic 'Brant'"},
    "Little Egret":          {"canonical": "Little Egret",          "ebird": "Little Egret"},
    "Little Grebe":          {"canonical": "Little Grebe",          "ebird": "Little Grebe"},
    "Little Gull":           {"canonical": "Little Gull",           "ebird": "Little Gull"},
    "Little Ringed Plover":  {"canonical": "Little Ringed Plover",  "ebird": "Little Ringed Plover"},
    "Magpie":                {"canonical": "Eurasian Magpie",       "ebird": "Eurasian Magpie"},
    "Marsh Harrier":         {"canonical": "Western Marsh Harrier", "ebird": "Western Marsh Harrier"},
    "Marsh Tit":             {"canonical": "Marsh Tit",             "ebird": "Marsh Tit"},
    "Meadow Pipit":          {"canonical": "Meadow Pipit",          "ebird": "Meadow Pipit"},
    "Mediterranean Gull":    {"canonical": "Mediterranean Gull",    "ebird": "Mediterranean Gull"},
    "Merlin":                {"canonical": "Merlin",                "ebird": "Merlin"},
    "Mistle Thrush":         {"canonical": "Mistle Thrush",         "ebird": "Mistle Thrush"},
    "Osprey":                {"canonical": "Osprey",                "ebird": "Osprey"},
    "Oystercatcher":         {"canonical": "Eurasian Oystercatcher","ebird": "Eurasian Oystercatcher"},
    "Peregrine":             {"canonical": "Peregrine Falcon",      "ebird": "Peregrine Falcon"},
    "Pintail":               {"canonical": "Northern Pintail",      "ebird": "Northern Pintail"},
    "Raven":                 {"canonical": "Common Raven",          "ebird": "Common Raven"},
    "Razorbill":             {"canonical": "Razorbill",             "ebird": "Razorbill"},
    "Red Kite":              {"canonical": "Red Kite",              "ebird": "Red Kite"},
    "Red-legged Partridge":  {"canonical": "Red-legged Partridge",  "ebird": "Red-legged Partridge"},
    "Red-throated Diver":    {"canonical": "Red-throated Diver",    "ebird": "Red-throated Loon",     "note": "UK 'Diver' kept over American 'Loon'"},
    "Redshank":              {"canonical": "Common Redshank",       "ebird": "Common Redshank"},
    "Redstart":              {"canonical": "Common Redstart",       "ebird": "Common Redstart"},
    "Redwing":               {"canonical": "Redwing",               "ebird": "Redwing"},
    "Reed Bunting":          {"canonical": "Reed Bunting",          "ebird": "Reed Bunting"},
    "Reed Warbler":          {"canonical": "Common Reed Warbler",   "ebird": "Common Reed Warbler"},
    "Ringed Plover":         {"canonical": "Common Ringed Plover",  "ebird": "Common Ringed Plover"},
    "Rock Pipit":            {"canonical": "Rock Pipit",            "ebird": "Rock Pipit"},
    "Sand Martin":           {"canonical": "Sand Martin",           "ebird": "Bank Swallow",          "note": "UK 'Sand Martin' kept over American 'Bank Swallow'"},
    "Sanderling":            {"canonical": "Sanderling",            "ebird": "Sanderling"},
    "Sandwich Tern":         {"canonical": "Sandwich Tern",         "ebird": "Sandwich Tern"},
    "Sedge Warbler":         {"canonical": "Sedge Warbler",         "ebird": "Sedge Warbler"},
    "Shag":                  {"canonical": "European Shag",         "ebird": "European Shag"},
    "Shelduck":              {"canonical": "Common Shelduck",       "ebird": "Common Shelduck"},
    "Shoveler":              {"canonical": "Northern Shoveler",     "ebird": "Northern Shoveler"},
    "Siskin":                {"canonical": "Eurasian Siskin",       "ebird": "Eurasian Siskin"},
    "Skylark":               {"canonical": "Eurasian Skylark",      "ebird": "Eurasian Skylark"},
    "Slavonian Grebe":       {"canonical": "Slavonian Grebe",       "ebird": "Horned Grebe",          "note": "UK 'Slavonian Grebe' kept over American 'Horned Grebe'"},
    "Snipe":                 {"canonical": "Common Snipe",          "ebird": "Common Snipe"},
    "Song Thrush":           {"canonical": "Song Thrush",           "ebird": "Song Thrush"},
    "Sparrowhawk":           {"canonical": "Eurasian Sparrowhawk",  "ebird": "Eurasian Sparrowhawk"},
    "Spoonbill":             {"canonical": "Eurasian Spoonbill",    "ebird": "Eurasian Spoonbill"},
    "Spotted Flycatcher":    {"canonical": "Spotted Flycatcher",    "ebird": "Spotted Flycatcher"},
    "Starling":              {"canonical": "Common Starling",       "ebird": "European Starling",     "note": "UK 'Common Starling' preferred over eBird's 'European Starling'"},
    "Stock Dove":            {"canonical": "Stock Dove",            "ebird": "Stock Dove"},
    "Swallow":               {"canonical": "Barn Swallow",          "ebird": "Barn Swallow"},
    "Swift":                 {"canonical": "Common Swift",          "ebird": "Common Swift"},
    "Tawny Owl":             {"canonical": "Tawny Owl",             "ebird": "Tawny Owl"},
    "Teal":                  {"canonical": "Eurasian Teal",         "ebird": "Green-winged Teal",     "note": "UK 'Eurasian Teal' preferred; eBird species page uses 'Green-winged Teal' (same Anas crecca)"},
    "Tree Pipit":            {"canonical": "Tree Pipit",            "ebird": "Tree Pipit"},
    "Turnstone":             {"canonical": "Ruddy Turnstone",       "ebird": "Ruddy Turnstone"},
    "Water Rail":            {"canonical": "Water Rail",            "ebird": "Water Rail"},
    "Wheatear":              {"canonical": "Northern Wheatear",     "ebird": "Northern Wheatear"},
    "Whinchat":              {"canonical": "Whinchat",              "ebird": "Whinchat"},
    "White Wagtail":         {"canonical": "White Wagtail",         "ebird": "White Wagtail"},
    "White-tailed Eagle":    {"canonical": "White-tailed Eagle",    "ebird": "White-tailed Eagle"},
    "Willow Warbler":        {"canonical": "Willow Warbler",        "ebird": "Willow Warbler"},
    "Woodcock":              {"canonical": "Eurasian Woodcock",     "ebird": "Eurasian Woodcock"},
    "Woodlark":              {"canonical": "Woodlark",              "ebird": "Wood Lark",             "note": "UK one-word 'Woodlark' kept"},
    "Yellow Wagtail":        {"canonical": "Western Yellow Wagtail","ebird": "Western Yellow Wagtail"},
    "Yellow-legged Gull":    {"canonical": "Yellow-legged Gull",    "ebird": "Yellow-legged Gull"},
}

GB_BASE = "https://www.goingbirding.co.uk/hants/species.aspx?species_id="
EB_BASE = "https://ebird.org/species/"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--goingbirding", default="data/goingbirding_birds.json")
    p.add_argument("--ebird", default="data/ebird_hotspots.json")
    p.add_argument("--out", default="data/species_canonical.json")
    args = p.parse_args()

    gb = json.load(open(args.goingbirding))
    eb = json.load(open(args.ebird))

    # gb_name → speciesId (first occurrence; the IDs are stable)
    gb_id_by_name: dict[str, int] = {}
    for r in gb["records"]:
        gb_id_by_name.setdefault(r["species"], r["speciesId"])

    # eBird commonName → entry
    eb_by_common: dict[str, dict] = {s["commonName"]: s for s in eb["species"]}

    # Sanity-check that every goingbirding name from records is in MANUAL_MAP.
    missing_gb = sorted(set(gb_id_by_name) - set(MANUAL_MAP))
    if missing_gb:
        print(f"WARN: {len(missing_gb)} goingbirding species not in MANUAL_MAP:", file=sys.stderr)
        for n in missing_gb:
            print(f"  - {n}", file=sys.stderr)

    species_out = []
    for gb_name, gb_id in sorted(gb_id_by_name.items()):
        m = MANUAL_MAP.get(gb_name, {"canonical": gb_name, "ebird": None})
        eb_name = m.get("ebird")
        eb_entry = eb_by_common.get(eb_name) if eb_name else None
        species_out.append({
            "canonical": m["canonical"],
            "scientificName": (eb_entry or {}).get("scientificName"),
            "family": (eb_entry or {}).get("family"),
            "exclude": m.get("exclude", False),
            "feral": m.get("feral", False),
            "note": m.get("note"),
            "goingbirding": {
                "name": gb_name,
                "id": gb_id,
                "url": GB_BASE + str(gb_id),
            },
            "ebird": {
                "name": eb_name,
                "speciesCode": (eb_entry or {}).get("speciesCode"),
                "url": EB_BASE + eb_entry["speciesCode"] if eb_entry else None,
            } if eb_name else None,
        })

    out = {
        "_meta": {
            "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "rule": "British-formal 'fancier' naming. Defaults to eBird's IOC name; UK form kept for the Anglo-American clashes (Skua/Jaeger, Guillemot/Murre, Diver/Loon, Brent/Brant, Goosander/Common Merganser, Grey Plover/Black-bellied Plover, Slavonian Grebe/Horned Grebe, Sand Martin/Bank Swallow, Great White Egret/Great Egret, Grey/Gray, Greylag/Graylag, Common Starling).",
            "totalSpecies": len(species_out),
            "excluded": sum(1 for s in species_out if s["exclude"]),
        },
        "species": species_out,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Wrote {args.out}: {len(species_out)} species, {out['_meta']['excluded']} excluded", file=sys.stderr)


if __name__ == "__main__":
    main()
