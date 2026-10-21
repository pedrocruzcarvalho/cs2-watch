"""
One 10-minute tick: scan, update the prediction ledger, replay the paper bankroll, publish the dashboard data.

State lives in Cloudflare KV (the repo stays free of data). The free plan allows 1,000 writes a day and a tick runs
144 times a day, so all state goes into ONE gzipped value, "state:bundle": 2 writes per tick (bundle + dashboard).
The keys below are the bundle's parts (older deployments stored them one per KV key; those are read once if the
bundle is missing):
  state:ledger   predictions, frozen at kickoff, resolved after the match
  state:wallets  wallet lookups cache
  state:baseline normal position sizes per tier (from cs2_watch.py --calibrate)
  state:meta     when the paper account was opened
  dashboard      everything the page shows (read by functions/api/data.js)

Run in GitHub Actions with CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_ID set.
Locally: python cloud_run.py --local some/dir   (same thing, files instead of KV)
"""

import argparse
import gzip
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bankroll as B
import cs2_watch as C
import oddspapi as O
import teams as TM
import informed as I
import live_money as LM
import oddshift as OS
import wallet_history as WH
import wallets as W

KEYS = {"state:ledger": "ledger.json", "state:wallets": "wallets.json", "state:baseline": "baseline.json",
        "state:meta": "meta.json", "state:oddspapi": "oddspapi.json", "state:watch_auto": "watch_auto.json",
        "watchlist": "watchlist.json", "state:profiles": "profiles.json", "state:vrs": "vrs.json",
        "state:informed": "informed.json", "state:livemoney": "livemoney.json", "state:oddshift": "oddshift.json"}
BUNDLE = "state:bundle"
KEEP_DAYS = 90
RESET = "2026-10-21"          # change to wipe every paper account back to $300 once (Pedro, Oct 20 2026)


class KV:
    def __init__(self):
        acc, ns = os.environ["CF_ACCOUNT_ID"], os.environ["CF_KV_ID"]
        self.base = f"https://api.cloudflare.com/client/v4/accounts/{acc}/storage/kv/namespaces/{ns}/values/"
        self.auth = {"Authorization": f"Bearer {os.environ['CF_API_TOKEN']}"}

    def get(self, key):
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + key, headers=self.auth), timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def put(self, key, data):
        req = urllib.request.Request(self.base + key, data=data, method="PUT",
                                     headers=self.auth | {"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=60) as r:
            if not json.loads(r.read()).get("success"):
                raise RuntimeError(f"KV put {key} failed")


class Files:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, key):
        p = self.root / key.replace(":", "_")
        return p.read_bytes() if p.exists() else None

    def put(self, key, data):
        (self.root / key.replace(":", "_")).write_bytes(data)


def fetch_winner(slug, cid):
    e = C.get(f"{C.GAMMA}/events?slug={slug}")[0]
    mk = next((m for m in e["markets"] if m["conditionId"] == cid), None)
    return C.winner_of(mk, e.get("ended")) if mk else None


def watch_weights(auto_watched, manual, profiles):
    """How much a watched wallet's bets count, from its track record. Hand-picked wallets always count fully."""
    out = {}
    for w, v in auto_watched.items():
        prof = profiles.get(w.lower())
        vd = prof["verdict"] if prof else "unclear"
        out[w.lower()] = dict(v, verdict=vd, bonus=W.BONUS[vd], floors=vd == "suspect")
    for w, v in manual.items():
        prof = profiles.get(w.lower())
        out[w.lower()] = dict(v, reason=v.get("note") or "added by hand", source="manual", bonus=5, floors=True,
                              verdict=prof["verdict"] if prof else None)
    return out


def watch_view(auto_watched, manual, rows, profiles=None):
    """Watchlist for the dashboard: every watched wallet, why, its record, and what it is betting on now."""
    now_bets = {}
    for r in rows:
        if r.get("watched") and r.get("wallet"):
            now_bets.setdefault(r["wallet"].lower(), []).append(dict(match=r["match"], market=r["market"], outcome=r["outcome"],
                                                                     usd=r["usd"], start=r["start"], level=r["level"]))
    out = []
    for w, v in auto_watched.items():
        out.append(dict(wallet=w, source="auto", reason=v["reason"], name=v.get("name"), since=v["since"],
                        won=v["won"], lost=v["lost"], flags=v["flags"], betting=now_bets.get(w.lower(), []),
                        profile=(profiles or {}).get(w.lower())))
    for w, v in manual.items():
        out.append(dict(wallet=w, source="manual", reason=v.get("note") or "added by hand", name=v.get("name"),
                        since=v.get("added_at"), won=None, lost=None, flags=None, betting=now_bets.get(w.lower(), []),
                        profile=(profiles or {}).get(w.lower())))
    return sorted(out, key=lambda x: (not x["betting"], -(x["since"] or 0)))


LEVEL_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


VERDICT_SHORT = {"suspect": "record: suspect", "sharp": "record: sharp bettor", "lucky": "record: normal luck",
                 "unclear": "record: unclear"}


def watch_tag(w, watch):
    """'Your list' for hand-picked wallets; auto-watched ones only when their record looks suspect or sharp
    (an auto-watched whale with normal luck is just a whale)."""
    v = watch.get(w)
    if not v:
        return None
    if v.get("source") == "manual":
        return dict(kind="watched", label="your list: " + (v.get("reason") or "")[:70])
    if v.get("verdict") in ("suspect", "sharp"):
        return dict(kind="repeat", label=f"repeat big bettor ({v['verdict']})")
    return None


def who(w, profiles, qual, life=None):
    """One plain line about the wallet: its record verdict and size of history."""
    if w in qual:
        r = qual[w]
        return f"informed: {r['wins']}-{r['losses']} in 21 days at {r['avg_price']:.0%} prices"
    prof = profiles.get(w)
    if not prof:
        return "no track record checked"
    a = prof.get("all") or {}
    base = VERDICT_SHORT.get(prof["verdict"], "")
    return base + (f" ({a['wins']}-{a['losses']}, won {a['win_rate']:.0%} at {a['avg_price']:.0%} prices)" if a.get("n") else "")


def radar(rows, infos, qual, rising, watch, now, profiles=None, limit=40):
    """Everything odd before kickoff on upcoming matches, for information (not bet): big out-of-place positions
    (the old flag levels), hand-picked wallets, informed and rising wallets, and lines moving toward a big bettor.
    Ordered so that real leads come first: hand-picked and informed wallets, suspect records, then plain size."""
    profiles = profiles or {}
    rise = {x["wallet"]: x for x in rising}
    groups = {}
    for r in rows:
        if r["start"] <= now or not (r.get("wallet") or (r.get("venue") == "kalshi" and r.get("level"))):
            continue
        w = (r.get("wallet") or "").lower()
        tags = [dict(kind="kalshi", label="Kalshi")] if r.get("venue") == "kalshi" else []
        if r.get("level"):
            tags.append(dict(kind="big", label=f"big bet ({r['level']})"))
        wt = watch_tag(w, watch)
        if wt:
            tags.append(wt)
        if w in qual:
            tags.append(dict(kind="informed", label=f"informed wallet (luck {qual[w]['z']:.1f})"))
        elif w in rise:
            tags.append(dict(kind="rising", label=f"rising wallet (luck {rise[w]['record']['z']:.1f})"))
        if (r.get("move") or 0) >= 0.10 and r["usd"] >= 1000:
            tags.append(dict(kind="move", label=f"line moved {r['move'] * 100:+.0f}c its way"))
        if not tags:
            continue
        g = groups.setdefault((r["match"], r["market"]), dict(match=r["match"], market=r["market"], start=r["start"],
                                                            url=r.get("url"), vtier=C.vtier(r["match"]), items=[], score=0))
        g["items"].append(dict(wallet=r.get("wallet") or "", name=r.get("name"), outcome=r["outcome"], usd=round(r["usd"]),
                               avg=round(r.get("avg_price") or 0, 3), tags=tags, why=(r.get("why") or [])[:6],
                               who=who(w, profiles, qual) if w else "anonymous (Kalshi)"))
        g["score"] += sum({"big": 1 + LEVEL_RANK.get(r.get("level"), 0), "watched": 8, "informed": 6, "repeat": 3,
                           "rising": 2, "move": 1, "kalshi": 0}[t["kind"]] for t in tags)
        if (profiles.get(w) or {}).get("verdict") == "lucky" and not any(t["kind"] in ("watched", "informed") for t in tags):
            g["score"] -= 1                                            # a whale with normal luck: the least interesting
    seen = {((x["wallet"] or "").lower(), g["match"], g["market"], x["outcome"]) for g in groups.values() for x in g["items"]}
    for i in infos:                                     # small bets ($20+) of followed wallets, which rows leave out
        if i.get("venue") != "poly" or i["start"] <= now:
            continue
        for w, _, o, usd, avg, name in i.get("allpos") or []:
            wt = watch_tag(w, watch)
            if (w, i["match"], i["market"], o) in seen or not (w in qual or w in rise or wt):
                continue
            tags = [wt] if wt else []
            if w in qual:
                tags.append(dict(kind="informed", label=f"informed wallet (luck {qual[w]['z']:.1f})"))
            elif w in rise:
                tags.append(dict(kind="rising", label=f"rising wallet (luck {rise[w]['record']['z']:.1f})"))
            g = groups.setdefault((i["match"], i["market"]), dict(match=i["match"], market=i["market"], start=i["start"],
                                                                url=i.get("url"), vtier=C.vtier(i["match"]), items=[], score=0))
            g["items"].append(dict(wallet=w, name=name, outcome=o, usd=round(usd), avg=round(avg, 3), tags=tags, why=[],
                                   who=who(w, profiles, qual)))
            g["score"] += sum({"watched": 8, "informed": 6, "repeat": 3, "rising": 2}[t["kind"]] for t in tags)
    out = sorted(groups.values(), key=lambda g: (-g["score"], g["start"]))[:limit]
    for g in out:
        g["items"].sort(key=lambda x: -x["usd"])
        g["items"] = g["items"][:8]
    return out


def tick(store, log=print):
    t0 = time.time()
    work = Path(tempfile.mkdtemp(prefix="cs2_"))
    C.DIR = work
    bundle = store.get(BUNDLE)
    parts = json.loads(gzip.decompress(bundle)) if bundle else {}
    for key, name in KEYS.items():
        if key == "watchlist":                                     # written by the dashboard, never by the tick
            blob = store.get(key)
        elif bundle:
            blob = parts[key].encode() if key in parts else None
        else:
            blob = store.get(key)
        if blob:
            (work / name).write_bytes(blob)
    meta = json.loads((work / "meta.json").read_text()) if (work / "meta.json").exists() else {}
    now = datetime.now(timezone.utc)
    meta.setdefault("opened_at", now.timestamp())
    baseline = json.loads((work / "baseline.json").read_text()) if (work / "baseline.json").exists() else None

    auto = O.load(work / "watch_auto.json")
    manual = O.load(work / "watchlist.json")                       # edited from the dashboard (functions/api/watchlist.js)
    profiles = O.load(work / "profiles.json")                      # wallet track records (wallets.py)
    C.TEAMS = TM.refresh(O.load(work / "vrs.json"), now.timestamp(), log=log)   # Valve ranking, refreshed daily
    (work / "vrs.json").write_text(json.dumps(C.TEAMS))
    auto_watched = C.update_watch(auto, [], now.timestamp())
    C.WATCH = watch_weights(auto_watched, manual, profiles)
    flags, rows, infos = C.scan(now - timedelta(hours=6), now + timedelta(hours=36), ["poly", "kalshi"], baseline, log=log)
    auto_watched = C.update_watch(auto, rows, now.timestamp())
    (work / "watch_auto.json").write_text(json.dumps(auto))
    # track records: watched wallets first, then every MEDIUM/HIGH-flagged wallet in this scan
    who = list(dict.fromkeys([w.lower() for w in list(auto_watched) + list(manual)] +
                             [r["wallet"].lower() for r in rows if r.get("wallet") and r.get("level") in ("MEDIUM", "HIGH")]))
    traded = {w.lower(): (i or {}).get("traded") for w, i in C._wallets.items()}
    W.refresh(profiles, who, {w.lower(): a for w, a in auto.items()}, traded, now.timestamp(), log=log)
    (work / "profiles.json").write_text(json.dumps(profiles))
    for r in rows:
        if r.get("wallet"):
            r["record"] = W.summary(profiles.get(r["wallet"].lower()))
    matches = C.build_matches(infos, rows)
    led = C.load_ledger()
    C.apply_line_moves(led, matches, now.timestamp())
    # bookmaker odds (oddspapi.py) are switched off at Pedro's request (Oct 20 2026); Kalshi back on (Oct 21)
    C.record(led, matches, "live", now.timestamp())
    C.backfill_snapshots(led, matches, now.timestamp())
    C.resolve(led, now.timestamp(), log=log)
    old = now.timestamp() - KEEP_DAYS * 86400
    led = {k: r for k, r in led.items() if r["mode"] == "live" and r["start"] >= old}
    C.save_ledger(led)

    # ---- informed wallets: the paper account the dashboard shows (informed.py) ----
    reset = meta.get("reset") != RESET                             # one-time restart of all paper accounts
    if reset:
        log(f"resetting every paper account to ${I.START_BALANCE:.0f} ({RESET})")
        meta["reset"] = RESET
    ist = O.load(work / "informed.json")
    if reset:
        ist.pop("account", None)
    ts = now.timestamp()
    added = I.add_resolved(ist, infos, ts)
    qual = I.qualifying(ist, ts)
    sigs = I.signals(infos, qual, C.vtier, ts)
    I.settle(ist, infos, ts, fetch_winner, log=log)
    I.place_bets(ist, sigs, ts, C.book_asks, B.fill, log=log)
    I.refresh_lifetime(ist, list(qual), ts, lambda w: WH.lifetime(w, max_closed=1500), log=log)
    (work / "informed.json").write_text(json.dumps(ist))
    inf = I.dashboard(ist, sigs, qual, ts)
    log(f"informed: +{added} resolved positions, {len(qual)} informed wallets, "
        f"{sum(s['active'] for s in sigs)} active signals")

    # ---- live big money: the second paper account (live_money.py) ----
    lst = {} if reset else O.load(work / "livemoney.json")
    lsigs = LM.signals(infos, C.vtier, ts)
    I.settle(lst, infos, ts, fetch_winner, log=log)
    I.place_bets(lst, lsigs, ts, C.book_asks, B.fill, log=log, live=True, tag="live")
    (work / "livemoney.json").write_text(json.dumps(lst))
    live = LM.dashboard(lst, lsigs, ts)
    log(f"live money: {len(lsigs)} markets with in-play money, {sum(s['active'] for s in lsigs)} active signals, "
        f"balance ${live['stats']['balance']:.2f}")

    # ---- odd shifts: price vs scoreboard (oddshift.py) ----
    ost = O.load(work / "oddshift.json")
    if reset:
        ost.pop("account", None)
    osigs = OS.signals(ost, infos, C.vtier, ts)
    I.settle(ost, infos, ts, fetch_winner, log=log)
    I.place_bets(ost, osigs, ts, C.book_asks, B.fill, log=log, live=True, tag="odd shift")
    (work / "oddshift.json").write_text(json.dumps(ost))
    shift = OS.dashboard(ost, osigs, ts)
    log(f"odd shifts: {len(osigs)} live matches with a score, {sum(s['active'] for s in osigs)} active signals, "
        f"{shift['snaps']} snapshots kept, balance ${shift['stats']['balance']:.2f}")

    meta["last_run"] = now.timestamp()
    meta["runs"] = meta.get("runs", 0) + 1
    dash = dict(at=now.timestamp(), opened_at=inf["opened_at"], took=round(time.time() - t0, 1), runs=meta["runs"],
                stats=inf["stats"], series=inf["series"], bets=inf["bets"], signals=inf["signals"],
                wallets=inf["wallets"], rising=inf["rising"], rules=inf["rules"], scanned=len({m["match_id"] for m in matches}),
                flagged=sum(1 for s in sigs if s["active"]), watch=watch_view(auto_watched, manual, rows, profiles),
                live=live, shift=shift,
                radar=radar(rows, infos, qual, inf["rising"], C.WATCH, ts, profiles))
    stats = inf["stats"]
    (work / "meta.json").write_text(json.dumps(meta))

    parts = {key: (work / name).read_text() for key, name in KEYS.items()
             if key != "watchlist" and (work / name).exists()}
    store.put(BUNDLE, gzip.compress(json.dumps(parts).encode()))
    store.put("dashboard", json.dumps(dash, default=str).encode())
    log(f"tick done in {time.time() - t0:.0f}s: {len(matches)} markets, {dash['flagged']} informed signals, "
        f"balance ${stats['balance']:.2f} "
        f"({stats['wins']}W/{stats['losses']}L, {stats['open']} open)")
    return dash


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", help="use this folder instead of Cloudflare KV")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    tick(Files(a.local) if a.local else KV())


if __name__ == "__main__":
    main()
