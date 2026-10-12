#!/usr/bin/env python3
"""Vantage win-probability updater.

Fetches Premier League results from football-data.org, fits team ratings,
and writes predictions.json for the home page. Standard library only.

  python scripts/update_predictions.py            # update predictions.json
  python scripts/update_predictions.py --backtest # score the model on past matches

Needs the FOOTBALL_DATA_API_KEY environment variable.
"""
import json, math, os, sys, time, datetime as dt, urllib.request, urllib.error
from collections import defaultdict

# ---- settings you can change -------------------------------------------------
FEATURED_CLUBS = ["MUN"]      # first fixture involving one of these is shown big
SHRINK = 8.0                  # pull toward average, in goals worth of evidence (bigger = calmer)
SEASON_DECAY = 0.5            # last season counts half as much as this one, the one before a quarter
RHO = -0.08                   # Dixon-Coles low-score correction; 0 turns it off. Typical value, NOT tuned by us.
TLA_FIX = {"NOT": "NFO", "NFO": "NFO"}   # API code -> our code
OUT = os.path.join(os.path.dirname(__file__), "..", "predictions.json")
API = "https://api.football-data.org/v4/competitions/PL/matches?season={}"
# -------------------------------------------------------------------------------


def fetch_season(season, key):
    req = urllib.request.Request(API.format(season), headers={"X-Auth-Token": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    out = []
    for m in data.get("matches", []):
        ft = (m.get("score") or {}).get("fullTime") or {}
        code = lambda t: TLA_FIX.get(t.get("tla"), t.get("tla"))
        out.append({
            "season": season, "matchday": m.get("matchday"), "status": m.get("status"),
            "utc": m.get("utcDate"),
            "h": code(m["homeTeam"]), "a": code(m["awayTeam"]),
            "hn": m["homeTeam"].get("shortName") or m["homeTeam"].get("name"),
            "an": m["awayTeam"].get("shortName") or m["awayTeam"].get("name"),
            "hg": ft.get("home"), "ag": ft.get("away"),
        })
    return out


def fit(rows, shrink=SHRINK, iters=300):
    """rows: (home, away, home_goals, away_goals, weight). Returns ratings and league means.
    Opponent-adjusted: each team's attack/defence is judged against who it played."""
    W = sum(r[4] for r in rows)
    mu_h = sum(r[4] * r[2] for r in rows) / W
    mu_a = sum(r[4] * r[3] for r in rows) / W
    teams = {r[0] for r in rows} | {r[1] for r in rows}
    att = {t: 1.0 for t in teams}
    dfn = {t: 1.0 for t in teams}
    for _ in range(iters):
        gf, ef, ga, ea = (defaultdict(float) for _ in range(4))
        for h, a, hg, ag, w in rows:
            gf[h] += w * hg; ef[h] += w * mu_h * dfn[a]
            gf[a] += w * ag; ef[a] += w * mu_a * dfn[h]
            ga[a] += w * hg; ea[a] += w * mu_h * att[h]
            ga[h] += w * ag; ea[h] += w * mu_a * att[a]
        na = {t: (gf[t] + shrink) / (ef[t] + shrink) for t in teams}
        nd = {t: (ga[t] + shrink) / (ea[t] + shrink) for t in teams}
        ma, md = sum(na.values()) / len(na), sum(nd.values()) / len(nd)
        att = {t: v / ma for t, v in na.items()}
        dfn = {t: v / md for t, v in nd.items()}
    return {"att": att, "dfn": dfn, "mu_h": mu_h, "mu_a": mu_a}


def _pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def predict(model, h, a):
    xh = model["mu_h"] * model["att"].get(h, 1.0) * model["dfn"].get(a, 1.0)
    xa = model["mu_a"] * model["att"].get(a, 1.0) * model["dfn"].get(h, 1.0)
    H = D = A = 0.0
    best = (0.0, 0, 0)
    for i in range(9):
        for j in range(9):
            tau = 1.0
            if i == 0 and j == 0: tau = 1 - xh * xa * RHO
            elif i == 0 and j == 1: tau = 1 + xh * RHO
            elif i == 1 and j == 0: tau = 1 + xa * RHO
            elif i == 1 and j == 1: tau = 1 - RHO
            p = _pois(i, xh) * _pois(j, xa) * tau
            if i > j: H += p
            elif i == j: D += p
            else: A += p
            if p > best[0]: best = (p, i, j)
    t = H + D + A
    return [H / t, D / t, A / t], xh, xa, best


def round100(ps):
    f = [p * 100 for p in ps]
    b = [int(x) for x in f]
    for _, i in sorted(((x - int(x), i) for i, x in enumerate(f)), reverse=True)[:100 - sum(b)]:
        b[i] += 1
    return b


def season_start(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


def weighted_rows(matches, target_season, before=None):
    rows = []
    for m in matches:
        if m["status"] != "FINISHED" or m["hg"] is None: continue
        if before and m["utc"] >= before: continue
        if m["season"] > target_season: continue
        rows.append((m["h"], m["a"], m["hg"], m["ag"], SEASON_DECAY ** (target_season - m["season"])))
    return rows


def _team(code, name):
    return {"code": code, "name": name}


def update(key):
    cur = season_start()
    matches = list(fetch_season(cur, key))
    try:
        time.sleep(1); matches += fetch_season(cur - 1, key)
    except Exception as e:  # free plans may not include history; carry on without it
        print("Could not fetch last season, continuing without it:", e)
    now_season = [m for m in matches if m["season"] == cur]
    open_states = ("SCHEDULED", "TIMED", "IN_PLAY", "PAUSED")

    # --- predictions: the next matchday that still has an unfinished game -------------
    up = [m for m in now_season if m["status"] in open_states]
    if not up:
        print("No upcoming fixtures found; leaving predictions.json unchanged."); return
    md = min(m["matchday"] for m in up)
    day = sorted((m for m in now_season if m["matchday"] == md), key=lambda m: m["utc"])
    # Predict every game from what was known BEFORE that matchday kicked off, so a game that
    # has already been played still shows the genuine pre-match prediction, not a hindsight one.
    rows = weighted_rows(matches, cur, before=day[0]["utc"])
    if len(rows) < 10:
        rows = weighted_rows(matches, cur)
    if len(rows) < 10:
        sys.exit("Not enough finished matches to fit a model; leaving predictions.json unchanged.")
    model = fit(rows)
    fixtures = []
    for m in day:
        (ph, pd_, pa), xh, xa, best = predict(model, m["h"], m["a"])
        fixtures.append({"home": _team(m["h"], m["hn"]), "away": _team(m["a"], m["an"]),
                         "kickoff": m["utc"], "pct": round100([ph, pd_, pa]),
                         "xg": [round(xh, 2), round(xa, 2)],
                         "likeliest": {"h": best[1], "a": best[2], "p": round(best[0], 3)}})
    feat = next((i for i, f in enumerate(fixtures) if f["home"]["code"] in FEATURED_CLUBS or f["away"]["code"] in FEATURED_CLUBS), 0)

    # --- results: the most recent matchday in which any game has finished -------------
    done_mds = [m["matchday"] for m in now_season if m["status"] == "FINISHED"]
    results = None
    if done_mds:
        rmd = max(done_mds)
        res = []
        for m in sorted((m for m in now_season if m["matchday"] == rmd), key=lambda m: m["utc"]):
            st = {"FINISHED": "FT", "IN_PLAY": "LIVE", "PAUSED": "LIVE", "POSTPONED": "POSTPONED"}.get(m["status"], "SCHEDULED")
            score = [m["hg"], m["ag"]] if st in ("FT", "LIVE") and m["hg"] is not None else None
            res.append({"home": _team(m["h"], m["hn"]), "away": _team(m["a"], m["an"]),
                        "kickoff": m["utc"], "status": st, "score": score})
        results = {"matchweek": rmd, "fixtures": res}

    out = {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "season": f"{cur}/{str(cur + 1)[2:]}", "matchweek": md, "featured": feat,
           "model": {"type": "opponent-adjusted Poisson", "shrink": SHRINK, "season_decay": SEASON_DECAY, "rho": RHO,
                     "matches_used": len(rows)},
           "fixtures": fixtures, "results": results}
    # Only rewrite the file when something real changed, so the "Updated" date means something.
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
        strip = lambda d: {k: v for k, v in d.items() if k != "updated"}
        if strip(old) == strip(out):
            print("No change since last run."); return
    except (FileNotFoundError, ValueError):
        pass
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(f"Wrote {len(fixtures)} fixtures (matchweek {md}) and {len(results['fixtures']) if results else 0} results.")


def backtest(key):
    cur = season_start()
    matches = []
    for s in (cur - 2, cur - 1, cur):
        try:
            matches += fetch_season(s, key); time.sleep(1)
        except Exception as e:
            print("skip season", s, e)
    done = [m for m in matches if m["status"] == "FINISHED" and m["hg"] is not None]
    targets = [m for m in done if m["season"] >= cur - 1]
    by_md = defaultdict(list)
    for m in targets: by_md[(m["season"], m["matchday"])].append(m)
    briers, base_briers, logl, pairs = [], [], [], []
    for (s, md), ms in sorted(by_md.items()):
        cutoff = min(m["utc"] for m in ms)
        rows = weighted_rows(done, s, before=cutoff)
        if len(rows) < 100: continue
        model = fit(rows, iters=120)
        n = sum(r[4] for r in rows)
        base = [sum(r[4] for r in rows if r[2] > r[3]) / n, sum(r[4] for r in rows if r[2] == r[3]) / n,
                sum(r[4] for r in rows if r[2] < r[3]) / n]
        for m in ms:
            p, *_ = predict(model, m["h"], m["a"])
            o = [m["hg"] > m["ag"], m["hg"] == m["ag"], m["hg"] < m["ag"]]
            briers.append(sum((p[k] - o[k]) ** 2 for k in range(3)))
            base_briers.append(sum((base[k] - o[k]) ** 2 for k in range(3)))
            logl.append(-math.log(max(p[o.index(True)], 1e-9)))
            pairs += [(p[k], 1.0 if o[k] else 0.0) for k in range(3)]
    if not briers: sys.exit("No matches could be scored.")
    n = len(briers)
    print(f"Scored {n} matches (walk-forward: each matchday predicted using only earlier results)")
    print(f"Brier score  model {sum(briers)/n:.4f}   always-use-base-rates {sum(base_briers)/n:.4f}   (lower is better)")
    print(f"Log loss     model {sum(logl)/n:.4f}")
    print("\nCalibration (predicted chance vs how often it happened):")
    for lo in range(0, 100, 10):
        b = [(p, y) for p, y in pairs if lo / 100 <= p < (lo + 10) / 100]
        if b: print(f"  {lo:>2}-{lo+10:<3}%  predicted {100*sum(p for p,_ in b)/len(b):5.1f}%   actual {100*sum(y for _,y in b)/len(b):5.1f}%   n={len(b)}")


if __name__ == "__main__":
    key = os.environ.get("FOOTBALL_DATA_API_KEY")
    if not key: sys.exit("Set FOOTBALL_DATA_API_KEY first.")
    try:
        backtest(key) if "--backtest" in sys.argv else update(key)
    except urllib.error.HTTPError as e:
        sys.exit(f"API error {e.code}: {e.reason}. predictions.json left unchanged.")
    except urllib.error.URLError as e:
        sys.exit(f"Network error: {e.reason}. predictions.json left unchanged.")

