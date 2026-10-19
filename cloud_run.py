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

KEYS = {"state:ledger": "ledger.json", "state:wallets": "wallets.json", "state:baseline": "baseline.json",
        "state:meta": "meta.json"}
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

    flags, rows, infos = C.scan(now - timedelta(hours=6), now + timedelta(hours=36), ["poly"], baseline, log=log)
    matches = C.build_matches(infos, rows)
    led = C.load_ledger()
    C.apply_line_moves(led, matches, now.timestamp())
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
                validation=C.validation(led))
    (work / "meta.json").write_text(json.dumps(meta))

    for key in ("state:ledger", "state:wallets", "state:meta"):
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
