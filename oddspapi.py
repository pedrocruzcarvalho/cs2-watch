"""
Bookmaker odds (Pinnacle, 1xBet, Stake) for CS2 from OddsPapi, as a second opinion on flagged matches.

The free key allows 250 billable requests a month, so:
  - the fixture list (/v4/fixtures, billable) is fetched at most every FIXTURE_EVERY, never past MONTH_CAP;
  - odds come only from /v4/historical-odds, which OddsPapi does not count ("always free"), with its 5 s cooldown,
    for at most HIST_PER_TICK fixtures per scan, flagged matches first.

Signal: the flagged side's implied chance at a bookmaker (margin removed) over the last LOOKBACK.
  moved >= SHARP_MOVE towards it -> bookmakers agree -> the market's level goes up one step;
  moved >= SHARP_MOVE against it -> bookmakers disagree -> down one step (not below LOW).
State lives in oddspapi.json next to the ledger (KV key state:oddspapi in the cloud).
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import cs2_watch as C

BASE = "https://api.oddspapi.io/v4"
SPORT, WINNER_MARKET = 17, "171"                     # ESport Counter-Strike; "Winner" (outcome 171 = p1, 172 = p2)
BOOKS = ["pinnacle", "1xbet", "stake"]
BOOK_NAME = {"pinnacle": "Pinnacle", "1xbet": "1xBet", "stake": "Stake"}
FIXTURE_EVERY, MONTH_CAP = 12 * 3600, 200
HIST_PER_TICK, HIST_EVERY, COOLDOWN = 8, 30 * 60, 5.2
SHARP_MOVE, LOOKBACK = 0.06, 12 * 3600


def key():
    k = os.environ.get("ODDSPAPI_KEY")
    if not k:
        env = C.Path(__file__).parent / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("ODDSPAPI_KEY="):
                    k = line.split("=", 1)[1].strip()
    return k


def call(path, **q):
    q["apiKey"] = key()
    req = urllib.request.Request(f"{BASE}/{path}?{urllib.parse.urlencode(q)}", headers={"User-Agent": "cs2-watch"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def load(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def implied(p1, p2):
    """Decimal odds of both sides -> chance of side 1 with the bookmaker margin removed."""
    a, b = 1 / p1, 1 / p2
    return a / (a + b)


def series(hist_entry):
    """{book: [[t, chance_of_p1], ...]} from a /historical-odds response."""
    out = {}
    for bk, d in (hist_entry.get("bookmakers") or {}).items():
        mk = (d.get("markets") or {}).get(WINNER_MARKET)
        if not mk:
            continue
        side = {}
        for oid, o in mk.get("outcomes", {}).items():
            entries = next(iter((o.get("players") or {}).values()), [])
            side[oid] = sorted((C.iso(e["createdAt"]).timestamp(), e["price"]) for e in entries if e.get("price"))
        p1, p2 = side.get("171", []), side.get("172", [])
        if not p1 or not p2:
            continue
        pts, j = [], 0
        for t, pr in p1:                                   # pair each p1 price with the latest p2 price at that time
            while j + 1 < len(p2) and p2[j + 1][0] <= t:
                j += 1
            if p2[j][0] <= t + 60:
                pts.append([round(t), round(implied(pr, p2[j][1]), 4)])
        for t, pr in p2:
            last1 = max((x for x in p1 if x[0] <= t + 60), default=None)
            if last1:
                pts.append([round(t), round(implied(last1[1], pr), 4)])
        pts.sort()
        if pts:
            out[bk] = pts
    return out


def link(fixtures, matches):
    """fixtureId -> (match, team that is participant 1)."""
    links = {}
    for f in fixtures:
        k = C.match_key(f"{f['p1']} vs {f['p2']}")
        for m in matches:
            if m.get("kind", "match") != "match" or abs(m["start"] - f["start"]) > 6 * 3600:
                continue
            if C.same_match(k, C.match_key(m["match"])):
                p1 = C.team_for(f["p1"], m["teams"]) or next((t for t in m["teams"] if C.same_match(
                    C.match_key(f"{f['p1']} vs x"), C.match_key(f"{t} vs x"))), None)
                if p1:
                    links[f["id"]] = (m, p1)
                break
    return links


def judge(m, p1_team, books, now):
    """Attach bookmaker context to a match and move its level if the bookmakers clearly agree or disagree."""
    side = m.get("fix_side") or m.get("tracked")
    if not side or not books:
        return
    flip = side != p1_team
    view = {}
    for bk, pts in books.items():
        pts = [(t, 1 - p if flip else p) for t, p in pts if t <= now]
        if not pts:
            continue
        before = [p for t, p in pts if t <= now - LOOKBACK]
        start_t, start = (now - LOOKBACK, before[-1]) if before else pts[0]
        view[bk] = dict(book=BOOK_NAME.get(bk, bk), now=round(pts[-1][1], 3), start=round(start, 3),
                        since=start_t, move=round(pts[-1][1] - start, 3), points=len(pts))
    if not view:
        return
    m["books"] = view
    if not m.get("fix_side") or not m.get("level") or now >= m["start"]:
        return
    best = max(view.values(), key=lambda v: (v["book"] == "Pinnacle", abs(v["move"])))
    if best["move"] >= SHARP_MOVE:
        m["sharp"] = dict(best, agree=True, raised_from=m["level"])
        m["level"] = C.LEVELS[max(C.LEVELS.index(m["level"]) - 1, 0)]
        m["pts"] += 2
    elif best["move"] <= -SHARP_MOVE:
        m["sharp"] = dict(best, agree=False, lowered_from=m["level"])
        m["level"] = C.LEVELS[min(C.LEVELS.index(m["level"]) + 1, 2)]  # never below LOW
        m["pts"] -= 2


def tick(state, matches, now, log=print):
    """One scan's worth of OddsPapi work. `state` is the dict stored between scans; it is updated in place."""
    if not key():
        return state
    month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
    if state.get("month") != month:
        state.update(month=month, used=0)
    if now - state.get("fixtures_at", 0) >= FIXTURE_EVERY and state.get("used", 0) < MONTH_CAP:
        lo = datetime.fromtimestamp(now - 6 * 3600, timezone.utc)
        try:
            fx = call("fixtures", sportId=SPORT, **{"from": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                    "to": (lo + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")})
            state["fixtures"] = [dict(id=f["fixtureId"], p1=f.get("participant1Name") or "", p2=f.get("participant2Name") or "",
                                      start=C.iso(f["startTime"]).timestamp(), tournament=f.get("tournamentName"))
                                 for f in fx if f.get("hasOdds")]
            state["fixtures_at"] = now
        except Exception as err:
            log(f"  oddspapi fixtures failed: {err}")
        state["used"] = state.get("used", 0) + 1                # billed whether it worked or not
        log(f"oddspapi: fixture list refreshed ({state['used']}/{MONTH_CAP} requests this month)")
    links = link(state.get("fixtures", []), matches)
    hist = state.setdefault("hist", {})
    due = sorted((fid for fid, (m, _) in links.items() if now - hist.get(fid, {}).get("checked", 0) >= HIST_EVERY),
                 key=lambda fid: (not links[fid][0].get("level"), links[fid][0]["start"]))[:HIST_PER_TICK]
    for i, fid in enumerate(due):
        if i:
            time.sleep(COOLDOWN)
        try:
            hist[fid] = dict(checked=now, books=series(call("historical-odds", fixtureId=fid, bookmakers=",".join(BOOKS))))
        except Exception as err:
            log(f"  oddspapi history failed for {fid}: {err}")
            hist.setdefault(fid, {})["checked"] = now
    for fid, (m, p1) in links.items():
        judge(m, p1, hist.get(fid, {}).get("books", {}), now)
    keep = {f["id"] for f in state.get("fixtures", [])}
    state["hist"] = {k: v for k, v in hist.items() if k in keep}
    if due:
        log(f"oddspapi: {len(links)} matches linked, odds history refreshed for {len(due)} (free endpoint)")
    return state
