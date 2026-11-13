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
import book as BK
import hunter as H
import mirror as M
import notify as N
import teams as TM
from legacy_run import KV, Files

BUNDLE = "state:bundle"
HEALTH = "health"
FRESH = "2026-11-13"            # Pedro: clear all history, start the hunter at $63.47 (his real balance)
FAIL_REPEAT = 3 * 3600
NL = chr(10)
BOOK_V = 2                     # v2: tier 1/2 matches kept too (a fixer bets 70%+ on tier 3+)


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
    bst = json.loads(parts.get("state:book") or "{}")       # every wallet's tier 3+ record (book.py)
    if bst.get("v") != BOOK_V:                               # (re)seed once per book format
        seed = store.get("seed:book")
        if seed:
            bst = json.loads(seed)
            bst["v"] = BOOK_V
            log(f"book: seeded {len(bst['book'])} wallets from history (v{BOOK_V})")
    bst.setdefault("book", {}); bst.setdefault("names", {})
    listed = {w.lower(): v for w, v in json.loads(store.get("watchlist") or b"{}").items()}   # your hand-picked wallets
    details = {}
    sigs, infos = H.signals(now, cache, log=log, book=bst["book"], listed=listed, names=bst["names"], details=details)
    wins = {i["cid"]: i["winner"] for i in infos if i.get("winner")}
    need = BK.pending(bst["book"], ts) - set(wins)
    for cid, m in H.market_states(list(need)[:400]).items():
        w = C.winner_of(m, False)
        if w:
            wins[cid] = w
    BK.resolve(bst["book"], wins, ts)
    BK.prune(bst["book"])
    board = BK.board(bst["book"], ts, bst["names"], listed)
    bst["names"] = {w: n for w, n in bst["names"].items() if w in bst["book"]}
    parts["state:book"] = json.dumps(bst, separators=(",", ":"))
    H.settle(st, infos, ts, log=log)
    if not st.get("seeded"):                                 # once: 14 days of history for the list (hunt_seed.py)
        seed = store.get("seed:signals")
        if seed:
            led = st.setdefault("signals", {})
            for k, x in json.loads(seed).items():
                led.setdefault(k, x)
            st["seeded"] = True
            log(f"signals: seeded {len(led)} from history")
    H.track(st, sigs, infos, ts, log=log)
    H.follow(st, sigs, ts, log=log)                          # the fixer sold: we sell too (and get a push)
    H.place(st, sigs, ts, log=log)
    view = H.view(st, sigs, ts, infos, details)
    fix_push = new_fixes(st, view, ts)                       # marked now, saved with the state, pushed after the save
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
    dash = dict(at=ts, took=round(time.time() - t0, 1), scanned=len(infos), hunter=view, mirror=mview, wallets=board)
    store.put("dashboard", json.dumps(dash, default=str).encode())
    N.send([("Caçador", st)], ts, log=log, balance=view["stats"]["balance"])   # after the save: never alert twice
    if fix_push:
        N.push(fix_push[0], fix_push[1], log=log, click=fix_push[2])
    s = view["stats"]
    log(f"tick done in {time.time() - t0:.0f}s: {len(infos)} markets, {len(sigs)} wallet bets of ${H.WALLET_MIN}+, "
        f"{sum(not u['no'] and not u['top'] for u in sigs)} APOSTAR, {len(st.get('signals', {}))} signals kept; balance ${s['balance']:.2f} ({s['wins']}W/{s['losses']}L, "
        f"{s['open']} open)")


def new_fixes(st, view, ts):
    """(title, text, click) for the matches flagged APOSTA for the first time, or None. One push per match and side,
    whether or not the bank can bet it; marked as sent in the state that is saved before the push."""
    sent = st.setdefault("alerted", {})
    for k in [k for k, t in sent.items() if ts - t > 7 * 86400]:
        del sent[k]
    new = [m for m in view["upcoming"] if m.get("rec") and f"{m['event_id']}|{m['rec']['side']}" not in sent]
    if not new:
        return None
    lines = []
    for m in new:
        r = m["rec"]
        mk = "" if r["kind"] == "match" else f" ({r['market']})"
        lines.append(" · ".join([f"APOSTA em {r['side']}{mk}", f"odd {r['odds']}"]) + NL +
                     f"{m['match']} · {m['event']} · tier {m['tier'][1:]}" + NL + r["why"])
        sent[f"{m['event_id']}|{r['side']}"] = ts
    title = "🚨 Match fixing: 1 jogo" if len(new) == 1 else f"🚨 Match fixing: {len(new)} jogos"
    return title, (NL + NL).join(lines), new[0]["url"] if len(new) == 1 else None


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
