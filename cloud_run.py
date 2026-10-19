"""
One 10-minute tick: scan, update the prediction ledger, replay the paper bankroll, publish the dashboard data.

State lives in Cloudflare KV (the repo stays free of data):
  state:ledger   predictions, frozen at kickoff, resolved after the match
  state:wallets  wallet lookups cache
  state:baseline normal position sizes per tier (from cs2_watch.py --calibrate)
  state:meta     when the paper account was opened
  dashboard      everything the page shows (read by functions/api/data.js)

Run in GitHub Actions with CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_ID set.
Locally: python cloud_run.py --local some/dir   (same thing, files instead of KV)
"""

import argparse
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
import wallets as W

KEYS = {"state:ledger": "ledger.json", "state:wallets": "wallets.json", "state:baseline": "baseline.json",
        "state:meta": "meta.json", "state:oddspapi": "oddspapi.json", "state:watch_auto": "watch_auto.json",
        "watchlist": "watchlist.json", "state:profiles": "profiles.json", "state:vrs": "vrs.json"}
KEEP_DAYS = 90


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


def tick(store, log=print):
    t0 = time.time()
    work = Path(tempfile.mkdtemp(prefix="cs2_"))
    C.DIR = work
    for key, name in KEYS.items():
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
    flags, rows, infos = C.scan(now - timedelta(hours=6), now + timedelta(hours=36), ["poly"], baseline, log=log)
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
    odds = O.load(work / "oddspapi.json")
    O.tick(odds, matches, now.timestamp(), log=log)                # bookmaker second opinion (budgeted)
    (work / "oddspapi.json").write_text(json.dumps(odds))
    C.record(led, matches, "live", now.timestamp())
    C.backfill_snapshots(led, matches, now.timestamp())
    C.resolve(led, now.timestamp(), log=log)
    old = now.timestamp() - KEEP_DAYS * 86400
    led = {k: r for k, r in led.items() if r["mode"] == "live" and r["start"] >= old}
    C.save_ledger(led)

    bets, stats, series = B.run(led, meta["opened_at"], now.timestamp())
    meta["last_run"] = now.timestamp()
    meta["runs"] = meta.get("runs", 0) + 1
    dash = dict(at=now.timestamp(), opened_at=meta["opened_at"], took=round(time.time() - t0, 1), runs=meta["runs"],
                stakes=B.STAKES, stats=stats, series=series, bets=bets[:500],
                planned=B.planned(matches, led, meta["opened_at"], now.timestamp()), scanned=len({m["match_id"] for m in matches}),
                flagged=sum(1 for m in matches if m["level"]),
                validation=C.validation(led),
                watch=watch_view(auto_watched, manual, rows, profiles))
    (work / "meta.json").write_text(json.dumps(meta))

    for key in ("state:ledger", "state:wallets", "state:meta", "state:oddspapi", "state:watch_auto", "state:profiles",
                "state:vrs"):
        store.put(key, (work / KEYS[key]).read_bytes())
    store.put("dashboard", json.dumps(dash, default=str).encode())
    log(f"tick done in {time.time() - t0:.0f}s: {len(matches)} matches, {dash['flagged']} flagged, "
        f"{len(dash['planned'])} planned bets, balance ${stats['balance']:.2f} "
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
