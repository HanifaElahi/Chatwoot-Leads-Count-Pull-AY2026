#!/usr/bin/env python3
"""Daily MDCAT lead pull.

Pulls the last 3 days of MDCAT conversations from Chatwoot, classifies gender,
merges into the running master, and updates two files:
  - daily/summary/all_leads.csv  — cumulative master (unique by phone)
(README is generated at daily/README.md by daily/scripts/run_all.sh after all 3 products finish.)

Manual run:  python3 daily/scripts/run.py
"""
import os, sys, csv, json, re, time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import Counter
from urllib.request import Request, urlopen
from urllib.error import HTTPError

HERE = Path(__file__).resolve().parent.parent          # .../daily-leads-count-pull/
ROOT = HERE.parent.parent                              # .../Qualified Leads Tagging/
PRODUCT = os.environ.get("PRODUCT", "mdcat").lower()   # mdcat | ecat | bcat
PRODUCT_UPPER = PRODUCT.upper()
SUMMARY = HERE / "summary" / PRODUCT
SUMMARY.mkdir(parents=True, exist_ok=True)

# --- env ---
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
# All inboxes that may carry MDCAT conversations.
# 2/19/21/22/23 = WhatsApp variants, 13 = In App, 16 = Messenger, 18 = Counsellor (web widget), 20 = Instagram
INBOXES = sorted(int(x) for x in os.environ.get("INBOXES", "2,13,16,18,19,20,21,22,23").split(","))
QUIET = os.environ.get("QUIET", "0") == "1"

def compute_since():
    """Auto-compute fetch start date: last_seen in master minus 3-day buffer.
    Falls back to 30 days ago if master is empty or missing."""
    buffer = timedelta(days=3)
    default_back = timedelta(days=30)
    snap = SUMMARY / "all_leads.csv"
    if snap.exists():
        try:
            dates = [r["last_seen"] for r in csv.DictReader(open(snap)) if r.get("last_seen")]
            if dates:
                latest = datetime.strptime(max(dates), "%Y-%m-%d")
                start = max(latest - buffer, datetime.now() - default_back)
                return start.strftime("%Y-%m-%d")
        except Exception: pass
    return (datetime.now() - default_back).strftime("%Y-%m-%d")

SINCE = os.environ.get("SINCE") or compute_since()

def fetch_product_labels():
    """Pull every label from Chatwoot whose name contains the PRODUCT key (case-insensitive)."""
    url = f"{BASE}/api/v1/accounts/{ACC}/labels"
    req = Request(url, headers=HEADERS)
    data = json.loads(urlopen(req, timeout=30).read())
    labels = sorted({l["title"] for l in data.get("payload", []) if PRODUCT in l["title"].lower()})
    print(f"Fetched {len(labels)} {PRODUCT_UPPER} labels from Chatwoot")
    return labels

PRODUCT_LABELS = fetch_product_labels()

def fetch_inbox_names():
    url = f"{BASE}/api/v1/accounts/{ACC}/inboxes"
    data = json.loads(urlopen(Request(url, headers=HEADERS), timeout=30).read())
    return {i["id"]: i["name"] for i in data.get("payload", [])}

INBOX_NAMES = fetch_inbox_names()

# --- gender classifier (lives in daily/gender.py — copy that + dictionaries/ to port elsewhere) ---
sys.path.insert(0, str(HERE))
from gender import classify  # noqa: E402

def normalize_phone(raw):
    if not raw: return ""
    d = re.sub(r"\D", "", raw)
    if not d: return ""
    if d.startswith("0"): d = "92" + d[1:]
    if not d.startswith("92") and len(d) == 10: d = "92" + d
    return "+" + d

def http_post(url, body):
    data = json.dumps(body).encode()
    for attempt in range(6):
        req = Request(url, data=data, headers=HEADERS, method="POST")
        try:
            with urlopen(req, timeout=60) as r: return json.loads(r.read())
        except HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(min(60, 2 ** attempt)); continue
            print(f"HTTP {e.code}: {e.read()[:200]}", file=sys.stderr); raise

def fetch_recent():
    print(f"Fetching {PRODUCT_UPPER} leads since {SINCE} ...")
    body = {"payload": [
        {"attribute_key": "created_at", "filter_operator": "is_greater_than", "values": [SINCE], "query_operator": "and"},
        {"attribute_key": "inbox_id", "filter_operator": "equal_to", "values": INBOXES, "query_operator": "and"},
        {"attribute_key": "labels", "filter_operator": "equal_to", "values": PRODUCT_LABELS},
    ]}
    convs = {}
    page = 1
    while True:
        url = f"{BASE}/api/v1/accounts/{ACC}/conversations/filter?page={page}"
        d = http_post(url, body)
        batch = d.get("payload") or []
        for c in batch: convs[c["id"]] = c
        meta = d.get("meta") or {}
        total = meta.get("all_count") or meta.get("total_count") or 0
        if not QUIET:
            print(f"  page {page}: +{len(batch)}  cumulative={len(convs)}  / total~{total}", flush=True)
        if len(batch) < 25 or page * 25 >= total: break
        page += 1
        if page > 500: break
    return list(convs.values())

WHATSAPP_IDS = {2, 19, 21, 22, 23}
def channel_from_inbox(inbox_id):
    if inbox_id in WHATSAPP_IDS: return "WhatsApp"
    if inbox_id == 13: return "In App"
    if inbox_id == 20: return "Instagram"
    if inbox_id == 16: return "Messenger"
    if inbox_id == 18: return "Web"
    return INBOX_NAMES.get(inbox_id, f"inbox-{inbox_id}")

def source_from_labels(labels_str):
    lb = labels_str.lower()
    if "meta" in lb: return "Meta Paid"
    if "landing-page" in lb or "blog" in lb or "website" in lb or "google" in lb: return "Google Search"
    if "insta" in lb or "socials" in lb or "influencer" in lb or "whatsapp-form" in lb or "email-form" in lb: return "Organic"
    return "Other"

def to_leads(convs):
    leads = {}
    for c in convs:
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

        ibx_name = INBOX_NAMES.get(inbox_id, f"inbox-{inbox_id}")
        if phone in leads:
            prev = leads[phone]
            prev["_channels"].add(channel_from_inbox(inbox_id))
            prev["_inboxes"].add(ibx_name)
            if updated > prev["last_ts"]:
                prev["last_seen"] = datetime.fromtimestamp(updated, tz=timezone.utc).strftime("%Y-%m-%d")
                prev["last_ts"] = updated
            # if this conversation is EARLIER than what we have, it becomes the first-touch
            if created < prev["created_ts"]:
                prev["first_seen"] = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
                prev["created_ts"] = created
                prev["source_type"] = source_from_labels(labels)  # first-touch source
            if ad_id and not prev.get("ad_ids"): prev["ad_ids"] = ad_id
            prev["labels"] = "|".join(sorted(set((prev["labels"]+"|"+labels).split("|")) - {""}))
            continue

        leads[phone] = {
            "phone": phone, "name": name, "email": email,
            "gender": classify(name, email),
            "_channels": {channel_from_inbox(inbox_id)},
            "_inboxes": {ibx_name},
            "source_type": source_from_labels(labels),  # source from the first conversation we see
            "labels": labels,
            "ad_ids": ad_id,
            "first_seen": datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d") if created else "",
            "last_seen": datetime.fromtimestamp(updated, tz=timezone.utc).strftime("%Y-%m-%d") if updated else "",
            "created_ts": created,
            "last_ts": updated,
        }
    for p in leads:
        chs = leads[p].pop("_channels")
        leads[p]["channel"] = "Both" if len(chs) > 1 else next(iter(chs))
        ibs = leads[p].pop("_inboxes")
        leads[p]["inboxes"] = "|".join(sorted(ibs))
    return leads

def main():
    today = datetime.now().strftime("%Y-%m-%d")
    convs = fetch_recent()
    new_pool = to_leads(convs)
    print(f"\nUnique phones since {SINCE}: {len(new_pool)}")

    snap_path = SUMMARY / "all_leads.csv"
    cols = ["phone","name","email","gender","channel","inboxes","source_type","labels","ad_ids","first_seen","last_seen"]
    existing = {}
    if snap_path.exists():
        for r in csv.DictReader(open(snap_path)): existing[r["phone"]] = r

    added_today = []
    reengaged = 0
    refined = 0
    for p, row in new_pool.items():
        if p in existing:
            reengaged += 1
            # gender refinement
            if existing[p].get("gender") == "Uncategorized" and row["gender"] != "Uncategorized":
                existing[p]["gender"] = row["gender"]
                existing[p]["name"] = row["name"] or existing[p].get("name", "")
                refined += 1
            if row["last_seen"] > (existing[p].get("last_seen") or ""):
                existing[p]["last_seen"] = row["last_seen"]
            # if a newly-seen conversation is EARLIER than the existing first_seen,
            # overwrite source_type to reflect the true first-touch
            if row.get("first_seen") and existing[p].get("first_seen") and row["first_seen"] < existing[p]["first_seen"]:
                existing[p]["first_seen"] = row["first_seen"]
                existing[p]["source_type"] = row["source_type"]
            # merge channel — if a new run brings a different channel, mark as "Both"
            prev_ch = existing[p].get("channel") or ""
            new_ch = row.get("channel") or ""
            if prev_ch and new_ch and prev_ch != new_ch:
                existing[p]["channel"] = "Both"
            elif not prev_ch:
                existing[p]["channel"] = new_ch
            # merge labels
            merged_labels = "|".join(sorted(set((existing[p].get("labels","")+"|"+row["labels"]).split("|")) - {""}))
            existing[p]["labels"] = merged_labels
            # merge inboxes (set union of inbox names)
            merged_ibx = "|".join(sorted(set((existing[p].get("inboxes","")+"|"+row.get("inboxes","")).split("|")) - {""}))
            existing[p]["inboxes"] = merged_ibx
            if row.get("ad_ids") and not existing[p].get("ad_ids"):
                existing[p]["ad_ids"] = row["ad_ids"]
        else:
            existing[p] = {c: row.get(c, "") for c in cols}
            added_today.append(row)

    with open(snap_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for p in existing: w.writerow({c: existing[p].get(c, "") for c in cols})

    today_counter = Counter(r["gender"] for r in added_today)
    counter = Counter(existing[p]["gender"] for p in existing)
    t_f, t_m, t_u = today_counter.get("Female",0), today_counter.get("Male",0), today_counter.get("Uncategorized",0)
    f_n, m_n, u_n = counter.get("Female",0), counter.get("Male",0), counter.get("Uncategorized",0)

    print(f"\nDone.")
    print(f"  +{len(added_today)} new today ({t_f}F / {t_m}M / {t_u}U), {reengaged} re-engaged, {refined} refined")
    print(f"  Master: {snap_path}  ({len(existing):,} unique phones — F {f_n:,} / M {m_n:,} / U {u_n:,})")

if __name__ == "__main__":
    main()
