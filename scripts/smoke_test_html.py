#!/usr/bin/env python3
"""
Smoke-test index.html — verifies the tide-split refactor still
renders correctly in both paths:
  1. tides.json fetch succeeds → uses real data for today (or sample if no entry)
  2. tides.json fetch fails    → falls back to inlined TIDE_FALLBACK ("__sample__")

Spawns a headless browser via Playwright (preferred) or falls back to a Node
harness that stubs document/fetch. If neither is present, prints what's missing.

Usage: python3 scripts/smoke_test_html.py
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "index.html"
TIDES = ROOT / "tides.json"
VERSES = ROOT / "verses.json"


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run_node_harness(stub_verses: bool) -> int:
    """Headless: load SunCalc, stub DOM + fetch, eval app script, await microtasks.

    If stub_verses, verses.json is served from disk by the fetch stub (real
    catalogue path). Otherwise it 404s and the inline VERSES_FALLBACK kicks in.
    Both paths should render the page.
    """
    if not have("node"):
        print("node not installed — install Node.js to run the smoke test")
        return 2
    harness = Path(__file__).with_suffix(".js")
    argv = ["node", str(harness), str(HTML), str(TIDES)]
    if stub_verses:
        argv.append(str(VERSES))
    p = subprocess.run(argv, capture_output=True, text=True)
    print(p.stdout, end="")
    if p.stderr:
        print("--- stderr ---", file=sys.stderr)
        print(p.stderr, file=sys.stderr)
    return p.returncode


if __name__ == "__main__":
    if not HTML.exists():
        print(f"missing {HTML}")
        sys.exit(2)
    if not TIDES.exists():
        print(f"missing {TIDES}")
        sys.exit(2)
    rcs = []
    for label, stub in [("verses.json fetched", True), ("verses.json missing (fallback)", False)]:
        if stub and not VERSES.exists():
            print(f"--- skip: {label} — {VERSES} not present ---")
            continue
        print(f"--- run: {label} ---")
        rcs.append(run_node_harness(stub_verses=stub))
    sys.exit(max(rcs) if rcs else 2)
