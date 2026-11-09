#!/usr/bin/env python3
"""
update_model_log.py  -  keeps the permanent prediction record behind the Model page.

Run it every time predictions.json is refreshed (same cron job / GitHub Action is fine):

    python3 update_model_log.py                       # uses ./predictions.json and ./data/prediction_log.json
    python3 update_model_log.py path/to/predictions.json path/to/data/prediction_log.json

What it does
  1. Adds every fixture that has a prediction (pct) and is not in the log yet.
  2. NEVER changes a prediction that is already logged, so the record cannot be rewritten after the fact.
  3. Fills in the final score for any logged match that now shows FT in predictions.json.
  4. Marks a prediction "late" if it was first logged after kick-off, so the page can say so.

The Model page (performance.html) and the Football > Model tab read the log and do all the scoring in the browser.
No third-party packages needed.
"""
import json, sys, os
from datetime import datetime, timezone

pred_path = sys.argv[1] if len(sys.argv) > 1 else "predictions.json"
log_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join("data", "prediction_log.json")

with open(pred_path, encoding="utf-8") as f:
    d = json.load(f)

comps = list(d["competitions"].values()) if "competitions" in d else [d]

log = []
if os.path.exists(log_path):
    with open(log_path, encoding="utf-8") as f:
        raw = json.load(f)
    log = raw["matches"] if isinstance(raw, dict) else raw

def key(c, hc, ac, k):
    return f"{c}|{hc}|{ac}|{k}"

index = {key(m["c"], m["hc"], m["ac"], m["k"]): m for m in log}
now = datetime.now(timezone.utc)
added = scored = 0

for c in comps:
    name = c.get("name", "Premier League")
    md = (c.get("results") or {}).get("matchweek") or c.get("matchweek")
    results = {(r["home"]["code"], r["away"]["code"], r["kickoff"]): r
               for r in (c.get("results") or {}).get("fixtures", [])}
    for f in c.get("fixtures", []):
        if not f.get("pct"):
            continue
        k = key(name, f["home"]["code"], f["away"]["code"], f["kickoff"])
        if k not in index:
            ko = datetime.fromisoformat(f["kickoff"].replace("Z", "+00:00"))
            m = {"c": name, "md": md, "k": f["kickoff"],
                 "h": f["home"]["name"], "hc": f["home"]["code"],
                 "a": f["away"]["name"], "ac": f["away"]["code"],
                 "p": f["pct"], "s": None,
                 "logged": now.isoformat(timespec="seconds"), "late": now > ko}
            index[k] = m
            log.append(m)
            added += 1
        m = index[k]
        r = results.get((f["home"]["code"], f["away"]["code"], f["kickoff"]))
        if r and r.get("status") == "FT" and r.get("score") and m.get("s") is None:
            m["s"] = r["score"]
            scored += 1

log.sort(key=lambda m: m["k"])
os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
with open(log_path, "w", encoding="utf-8") as f:
    json.dump({"updated": now.isoformat(timespec="seconds"), "matches": log}, f, indent=1, ensure_ascii=False)

done = sum(1 for m in log if m.get("s"))
print(f"{added} new predictions logged, {scored} newly scored. Log now holds {len(log)} ({done} scored).")
