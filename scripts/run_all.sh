#!/bin/bash
# Daily counts pull — last 3 days × {MDCAT, ECAT, BCAT}.
# Updates:
#   3-automation/daily-leads-count-pull/summary/<product>/all_leads.csv
#   3-automation/daily-leads-count-pull/README.md
#
# Usage:  bash 3-automation/daily-leads-count-pull/scripts/run_all.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERE="$(cd "$SCRIPT_DIR/.." && pwd)"           # .../daily-leads-count-pull/
cd "$HERE"

SINCE="$(date -v-3d +%Y-%m-%d 2>/dev/null || date -d '3 days ago' +%Y-%m-%d)"
echo "Daily counts pull · SINCE=$SINCE"

for P in mdcat ecat bcat; do
  SINCE="$SINCE" QUIET=1 PRODUCT="$P" python3 "$HERE/scripts/run.py" > "/tmp/daily_$P.log" 2>&1 &
done
wait

# Top-level dashboard
HERE="$HERE" python3 <<'PY'
import csv, os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(os.environ["HERE"])
SUM = ROOT / "summary"
out = []
out += [
    "# Daily Leads Dashboard",
    "",
    f"_Last run: {datetime.now().strftime('%Y-%m-%d %H:%M PKT')}_  ",
    f"_Window: Oct 1, 2025 → today_",
    "",
    "## Cumulative totals",
    "",
    "| Product | Total | Female | Male | Uncategorized | F % classified |",
    "|---|--:|--:|--:|--:|--:|",
]
grand = Counter()
for p in ("mdcat","ecat","bcat"):
    rows = list(csv.DictReader(open(SUM / p / "all_leads.csv")))
    g = Counter(r["gender"] for r in rows)
    f,m,u = g["Female"], g["Male"], g["Uncategorized"]; cl = f+m
    grand.update(g)
    out.append(f"| {p.upper()} | {len(rows):,} | {f:,} | {m:,} | {u:,} | {100*f/cl if cl else 0:.1f}% |")
f,m,u = grand["Female"], grand["Male"], grand["Uncategorized"]; cl = f+m; tot = f+m+u
out.append(f"| **All (sum)** | **{tot:,}** | **{f:,}** | **{m:,}** | **{u:,}** | **{100*f/cl if cl else 0:.1f}%** |")

out += [
    "",
    "## Past 7 days (by first_seen)",
    "",
    "| Date | MDCAT | ECAT | BCAT | Total |",
    "|---|--:|--:|--:|--:|",
]
today = datetime.now().date()
prod_rows = {p: list(csv.DictReader(open(SUM / p / "all_leads.csv"))) for p in ("mdcat","ecat","bcat")}
for i in range(6,-1,-1):
    d = (today - timedelta(days=i)).isoformat()
    counts = {p: sum(1 for r in prod_rows[p] if r["first_seen"] == d) for p in ("mdcat","ecat","bcat")}
    tot = sum(counts.values())
    out.append(f"| {d} | {counts['mdcat']:,} | {counts['ecat']:,} | {counts['bcat']:,} | {tot:,} |")
out += [""]
(ROOT / "README.md").write_text("\n".join(out))
print(f"\n=== Dashboard written: {ROOT/'README.md'} ===")
print("\n".join(out[5:14]))
PY

# Push to gsheet — runs if local .env exists OR env vars set externally
if [ -f "$HERE/.env" ] || [ -n "$DAILY_COUNTS_SPREADSHEET_ID" ] || [ -n "$SPREADSHEET_ID" ]; then
    echo ""
    echo "=== Pushing to gsheet ==="
    python3 "$HERE/scripts/push_to_gsheet.py" || echo "  (gsheet push failed — check creds/sheet ID)"
fi
