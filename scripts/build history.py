#!/usr/bin/env python3
"""Vantage history builder + honest (walk-forward) model scorer.

1. Downloads past Premier League and La Liga results (with bookmaker odds) from football-data.co.uk
2. Replays your real model (the fit/predict code in update_predictions.py) on every past round,
   using ONLY results that existed before that round kicked off
3. Scores the predictions and writes the files the accuracy page reads

  python scripts/build_history.py              # download + score (about 3 past seasons)
  python scripts/build_history.py --seasons 2  # fewer seasons = faster
  python scripts/build_history.py --offline    # skip downloads, use data/raw/*.csv you already have

Writes: data/matches_history.json, data/predictions_history.json, model_stats.json
Standard library only. Put this file in the same folder as update_predictions.py.
"""
import csv, io, json, math, os, sys, datetime as dt, urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import update_predictions as up
except ImportError:
    import update_predictions_4 as up      # the name your download had

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA, RAW = os.path.join(ROOT, "data"), os.path.join(ROOT, "data", "raw")
LEAGUES = {"PL": ("Premier League", "E0"), "PD": ("La Liga", "SP1")}   # football-data.co.uk file codes
URL = "https://www.football-data.co.uk/mmz4281/{s}/{f}.csv"
ROUND_GAP_HOURS = 36      # a gap this long between kick-offs starts a new round
MIN_ROWS = 100            # same minimum as the live backtest


# ---------- 1. get the data ------------------------------------------------------------
def get_csv(season, fname, offline):
    os.makedirs(RAW, exist_ok=True)
    path = os.path.join(RAW, f"{fname}_{season}.csv")
    if not offline and (not os.path.exists(path) or season == up.season_start()):   # always refresh this season
        url = URL.format(s=f"{season % 100:02d}{(season + 1) % 100:02d}", f=fname)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                open(path, "wb").write(r.read())
            print(f"  downloaded {fname} {season}/{season + 1}")
        except Exception as e:
            print(f"  could not download {url}: {e}")
    if not os.path.exists(path):
        print(f"  MISSING {path} (download it by hand from football-data.co.uk and save it there)")
        return None
    return open(path, encoding="utf-8-sig", errors="replace").read()


def parse(text, season, code):
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        d, hg, ag = (r.get("Date") or "").strip(), (r.get("FTHG") or "").strip(), (r.get("FTAG") or "").strip()
        if not d or not hg or not ag or not r.get("HomeTeam"):
            continue
        day = None
        for f in ("%d/%m/%Y", "%d/%m/%y"):
            try: day = dt.datetime.strptime(d, f); break
            except ValueError: pass
        if day is None:
            continue
        hh, mm = ((r.get("Time") or "12:00").strip() or "12:00").split(":")[:2]
        odds = None
        for pre in ("Avg", "B365", "BbAv"):          # best source first
            try:
                o = [float(r[f"{pre}{x}"]) for x in "HDA"]
                if min(o) > 1.0: odds = o; break
            except (KeyError, ValueError):
                pass
        out.append({"season": season, "stage": "", "matchday": None, "status": "FINISHED", "ext": False,
                    "utc": f"{day:%Y-%m-%d}T{int(hh):02d}:{int(mm):02d}:00Z", "comp": code,
                    "h": r["HomeTeam"].strip(), "a": r["AwayTeam"].strip(),
                    "hn": r["HomeTeam"].strip(), "an": r["AwayTeam"].strip(),
                    "hg": int(hg), "ag": int(ag), "odds": odds})
    return out


def assign_rounds(ms):
    ms.sort(key=lambda m: m["utc"])
    idx, prev = -1, None
    for m in ms:
        t = up._parse(m["utc"])
        if prev is None or (t - prev).total_seconds() >= ROUND_GAP_HOURS * 3600:
            idx += 1
        m["rk"], prev = (0, idx), t


# ---------- 2. replay the model, no peeking -------------------------------------------
def replay(by_comp, score_from):
    preds = []
    for code, ms in by_comp.items():
        groups = defaultdict(list)
        for m in ms: groups[(m["season"], m["rk"])].append(m)
        for (s, _), grp in sorted(groups.items()):
            if s < score_from: continue
            cutoff = min(m["utc"] for m in grp)
            pool = [m for m in ms if m["season"] >= s - 1]          # same window the live model uses
            rows = up.weighted_rows(pool, s, before=cutoff)
            if len(rows) < MIN_ROWS: continue
            model = up.fit(rows, iters=120)
            n = sum(r[4] for r in rows)
            base = [sum(r[4] for r in rows if c(r)) / n for c in
                    (lambda r: r[2] > r[3], lambda r: r[2] == r[3], lambda r: r[2] < r[3])]
            for m in grp:
                p, *_ = up.predict(model, m["h"], m["a"])
                o = 0 if m["hg"] > m["ag"] else 1 if m["hg"] == m["ag"] else 2
                mk = None
                if m["odds"]:
                    inv = [1 / x for x in m["odds"]]; mk = [x / sum(inv) for x in inv]   # remove bookmaker margin
                preds.append({"comp": code, "season": s, "utc": m["utc"], "home": m["hn"], "away": m["an"],
                              "score": [m["hg"], m["ag"]], "o": o, "p": [round(x, 4) for x in p],
                              "b": [round(x, 4) for x in base], "m": [round(x, 4) for x in mk] if mk else None,
                              "source": "backtest"})
        print(f"  {code}: {sum(1 for p in preds if p['comp'] == code)} matches scored")
    return preds


# ---------- 3. score ----------------------------------------------------------------------
def rps(p, o):
    return 0.5 * ((p[0] - (o == 0)) ** 2 + (p[0] + p[1] - (o <= 1)) ** 2)

def hit(p, o): return p.index(max(p)) == o
def ll(p, o): return -math.log(max(p[o], 1e-9))
def brier(p, o): return sum((p[k] - (o == k)) ** 2 for k in range(3))

def summarize(ps):
    n = len(ps)
    if not n: return None
    f = lambda fn, key="p": sum(fn(x[key], x["o"]) for x in ps) / n
    r = lambda v, d=3: round(v, d)
    return {"n": n, "acc": r(100 * f(hit), 1), "rps": r(f(rps)), "brier": r(f(brier)), "logloss": r(f(ll)),
            "base_acc": r(100 * f(hit, "b"), 1), "base_rps": r(f(rps, "b")), "base_logloss": r(f(ll, "b")),
            "home_acc": r(100 * sum(x["o"] == 0 for x in ps) / n, 1)}

def stats(preds):
    ps = sorted(preds, key=lambda x: x["utc"])
    cal = []
    for b in range(10):
        pr = [(q, float(x["o"] == k)) for x in ps for k, q in enumerate(x["p"]) if min(int(q * 10), 9) == b]
        if pr: cal.append({"pred": round(100 * sum(q for q, _ in pr) / len(pr), 1),
                           "obs": round(100 * sum(y for _, y in pr) / len(pr), 1), "n": len(pr)})
    conf = []
    for lo, hi, lab in ((0, .4, "<40%"), (.4, .5, "40–50%"), (.5, .6, "50–60%"), (.6, .7, "60–70%"), (.7, 1.01, "70%+")):
        g = [x for x in ps if lo <= max(x["p"]) < hi]
        if g: conf.append({"label": lab, "n": len(g), "hit": round(100 * sum(hit(x["p"], x["o"]) for x in g) / len(g), 1)})
    timeline, ok, home = [], 0, 0
    for i, x in enumerate(ps, 1):
        ok += hit(x["p"], x["o"]); home += x["o"] == 0
        if i == len(ps) or ps[i]["utc"][:7] != x["utc"][:7]:
            timeline.append({"month": x["utc"][:7], "n": i, "acc": round(100 * ok / i, 1), "home": round(100 * home / i, 1)})
    mk = [x for x in ps if x["m"]]
    market = None
    if mk:
        g = lambda fn, key: round(sum(fn(x[key], x["o"]) for x in mk) / len(mk), 3)
        market = {"n": len(mk), "model_rps": g(rps, "p"), "market_rps": g(rps, "m"),
                  "model_acc": round(100 * sum(hit(x["p"], x["o"]) for x in mk) / len(mk), 1),
                  "market_acc": round(100 * sum(hit(x["m"], x["o"]) for x in mk) / len(mk), 1),
                  "model_logloss": g(ll, "p"), "market_logloss": g(ll, "m")}
    return {"overall": summarize(ps), "calibration": cal, "confidence": conf, "timeline": timeline, "market": market,
            "by_competition": [dict(code=c, name=LEAGUES[c][0], **summarize([x for x in ps if x["comp"] == c]))
                               for c in LEAGUES if any(x["comp"] == c for x in ps)],
            "by_season": [dict(season=f"{s}/{str(s + 1)[2:]}", **summarize([x for x in ps if x["season"] == s]))
                          for s in sorted({x["season"] for x in ps})]}


def main():
    args = sys.argv[1:]
    offline = "--offline" in args
    n = int(args[args.index("--seasons") + 1]) if "--seasons" in args else 3
    cur = up.season_start()
    by_comp, allm = {}, []
    for code, (name, fname) in LEAGUES.items():
        print(f"{name}:")
        ms = []
        for s in range(cur - n - 1, cur + 1):          # one extra season of warm-up before the first scored one
            text = get_csv(s, fname, offline)
            if text:
                part = parse(text, s, code); assign_rounds(part); ms += part
        if ms: by_comp[code] = ms; allm += ms
    if not by_comp: sys.exit("No data found.")
    os.makedirs(DATA, exist_ok=True)
    json.dump([{k: v for k, v in m.items() if k != "rk"} for m in allm],
              open(os.path.join(DATA, "matches_history.json"), "w"), separators=(",", ":"))
    print("Replaying the model (walk-forward), this takes a few minutes...")
    preds = replay(by_comp, cur - n)
    if not preds: sys.exit("Nothing could be scored. Check the files in data/raw/.")
    json.dump(preds, open(os.path.join(DATA, "predictions_history.json"), "w"), separators=(",", ":"))
    out = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "mode": "backtest",
           "method": "Each round predicted using only results from before kick-off (walk-forward). Same fit/predict code as the live model.",
           "model": {"shrink": up.SHRINK, "season_decay": up.SEASON_DECAY, "rho": up.RHO}, **stats(preds)}
    json.dump(out, open(os.path.join(ROOT, "model_stats.json"), "w"), indent=1, ensure_ascii=False)
    o = out["overall"]
    print(f"\nScored {o['n']} matches. Accuracy {o['acc']}%  RPS {o['rps']} (base rates {o['base_rps']})  Log loss {o['logloss']}")
    if out["market"]:
        m = out["market"]; print(f"Market odds on the same {m['n']} matches: RPS {m['market_rps']} vs model {m['model_rps']}")


if __name__ == "__main__":
    main()
