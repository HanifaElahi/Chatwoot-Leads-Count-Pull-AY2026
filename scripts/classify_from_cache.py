#!/usr/bin/env python3
"""One-pass cache classifier for MDCAT + ECAT + BCAT.

Reads cache/conversations/*.json (no Chatwoot API calls), classifies each
lead's gender via daily/gender.py, and writes a unified output:

    daily/summary/all_leads.csv   — one row per phone; `products` field
                                    is pipe-joined: "MDCAT", "MDCAT|ECAT", etc.
    daily/summary/summary.md      — 4 tables per product: totals / inbox /
                                    month-over-month / past 7 days.

Run:
    python3 daily/scripts/classify_from_cache.py
    SINCE=2025-10-01 python3 daily/scripts/classify_from_cache.py   # default
"""
import os, sys, csv, json, re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE = ROOT / "cache" / "conversations"
SUMMARY = ROOT / "daily" / "summary"
SUMMARY.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT / "daily"))
from gender import classify

PRODUCTS = ["mdcat", "ecat", "bcat"]
SINCE = os.environ.get("SINCE", "2025-10-01")

WHATSAPP_IDS = {2, 19, 21, 22, 23}
INBOX_NAMES = {
    2: "WhatsApp Primary", 13: "In App", 16: "Messenger", 18: "Counsellor",
    19: "WhatsApp Secondary", 20: "Instagram", 21: "WhatsApp Outbound",
    22: "WhatsApp Free Trial", 23: "WhatsApp OTP",
}

def normalize_phone(raw):
    if not raw: return ""
    d = re.sub(r"\D", "", raw)
    if not d: return ""
    if d.startswith("0"): d = "92" + d[1:]
    if not d.startswith("92") and len(d) == 10: d = "92" + d
    return "+" + d

def source_from_labels(lb):
    s = lb.lower()
    if "meta" in s: return "Meta Paid"
    if "landing-page" in s or "blog" in s or "website" in s or "google" in s: return "Google Search"
    if "insta" in s or "socials" in s or "influencer" in s or "whatsapp-form" in s or "email-form" in s: return "Organic"
    return "Other"


def main():
    since_epoch = int(datetime.strptime(SINCE, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    leads = {}  # phone -> aggregated record (across all products)

    files = sorted(CACHE.glob("*.json"))
    print(f"[scan] {len(files)} cached conversations (SINCE={SINCE})")
    for i, f in enumerate(files, 1):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        meta = d.get("meta") or {}
        labels = set(meta.get("labels") or [])
        if not labels: continue
        lb_lower = " ".join(l.lower() for l in labels)
        prods_here = [p for p in PRODUCTS if p in lb_lower]
        if not prods_here: continue

        contact = meta.get("contact") or {}
        msgs = d.get("payload") or []
        if not msgs: continue
        first_ts = min((m.get("created_at", 0) for m in msgs if m.get("created_at")), default=0)
        last_ts = max((m.get("created_at", 0) for m in msgs if m.get("created_at")), default=0)
        if first_ts < since_epoch: continue
        inbox_id = msgs[0].get("inbox_id") or 0

        phone = normalize_phone(contact.get("phone_number"))
        if not phone: continue
        name = (contact.get("name") or "").strip()
        email = (contact.get("email") or "").strip().lower()
        labels_str = "|".join(sorted(labels))
        ad_id = ((contact.get("custom_attributes") or {}).get("ad_source_id") or "")
        ibx_name = INBOX_NAMES.get(inbox_id, f"inbox-{inbox_id}")
        src = source_from_labels(labels_str)
        first_seen = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        last_seen = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        if phone in leads:
            r = leads[phone]
            for p in prods_here: r["_products"].add(p.upper())
            r["_inboxes"].add(ibx_name)
            if first_ts < r["_first_ts"]:
                r["_first_ts"] = first_ts; r["first_seen"] = first_seen
                r["source_type"] = src
            if last_ts > r["_last_ts"]:
                r["_last_ts"] = last_ts; r["last_seen"] = last_seen
            if not r.get("name") and name: r["name"] = name
            if not r.get("email") and email: r["email"] = email
            if ad_id and not r.get("ad_ids"): r["ad_ids"] = ad_id
            r["_labels"].update(labels)
        else:
            leads[phone] = {
                "phone": phone, "name": name, "email": email,
                "gender": classify(name, email),
                "_products": {p.upper() for p in prods_here},
                "_inboxes": {ibx_name},
                "source_type": src,
                "_labels": set(labels),
                "ad_ids": ad_id,
                "first_seen": first_seen, "last_seen": last_seen,
                "_first_ts": first_ts, "_last_ts": last_ts,
            }
        if i % 10000 == 0:
            print(f"[scan]   {i}/{len(files)}  unique leads={len(leads)}")

    # Finalise
    rows = []
    for p in leads:
        r = leads[p]
        rows.append({
            "phone": r["phone"], "name": r["name"], "email": r["email"],
            "gender": r["gender"],
            "products": "|".join(sorted(r.pop("_products"))),
            "inboxes": "|".join(sorted(r.pop("_inboxes"))),
            "source_type": r["source_type"],
            "labels": "|".join(sorted(r.pop("_labels"))),
            "ad_ids": r.get("ad_ids", ""),
            "first_seen": r["first_seen"], "last_seen": r["last_seen"],
        })

    cols = ["phone","name","email","gender","products","inboxes","source_type","labels","ad_ids","first_seen","last_seen"]
    with open(SUMMARY / "all_leads.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    print(f"\nWrote {len(rows):,} unique leads -> {SUMMARY/'all_leads.csv'}")

    # ---------- summary.md ----------
    write_summary_md(rows)

def write_summary_md(rows):
    today_dt = datetime.now()
    cur_month = today_dt.strftime("%Y-%m")
    prev_month = (today_dt.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    cutoff_7 = (today_dt - timedelta(days=6)).strftime("%Y-%m-%d")

    def row_md(label, gc, total_override=None):
        f = gc.get("Female",0); m = gc.get("Male",0); u = gc.get("Uncategorized",0)
        tot = total_override if total_override is not None else (f + m + u)
        cl = f + m
        pct = f"{100*f/cl:.1f}%" if cl else "—"
        return f"| {label} | {tot:,} | {f:,} | {m:,} | {u:,} | {pct} |"

    md = [
        "# Leads — Summary (MDCAT · ECAT · BCAT)",
        "",
        f"_Generated from cache: {datetime.now().strftime('%Y-%m-%d %H:%M')}  ·  window: first_seen ≥ {SINCE}_",
        "",
    ]

    # ---- Table 1: cumulative totals
    md += ["## 1) Cumulative totals", "",
           "| Product | Total | Female | Male | Uncategorized | F % classified |",
           "|---|--:|--:|--:|--:|--:|"]
    for prod in PRODUCTS:
        sub = [r for r in rows if prod.upper() in r["products"].split("|")]
        gc = Counter(r["gender"] for r in sub)
        md.append(row_md(prod.upper(), gc, total_override=len(sub)))
    # "Any product" row
    gc_all = Counter(r["gender"] for r in rows)
    md.append(row_md("**Any (dedup)**", gc_all, total_override=len(rows)))
    md.append("")

    # ---- Table 2: by inbox
    md += ["## 2) By inbox", "",
           "| Product | Inbox | Leads | Female | Male | Uncategorized | F % classified |",
           "|---|---|--:|--:|--:|--:|--:|"]
    for prod in PRODUCTS:
        sub = [r for r in rows if prod.upper() in r["products"].split("|")]
        ibx_ct = Counter(); ibx_g = {}
        for r in sub:
            for ib in (r.get("inboxes") or "").split("|"):
                if not ib: continue
                ibx_ct[ib] += 1
                ibx_g.setdefault(ib, Counter())[r["gender"]] += 1
        for ib, n in ibx_ct.most_common():
            gc = ibx_g[ib]
            md.append(f"| {prod.upper()} | {ib} | {n:,} | "
                      f"{gc.get('Female',0):,} | {gc.get('Male',0):,} | {gc.get('Uncategorized',0):,} | "
                      f"{(100*gc.get('Female',0)/(gc.get('Female',0)+gc.get('Male',0))):.1f}%"
                      if (gc.get('Female',0)+gc.get('Male',0)) else
                      f"| {prod.upper()} | {ib} | {n:,} | {gc.get('Female',0):,} | {gc.get('Male',0):,} | {gc.get('Uncategorized',0):,} | —"
                      )
            # Make sure each row has trailing pipe
            if not md[-1].endswith("|"): md[-1] += " |"
    md.append("")

    # ---- Table 3: month-over-month
    md += ["## 3) Current month vs previous (by first_seen)", "",
           "| Product | Month | Leads | Female | Male | Uncategorized | F % classified |",
           "|---|---|--:|--:|--:|--:|--:|"]
    for prod in PRODUCTS:
        sub = [r for r in rows if prod.upper() in r["products"].split("|")]
        for label, ym in [(prev_month, prev_month), (f"**{cur_month}**", cur_month)]:
            g = Counter(r["gender"] for r in sub if r["first_seen"].startswith(ym))
            tot = sum(g.values())
            f = g.get("Female",0); m = g.get("Male",0); u = g.get("Uncategorized",0)
            cl = f + m
            pct = f"{100*f/cl:.1f}%" if cl else "—"
            md.append(f"| {prod.upper()} | {label} | {tot:,} | {f:,} | {m:,} | {u:,} | {pct} |")
    md.append("")

    # ---- Table 4: past 7 days
    md += ["## 4) Past 7 days (by first_seen)", "",
           "| Product | Date | Leads | Female | Male | Uncategorized | F % classified |",
           "|---|---|--:|--:|--:|--:|--:|"]
    for prod in PRODUCTS:
        sub = [r for r in rows if prod.upper() in r["products"].split("|")]
        for i in range(6, -1, -1):
            d = (today_dt - timedelta(days=i)).strftime("%Y-%m-%d")
            g = Counter(r["gender"] for r in sub if r["first_seen"] == d)
            tot = sum(g.values())
            f = g.get("Female",0); m = g.get("Male",0); u = g.get("Uncategorized",0)
            cl = f + m
            pct = f"{100*f/cl:.1f}%" if cl else "—"
            md.append(f"| {prod.upper()} | {d} | {tot:,} | {f:,} | {m:,} | {u:,} | {pct} |")
    md.append("")

    (SUMMARY / "summary.md").write_text("\n".join(md))
    print(f"Wrote {SUMMARY/'summary.md'}")


if __name__ == "__main__":
    main()
