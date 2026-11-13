"""
One 10-minute tick of the hunter (hunter.py): scan CS2 + Dota 2, settle, bet, publish the page, push new bets.

State lives in Cloudflare KV as one gzipped value ("state:bundle", part "state:hunter"), the page data in "dashboard".
If a tick fails, the phone gets one push (then at most one every 3 hours) and another when the scan works again, so
a dead scan can't go unnoticed (Nov 12 2026: a Polymarket API change stopped every scan for 10 hours, silently).
The old multi-account tick (live money, odd shifts, radar, tennis) is in legacy_run.py, no longer run.

Run in GitHub Actions with CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_ID (and NTFY_TOPIC) set.
Locally: python cloud_run.py --local some/dir   (same thing, files instead of KV)
"""

import argparse
import gzip
import json
import sys
import time
import traceback
from datetime import datetime, timezone

import cs2_watch as C
import hunter as H
import mirror as M
import notify as N
import teams as TM
from legacy_run import KV, Files

BUNDLE = "state:bundle"
HEALTH = "health"
FRESH = "2026-11-13"            # Pedro: clear all history, start the hunter at $63.47 (his real balance)
FAIL_REPEAT = 3 * 3600


def tick(store, log=print):
    t0 = time.time()
    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    blob = store.get(BUNDLE)
    parts = json.loads(gzip.decompress(blob)) if blob else {}
    if parts.get("fresh") != FRESH:                       # once: drop every old account and its history
        log(f"fresh start {FRESH}: old state dropped, hunter opens at ${H.START}")
        parts = dict(fresh=FRESH)
    st = json.loads(parts.get("state:hunter") or "{}")
    cache = st.setdefault("created", {})
    for w in [w for w, c in cache.items() if ts - c.get("checked", 0) > 30 * 86400]:
        del cache[w]

    C.TEAMS = TM.refresh(json.loads(parts.get("state:vrs") or "{}"), ts, log=log)   # Valve ranking: CS2 tiers
    parts["state:vrs"] = json.dumps(C.TEAMS)
    sigs, infos = H.signals(now, cache, log=log)
    H.settle(st, infos, ts, log=log)
    H.place(st, sigs, ts, log=log)
    view = H.view(st, sigs, ts)
    parts["state:hunter"] = json.dumps(st)
    mst = json.loads(parts.get("state:mirror") or "{}")      # paper copy of one wallet, no alerts (mirror.py)
    mview = None
    try:
        M.tick(mst, ts, log=log)
        parts["state:mirror"] = json.dumps(mst)
        mview = M.view(mst, ts)
    except Exception as e:                                    # the mirror must never stop the hunter
        log(f"mirror failed: {e!r}")
    store.put(BUNDLE, gzip.compress(json.dumps(parts).encode()))
    dash = dict(at=ts, took=round(time.time() - t0, 1), scanned=len(infos), hunter=view, mirror=mview)
    store.put("dashboard", json.dumps(dash, default=str).encode())
    N.send([("Hunter", st)], ts, log=log, balance=view["stats"]["balance"])   # after the save: never alert twice
    s = view["stats"]
    log(f"tick done in {time.time() - t0:.0f}s: {len(infos)} markets, {len(sigs)} wallet bets of ${H.WALLET_MIN}+, "
        f"{sum(not u['no'] for u in sigs)} pass the rules; balance ${s['balance']:.2f} ({s['wins']}W/{s['losses']}L, "
        f"{s['open']} open)")


def health(store, ok, err=None, log=print):
    """Push once when the scan starts failing (again every 3 h while it stays down) and once when it recovers."""
    try:
        h = json.loads(store.get(HEALTH) or b"{}")
    except Exception:
        h = {}
    now = time.time()
    if ok:
        if h.get("down_since"):
            mins = (now - h["down_since"]) / 60
            N.push("Scan voltou a funcionar", f"Esteve parado {mins:.0f} min.", log=log)
            store.put(HEALTH, b"{}")
        return
    if now - h.get("alerted", 0) >= FAIL_REPEAT:
        N.push("Scan FALHOU", f"O scan do caçador está a falhar: {err}"[:500], log=log)
        h["alerted"] = now
    h.setdefault("down_since", now)
    store.put(HEALTH, json.dumps(h).encode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", help="use this folder instead of Cloudflare KV")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    store = Files(a.local) if a.local else KV()
    try:
        tick(store)
    except Exception as e:
        traceback.print_exc()
        try:
            health(store, False, f"{type(e).__name__}: {str(e)[:200]}")
        except Exception:
            traceback.print_exc()
        sys.exit(1)
    health(store, True)


if __name__ == "__main__":
    main()
