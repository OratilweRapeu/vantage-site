#!/usr/bin/env python3
"""Vantage win-probability updater.

Fetches Premier League, La Liga and Champions League results from football-data.org,
fits team ratings per competition, and writes predictions.json for the home page.
Standard library only.

  python scripts/update_predictions.py                 # update predictions.json
  python scripts/update_predictions.py --backtest      # score the model (Premier League)
  python scripts/update_predictions.py --backtest CL   # ...or PD / CL

Needs the FOOTBALL_DATA_API_KEY environment variable.
"""
import json, math, os, sys, time, datetime as dt, urllib.request, urllib.error
from collections import defaultdict

# ---- settings you can change -------------------------------------------------
# One entry per competition shown on the page (the order here is the tab order).
#   kind     "league" = rounds are matchdays; "cup" = rounds are stages/legs (Champions League)
#   featured clubs for the big card, in order of preference; falls back to the first fixture
COMPETITIONS = {
    "PL": {"name": "Premier League",   "kind": "league", "featured": ["MUN"]},
    "PD": {"name": "La Liga",          "kind": "league", "featured": ["RMA", "BAR"]},
    "CL": {"name": "Champions League", "kind": "cup",    "featured": ["MUN"]},
}
SHRINK = 8.0                  # pull toward average, in goals worth of evidence (bigger = calmer)
SEASON_DECAY = 0.5            # last season counts half as much as this one, the one before a quarter
RHO = -0.08                   # Dixon-Coles low-score correction; 0 turns it off. Typical value, NOT tuned by us.
TLA_FIX = {"NOT": "NFO", "NFO": "NFO"}   # API code -> our code
# Some clubs share a three-letter code in the API (e.g. Bayern and Barcelona). Map the API's full
# team name to a unique code of ours. If two crests ever show the same letters, add the club here.
NAME_FIX = {"FC Bayern M\u00fcnchen": "BAY", "FC Barcelona": "BAR", "Celtic FC": "CLT"}
KNOCKOUT_GAP_DAYS = 4         # Champions League: kick-offs further apart than this start a new round (leg)
OUT = os.path.join(os.path.dirname(__file__), "..", "predictions.json")
API = "https://api.football-data.org/v4/competitions/{}/matches?season={}"
# -------------------------------------------------------------------------------

STAGE_RANK = {"LEAGUE_STAGE": 0, "GROUP_STAGE": 0, "PLAYOFFS": 1, "LAST_16": 2,
              "QUARTER_FINALS": 3, "SEMI_FINALS": 4, "FINAL": 5}
STAGE_NAME = {"PLAYOFFS": "Knockout play-offs", "LAST_16": "Round of 16", "QUARTER_FINALS": "Quarter-finals",
              "SEMI_FINALS": "Semi-finals", "FINAL": "Final"}
TWO_LEGGED = {"PLAYOFFS", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS"}


def _get(url, key, tries=3):
    for n in range(tries):
        req = urllib.request.Request(url, headers={"X-Auth-Token": key})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and n < tries - 1:      # free plan: 10 requests a minute
                print("Rate limited, waiting 30s..."); time.sleep(30); continue
            raise


def _code(t):
    return NAME_FIX.get(t.get("name")) or TLA_FIX.get(t.get("tla"), t.get("tla"))


def fetch_season(comp, season, key):
    data = _get(API.format(comp, season), key)
    out = []
    for m in data.get("matches", []):
        h, a = m.get("homeTeam") or {}, m.get("awayTeam") or {}
        if not h.get("tla") or not a.get("tla") or not m.get("utcDate"):
            continue            # undecided knockout slots have no teams yet
        sc = m.get("score") or {}
        ft = sc.get("fullTime") or {}
        reg = sc.get("regularTime") or {}
        # Ties settled in extra time or penalties: use the 90-minute score, so the model sees what the
        # teams did in normal time and the page does not show a shoot-out as a scoreline.
        ext = (sc.get("duration") or "REGULAR") != "REGULAR" and reg.get("home") is not None
        g = reg if ext else ft
        out.append({
            "season": season, "stage": m.get("stage") or "", "matchday": m.get("matchday"),
            "status": m.get("status"), "utc": m.get("utcDate"), "ext": ext,
            "h": _code(h), "a": _code(a),
            "hn": h.get("shortName") or h.get("name"), "an": a.get("shortName") or a.get("name"),
            "hg": g.get("home"), "ag": g.get("away"),
        })
    return out


def _parse(utc):
    return dt.datetime.fromisoformat(utc.replace("Z", "+00:00"))


def assign_rounds(matches, kind):
    """Give every match a sortable round key `rk` and a display label `rlabel`.
    League: the matchday. Cup: the stage, split into legs by gaps in the calendar."""
    if kind == "league":
        for m in matches:
            m["rk"] = (0, m["matchday"] or 0); m["rlabel"] = f"Matchweek {m['matchday']}"
        return
    by = defaultdict(list)
    for m in matches: by[(m["season"], m["stage"])].append(m)
    for (season, stage), ms in by.items():
        ms.sort(key=lambda m: m["utc"])
        idx, prev = -1, None
        for m in ms:
            t = _parse(m["utc"])
            if prev is None or (t - prev).days >= KNOCKOUT_GAP_DAYS: idx += 1
            m["_i"], prev = idx, t
        legs = idx + 1
        for m in ms:
            rank = STAGE_RANK.get(stage, 9)
            m["rk"] = (rank, m["_i"])
            if rank == 0:
                m["rlabel"] = f"League phase \u00b7 Matchday {m['matchday'] or m['_i'] + 1}"
            else:
                name = STAGE_NAME.get(stage, stage.replace("_", " ").title())
                if stage in TWO_LEGGED and legs == 2:
                    name += " \u00b7 " + ("1st" if m["_i"] == 0 else "2nd") + " leg"
                m["rlabel"] = name


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


def build_competition(code, cfg, key):
    """Returns this competition's block for predictions.json, or None if there is nothing to show."""
    cur = season_start()
    matches = fetch_season(code, cur, key)
    try:
        time.sleep(1); matches += fetch_season(code, cur - 1, key)
    except Exception as e:  # free plans may not include history; carry on without it
        print(f"[{code}] Could not fetch last season, continuing without it:", e)
    for season in {m["season"] for m in matches}:
        assign_rounds([m for m in matches if m["season"] == season], cfg["kind"])
    now_season = [m for m in matches if m["season"] == cur]
    open_states = ("SCHEDULED", "TIMED", "IN_PLAY", "PAUSED")

    # --- predictions: the next round that still has an unfinished game ----------------
    up = [m for m in now_season if m["status"] in open_states]
    if not up:
        print(f"[{code}] No upcoming fixtures found; keeping what is already in predictions.json."); return None
    rk = min(m["rk"] for m in up)
    day = sorted((m for m in now_season if m["rk"] == rk), key=lambda m: m["utc"])
    # Predict every game from what was known BEFORE that round kicked off, so a game that
    # has already been played still shows the genuine pre-match prediction, not a hindsight one.
    rows = weighted_rows(matches, cur, before=day[0]["utc"])
    if len(rows) < 10:
        rows = weighted_rows(matches, cur)
    if len(rows) < 10:
        print(f"[{code}] Not enough finished matches to fit a model; keeping what is already in predictions.json."); return None
    model = fit(rows)
    fixtures = []
    for m in day:
        (ph, pd_, pa), xh, xa, best = predict(model, m["h"], m["a"])
        fixtures.append({"home": _team(m["h"], m["hn"]), "away": _team(m["a"], m["an"]),
                         "kickoff": m["utc"], "pct": round100([ph, pd_, pa]),
                         "xg": [round(xh, 2), round(xa, 2)],
                         "likeliest": {"h": best[1], "a": best[2], "p": round(best[0], 3)}})
    feat = 0
    for club in cfg["featured"]:
        hit = next((i for i, f in enumerate(fixtures) if club in (f["home"]["code"], f["away"]["code"])), None)
        if hit is not None: feat = hit; break

    # --- results: the most recent round in which any game has finished ----------------
    done = [m["rk"] for m in now_season if m["status"] == "FINISHED"]
    results = None
    if done:
        rrk = max(done)
        rnd = sorted((m for m in now_season if m["rk"] == rrk), key=lambda m: m["utc"])
        res = []
        for m in rnd:
            st = {"FINISHED": "FT", "IN_PLAY": "LIVE", "PAUSED": "LIVE", "POSTPONED": "POSTPONED"}.get(m["status"], "SCHEDULED")
            score = [m["hg"], m["ag"]] if st in ("FT", "LIVE") and m["hg"] is not None else None
            r = {"home": _team(m["h"], m["hn"]), "away": _team(m["a"], m["an"]),
                 "kickoff": m["utc"], "status": st, "score": score}
            if st == "FT" and m["ext"]: r["note"] = "90 min"
            res.append(r)
        results = {"label": rnd[0]["rlabel"], "fixtures": res}

    return {"name": cfg["name"], "kind": cfg["kind"], "season": f"{cur}/{str(cur + 1)[2:]}",
            "label": day[0]["rlabel"], "featured": feat,
            "model": {"type": "opponent-adjusted Poisson", "shrink": SHRINK, "season_decay": SEASON_DECAY, "rho": RHO,
                      "matches_used": len(rows)},
            "fixtures": fixtures, "results": results}


def load_previous():
    """Last run's competition blocks, so one competition failing never blanks the others.
    Also understands the old single-competition file format (Premier League only)."""
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    if "competitions" in old:
        return old["competitions"]
    if not old.get("fixtures"):
        return {}
    pl = dict(old, name="Premier League", kind="league", label=f"Matchweek {old.get('matchweek')}")
    if old.get("results"):
        pl["results"] = dict(old["results"], label=f"Matchweek {old['results'].get('matchweek')}")
    return {"PL": pl}


def update(key):
    prev = load_previous()
    blocks, problems = {}, []
    for n, (code, cfg) in enumerate(COMPETITIONS.items()):
        if n: time.sleep(2)
        try:
            block = build_competition(code, cfg, key)
        except urllib.error.HTTPError as e:
            problems.append(f"{code}: API error {e.code} {e.reason}"); block = None
        except urllib.error.URLError as e:
            problems.append(f"{code}: network error {e.reason}"); block = None
        if block is None and code in prev:
            block = prev[code]
        if block is not None:
            blocks[code] = block
    for p in problems: print("WARNING -", p)
    if not blocks:
        sys.exit("Nothing to write; predictions.json left unchanged.")
    if len(problems) == len(COMPETITIONS):
        sys.exit("Every competition failed; predictions.json left unchanged.")

    out = {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "order": list(blocks), "competitions": blocks}
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
    for code, b in blocks.items():
        print(f"{code}: {len(b['fixtures'])} fixtures ({b['label']}), {len(b['results']['fixtures']) if b['results'] else 0} results")


def backtest(key, code="PL"):
    cfg = COMPETITIONS[code]
    cur = season_start()
    matches = []
    for s in (cur - 2, cur - 1, cur):
        try:
            part = fetch_season(code, s, key); assign_rounds(part, cfg["kind"]); matches += part; time.sleep(1)
        except Exception as e:
            print("skip season", s, e)
    done = [m for m in matches if m["status"] == "FINISHED" and m["hg"] is not None]
    targets = [m for m in done if m["season"] >= cur - 1]
    by_md = defaultdict(list)
    for m in targets: by_md[(m["season"], m["rk"])].append(m)
    min_rows = 100 if cfg["kind"] == "league" else 60
    briers, base_briers, logl, pairs = [], [], [], []
    for (s, _), ms in sorted(by_md.items()):
        cutoff = min(m["utc"] for m in ms)
        rows = weighted_rows(done, s, before=cutoff)
        if len(rows) < min_rows: continue
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
    print(f"{cfg['name']}: scored {n} matches (walk-forward: each round predicted using only earlier results)")
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
        if "--backtest" in sys.argv:
            rest = [a for a in sys.argv[sys.argv.index("--backtest") + 1:] if not a.startswith("--")]
            code = rest[0].upper() if rest else "PL"
            if code not in COMPETITIONS: sys.exit(f"Unknown competition {code}. Choose from: {', '.join(COMPETITIONS)}")
            backtest(key, code)
        else:
            update(key)
    except urllib.error.HTTPError as e:
        sys.exit(f"API error {e.code}: {e.reason}. predictions.json left unchanged.")
    except urllib.error.URLError as e:
        sys.exit(f"Network error: {e.reason}. predictions.json left unchanged.")
