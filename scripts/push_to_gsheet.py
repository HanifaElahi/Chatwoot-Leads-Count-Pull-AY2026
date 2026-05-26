#!/usr/bin/env python3
"""Push daily-leads-count-pull snapshot to a dedicated Google Sheet.

Snapshot mode — overwrites a single tab each run with the current state:
  - Cumulative totals (MDCAT, ECAT, BCAT, All) — total / F / M / U / F % classified
  - Past 7 days × product

Required env vars (set in ../.env or GitHub Secrets):
  GOOGLE_SERVICE_ACCOUNT_EMAIL
  GOOGLE_PRIVATE_KEY
  DAILY_COUNTS_SPREADSHEET_ID         (the dedicated sheet)
  DAILY_COUNTS_TAB_NAME               (optional, default 'snapshot')

Install: pip install gspread
"""
import os, csv, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import gspread

PKT = timezone(timedelta(hours=5))

HERE = Path(__file__).resolve().parent.parent          # daily-leads-count-pull/
ROOT = HERE.parent.parent                              # Qualified Leads Tagging/
SUMMARY = HERE / "summary"

# --- env (local .env wins; falls back to parent .env for shared creds) ---
for env_candidate in (HERE / ".env", ROOT / ".env", ROOT.parent / ".env"):
    if env_candidate.exists():
        for line in env_candidate.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Accept both DAILY_COUNTS_SPREADSHEET_ID (preferred) or plain SPREADSHEET_ID
SPREADSHEET_ID_KEY = "DAILY_COUNTS_SPREADSHEET_ID" if "DAILY_COUNTS_SPREADSHEET_ID" in os.environ else "SPREADSHEET_ID"

SA_EMAIL = os.environ["GOOGLE_SERVICE_ACCOUNT_EMAIL"]
PRIVATE_KEY = os.environ["GOOGLE_PRIVATE_KEY"].replace("\\n", "\n")
SPREADSHEET_ID = os.environ[SPREADSHEET_ID_KEY]
TAB_NAME = os.environ.get("DAILY_COUNTS_TAB_NAME", "AY2026_Leads_Count")


def build_snapshot_rows():
    rows = []
    # Header block
    rows.append(["Daily Leads Count — Snapshot"])
    rows.append([f"Last updated: {datetime.now(tz=PKT).strftime('%Y-%m-%d %H:%M')} PKT (GMT+5)"])
    rows.append([])
    # Cumulative totals
    rows.append(["Cumulative totals"])
    rows.append(["Product", "Total", "Female", "Male", "Uncategorized", "F % classified"])
    grand = Counter()
    for p in ("mdcat", "ecat", "bcat"):
        path = SUMMARY / p / "all_leads.csv"
        if not path.exists():
            rows.append([p.upper(), "—", "—", "—", "—", "—"])
            continue
        data = list(csv.DictReader(open(path)))
        g = Counter(r["gender"] for r in data)
        f, m, u = g.get("Female", 0), g.get("Male", 0), g.get("Uncategorized", 0)
        grand.update(g)
        cl = f + m
        rows.append([p.upper(), len(data), f, m, u, f"{100*f/cl:.1f}%" if cl else "—"])
    f, m, u = grand["Female"], grand["Male"], grand["Uncategorized"]
    cl = f + m; tot = f + m + u
    rows.append(["All (sum)", tot, f, m, u, f"{100*f/cl:.1f}%" if cl else "—"])
    rows.append([])
    # Past 7 days
    rows.append(["Past 7 days (by first_seen)"])
    rows.append(["Date", "MDCAT", "ECAT", "BCAT", "Total"])
    prod_rows = {}
    for p in ("mdcat", "ecat", "bcat"):
        path = SUMMARY / p / "all_leads.csv"
        prod_rows[p] = list(csv.DictReader(open(path))) if path.exists() else []
    today = datetime.now(tz=PKT).date()
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        counts = {p: sum(1 for r in prod_rows[p] if r.get("first_seen") == d) for p in ("mdcat","ecat","bcat")}
        rows.append([d, counts["mdcat"], counts["ecat"], counts["bcat"], sum(counts.values())])
    return rows


def main():
    creds = {
        "type": "service_account",
        "client_email": SA_EMAIL,
        "private_key": PRIVATE_KEY,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    gc = gspread.service_account_from_dict(creds)
    sheet = gc.open_by_key(SPREADSHEET_ID)

    # Get or create tab
    try:
        ws = sheet.worksheet(TAB_NAME)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=TAB_NAME, rows=50, cols=10)

    rows = build_snapshot_rows()
    ws.clear()
    ws.update(values=rows, range_name="A1")

    # ---- Formatting ----
    # Title (row 1) — bold, larger
    ws.format("A1:F1", {"textFormat": {"bold": True, "fontSize": 13}})
    # Timestamp (row 2) — italic, gray
    ws.format("A2:F2", {"textFormat": {"italic": True, "foregroundColor": {"red":0.4,"green":0.4,"blue":0.4}}})
    # Section header "Cumulative totals" (row 4) — bold, light fill
    ws.format("A4:F4", {"textFormat": {"bold": True, "fontSize": 11},
                        "backgroundColor": {"red":0.93,"green":0.95,"blue":1.0}})
    # Column headers (row 5) — bold
    ws.format("A5:F5", {"textFormat": {"bold": True},
                        "backgroundColor": {"red":0.95,"green":0.95,"blue":0.95}})
    # "All (sum)" row (row 9) — bold
    ws.format("A9:F9", {"textFormat": {"bold": True},
                        "backgroundColor": {"red":0.98,"green":0.93,"blue":0.85}})
    # Section header "Past 7 days" (row 11) — bold, light fill
    ws.format("A11:E11", {"textFormat": {"bold": True, "fontSize": 11},
                          "backgroundColor": {"red":0.93,"green":0.95,"blue":1.0}})
    # 7-day column headers (row 12)
    ws.format("A12:E12", {"textFormat": {"bold": True},
                          "backgroundColor": {"red":0.95,"green":0.95,"blue":0.95}})
    # Freeze first 2 rows so header stays visible
    ws.freeze(rows=2)
    # Auto-resize columns A-F
    sheet_id = ws._properties["sheetId"]
    sheet.batch_update({"requests": [{
        "autoResizeDimensions": {
            "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": 6}}}]})

    print(f"Pushed {len(rows)} rows to '{TAB_NAME}' tab in sheet {SPREADSHEET_ID}")


if __name__ == "__main__":
    main()
