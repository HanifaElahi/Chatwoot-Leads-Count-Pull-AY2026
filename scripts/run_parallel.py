#!/usr/bin/env python3
"""Parallel variant of run.py — fetches conversation listing pages concurrently.

Same outputs as run.py (daily/summary/[<product>/]all_leads.csv + summary.md).
Use when you need a large historical backfill quickly.

Env:
    PRODUCT={mdcat|ecat|bcat}      default mdcat
    SINCE=YYYY-MM-DD               default = auto (last_seen-3d)
    WORKERS=N                      default 6 (parallel page fetchers)
    QUIET=1                        suppress per-page logs
"""
import os, sys, csv, json, re, time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent.parent.parent
PRODUCT = os.environ.get("PRODUCT", "mdcat").lower()
PRODUCT_UPPER = PRODUCT.upper()
SUMMARY = ROOT / "daily" / "summary" / PRODUCT
SUMMARY.mkdir(parents=True, exist_ok=True)

ENV_FILE = ROOT.parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE = os.environ["CHATWOOT_BASE_URL"].rstrip("/")
ACC = os.environ["CHATWOOT_ACCOUNT_ID"]
TOKEN = os.environ["CHATWOOT_API_TOKEN"]
HEADERS = {"api_access_token": TOKEN, "Content-Type": "application/json"}
INBOXES = sorted(int(x) for x in os.environ.get("INBOXES", "2,13,16,18,19,20,21,22,23").split(","))
WORKERS = int(os.environ.get("WORKERS", "6"))
QUIET = os.environ.get("QUIET", "0") == "1"

def compute_since():
    snap = SUMMARY / "all_leads.csv"
    if snap.exists():
        try:
            dates = [r["last_seen"] for r in csv.DictReader(open(snap)) if r.get("last_seen")]
            if dates:
                latest = datetime.strptime(max(dates), "%Y-%m-%d")
                return max(latest - timedelta(days=3), datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        except Exception: pass
    return (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

SINCE = os.environ.get("SINCE") or compute_since()

def fetch_product_labels():
    url = f"{BASE}/api/v1/accounts/{ACC}/labels"
    data = json.loads(urlopen(Request(url, headers=HEADERS), timeout=30).read())
    return sorted({l["title"] for l in data.get("payload", []) if PRODUCT in l["title"].lower()})

def fetch_inbox_names():
    url = f"{BASE}/api/v1/accounts/{ACC}/inboxes"
    data = json.loads(urlopen(Request(url, headers=HEADERS), timeout=30).read())
    return {i["id"]: i["name"] for i in data.get("payload", [])}

PRODUCT_LABELS = fetch_product_labels()
print(f"Fetched {len(PRODUCT_LABELS)} {PRODUCT_UPPER} labels from Chatwoot")
INBOX_NAMES = fetch_inbox_names()

sys.path.insert(0, str(ROOT / "daily"))
from gender import classify

def normalize_phone(raw):
    if not raw: return ""
    d = re.sub(r"\D", "", raw)
    if not d: return ""
    if d.startswith("0"): d = "92" + d[1:]
    if not d.startswith("92") and len(d) == 10: d = "92" + d
    return "+" + d

BODY = {"payload": [
    {"attribute_key": "created_at", "filter_operator": "is_greater_than", "values": [SINCE], "query_operator": "and"},
    {"attribute_key": "inbox_id", "filter_operator": "equal_to", "values": INBOXES, "query_operator": "and"},
    {"attribute_key": "labels", "filter_operator": "equal_to", "values": PRODUCT_LABELS},
]}

def fetch_page(p, max_retries=5):
    url = f"{BASE}/api/v1/accounts/{ACC}/conversations/filter?page={p}"
    data = json.dumps(BODY).encode()
    for attempt in range(max_retries):
        try:
            req = Request(url, data=data, headers=HEADERS, method="POST")
            with urlopen(req, timeout=60) as r:
                return p, json.loads(r.read())
        except HTTPError as e:
            if e.code in (429,500,502,503,504) and attempt < max_retries-1:
                time.sleep(min(60, 2**attempt)); continue
            raise
        except Exception:
            if attempt < max_retries-1:
                time.sleep(2**attempt); continue
            raise

# Page 1 first to learn total
print(f"Fetching {PRODUCT_UPPER} leads since {SINCE} ... (workers={WORKERS})")
_, d1 = fetch_page(1)
meta = d1.get("meta") or {}
total = meta.get("all_count") or meta.get("total_count") or 0
max_page = (total + 24) // 25
print(f"  total~{total}  pages={max_page}")
convs = {}
for c in (d1.get("payload") or []): convs[c["id"]] = c

t0 = time.time(); done = 1
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    futs = {ex.submit(fetch_page, p): p for p in range(2, max_page+1)}
    for fut in as_completed(futs):
        try:
            p, d = fut.result()
            for c in (d.get("payload") or []): convs[c["id"]] = c
        except Exception as e:
            print(f"  page {futs[fut]} ERROR: {e}", flush=True)
        done += 1
        if not QUIET and (done % 10 == 0 or done == max_page):
            rate = done / max(0.1, time.time()-t0)
            eta = (max_page - done) / max(0.1, rate)
            print(f"  pages {done}/{max_page}  cumulative={len(convs)}  rate={rate:.1f}/s  eta={eta:.0f}s", flush=True)

print(f"Fetched {len(convs)} unique conversations")

# ------ now collapse to leads ------
WHATSAPP_IDS = {2,19,21,22,23}
def channel_from_inbox(i):
    if i in WHATSAPP_IDS: return "WhatsApp"
    if i == 13: return "In App"
    if i == 20: return "Instagram"
    if i == 16: return "Messenger"
    if i == 18: return "Web"
    return INBOX_NAMES.get(i, f"inbox-{i}")

def source_from_labels(lb):
    s = lb.lower()
    if "meta" in s: return "Meta Paid"
    if "landing-page" in s or "blog" in s or "website" in s or "google" in s: return "Google Search"
    if "insta" in s or "socials" in s or "influencer" in s or "whatsapp-form" in s or "email-form" in s: return "Organic"
    return "Other"

leads = {}
for c in convs.values():
    contact = (c.get("meta") or {}).get("sender") or {}
    phone = normalize_phone(contact.get("phone_number"))
    if not phone: continue
    name = (contact.get("name") or "").strip()
    email = (contact.get("identifier") or contact.get("email") or "").strip().lower()
    labels = "|".join(c.get("labels") or [])
    created = c.get("created_at") or 0
    updated = c.get("last_activity_at") or created or 0
    inbox_id = c.get("inbox_id") or 0
    ad_id = ((c.get("custom_attributes") or {}).get("ad_source_id") or "")
    ibx = INBOX_NAMES.get(inbox_id, f"inbox-{inbox_id}")
    if phone in leads:
        r = leads[phone]
        r["_channels"].add(channel_from_inbox(inbox_id))
        r["_inboxes"].add(ibx)
        if updated > r["_last_ts"]:
            r["last_seen"] = datetime.fromtimestamp(updated, tz=timezone.utc).strftime("%Y-%m-%d")
            r["_last_ts"] = updated
        if created < r["_created_ts"]:
            r["first_seen"] = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
            r["_created_ts"] = created
            r["source_type"] = source_from_labels(labels)
        if ad_id and not r.get("ad_ids"): r["ad_ids"] = ad_id
        r["labels"] = "|".join(sorted(set((r["labels"]+"|"+labels).split("|")) - {""}))
    else:
        leads[phone] = {
            "phone": phone, "name": name, "email": email,
            "gender": classify(name, email),
            "_channels": {channel_from_inbox(inbox_id)},
            "_inboxes": {ibx},
            "source_type": source_from_labels(labels),
            "labels": labels, "ad_ids": ad_id,
            "first_seen": datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d") if created else "",
            "last_seen": datetime.fromtimestamp(updated, tz=timezone.utc).strftime("%Y-%m-%d") if updated else "",
            "_created_ts": created, "_last_ts": updated,
        }

for p in leads:
    chs = leads[p].pop("_channels"); ibs = leads[p].pop("_inboxes")
    leads[p]["channel"] = "Both" if len(chs) > 1 else next(iter(chs))
    leads[p]["inboxes"] = "|".join(sorted(ibs))
    leads[p].pop("_created_ts", None); leads[p].pop("_last_ts", None)

# Merge with existing master (idempotent — same logic as run.py)
cols = ["phone","name","email","gender","channel","inboxes","source_type","labels","ad_ids","first_seen","last_seen"]
snap = SUMMARY / "all_leads.csv"
existing = {}
if snap.exists():
    for r in csv.DictReader(open(snap)): existing[r["phone"]] = r

for p, row in leads.items():
    if p in existing:
        e = existing[p]
        if e.get("gender") == "Uncategorized" and row["gender"] != "Uncategorized":
            e["gender"] = row["gender"]; e["name"] = row["name"] or e.get("name","")
        if row["last_seen"] > (e.get("last_seen") or ""): e["last_seen"] = row["last_seen"]
        if row.get("first_seen") and e.get("first_seen") and row["first_seen"] < e["first_seen"]:
            e["first_seen"] = row["first_seen"]; e["source_type"] = row["source_type"]
        prev_ch = e.get("channel") or ""; new_ch = row.get("channel") or ""
        if prev_ch and new_ch and prev_ch != new_ch: e["channel"] = "Both"
        elif not prev_ch: e["channel"] = new_ch
        e["labels"] = "|".join(sorted(set((e.get("labels","")+"|"+row["labels"]).split("|")) - {""}))
        e["inboxes"] = "|".join(sorted(set((e.get("inboxes","")+"|"+row.get("inboxes","")).split("|")) - {""}))
        if row.get("ad_ids") and not e.get("ad_ids"): e["ad_ids"] = row["ad_ids"]
    else:
        existing[p] = {c: row.get(c,"") for c in cols}

with open(snap, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
    for p in existing: w.writerow({c: existing[p].get(c,"") for c in cols})

g = Counter(existing[p]["gender"] for p in existing)
print(f"\nDone. Master: {snap}  ({len(existing):,} unique phones)")
print(f"  Female {g.get('Female',0):,}  Male {g.get('Male',0):,}  Uncategorized {g.get('Uncategorized',0):,}")
