"""
Replay the radar account with today's rules over a past period: which bets would it have made, and how did they do?

For every 10-minute scan time since the account opened, the scanner is run on the trade history cut off at that
moment (cs2_watch.poly_positions / poly_info with a cutoff), the radar is built with today's rules (cloud_run.radar)
and the radar account places its bets (radar_account.signals, once per market, before kickoff or in-play). Each bet
is filled at the last traded price at that moment + 1c, fee included, and settled with the real winner.

Limits: wallet facts (age, record) are today's; very busy matches only have their newest 3,000 trades.
Output: JSON list of bets (radar account format) for cloud_run to merge once (KV seed:radar_backfill).

Run: python radar_replay.py --since <unix time> --out file.json
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

import bankroll as B
import cloud_run as CR
import cs2_watch as C
import informed as I
import radar_account as RA
import sniper as SN
import wallets as W


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=float, required=True)
    ap.add_argument("--state", required=True, help="folder with the cloud state (cloud_run --local layout)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    from pathlib import Path
    st = Path(a.state)
    load = lambda n: json.loads((st / n).read_text()) if (st / n).exists() else {}
    C.DIR = st
    C.TEAMS = load("state_vrs")
    profiles = load("state_profiles")
    manual = load("watchlist")
    auto = load("state_watch_auto")
    C.WATCH = CR.watch_weights(C.update_watch(auto, [], time.time()), manual, profiles)
    now = time.time()

    # every CS2 event that could be on the radar at some point since --since, with all its trades (fetched once)
    lo = datetime.fromtimestamp(a.since, timezone.utc) - timedelta(hours=6)
    hi = datetime.now(timezone.utc) + timedelta(hours=36)
    events = [e for e in C.poly_matches(lo, hi) if C.tier(e["title"]) != "top"]
    trades = {}
    for e in events:
        try:
            trades[e["id"]] = C.poly_trades(e["id"])
        except Exception as err:
            print(f"  trades {e['title'][:40]}: {err}")
            trades[e["id"]] = []
    print(f"{len(events)} events, {sum(len(v) for v in trades.values())} trades", flush=True)
    winners = {}                                                # cid -> winner now (for settling)
    for e in events:
        for m in e["markets"]:
            winners[m["conditionId"]] = C.winner_of(m, e.get("ended"))

    orig_pos, orig_info = C.poly_positions, C.poly_info
    state = dict(account=dict(opened_at=a.since, bets={}))
    t = a.since
    while t <= now:
        T = t
        C.poly_matches = lambda lo_, hi_: [dict(e, ended=False, live=None, score=None) for e in events
                                          if lo_.timestamp() <= C.iso(e["startTime"]).timestamp() <= hi_.timestamp()]
        C.poly_trades = lambda eid: [x for x in trades.get(eid, []) if x["timestamp"] < T]
        C.poly_positions = lambda e, tr, cutoff=None: orig_pos(e, tr, T)
        C.poly_info = lambda e, tr, cutoff=None: orig_info(e, tr, T)
        dt = datetime.fromtimestamp(T, timezone.utc)
        _, rows, infos = C.scan(dt - timedelta(hours=6), dt + timedelta(hours=36), ["poly"], None, log=lambda *x: None)
        for i in infos:
            i["winner"] = None                                    # not known yet at time T
        for r in rows:
            if r.get("wallet"):
                r["record"] = W.summary(profiles.get(r["wallet"].lower()))
        rad = CR.radar(rows, infos, {}, {}, C.WATCH, T, profiles)
        sigs = RA.signals(rad, infos, T, skip_tiers=("T1",))
        for s in sigs:
            if not s["active"] or s["cid"] in state["account"]["bets"]:
                continue
            px = s["prices"].get(s["side"])
            if px is None or px >= 0.96:
                continue
            ask = min(px + 0.01, 0.99)
            sh, spent, avg, levels = B.fill(I.STAKE, [[ask, 1e9]])
            b = dict(I.sig_summary(s), placed_at=T, status="open", stake=round(spent, 2), shares=round(sh, 2),
                     price=round(avg, 4), levels=levels, replayed=True)
            w = winners.get(s["cid"])
            if w:
                b.update(winner=w, settled_at=max(T, s["start"]) + 3 * 3600,
                         status="won" if w == s["side"] else "lost",
                         pnl=round(sh - spent, 2) if w == s["side"] else -round(spent, 2))
            state["account"]["bets"][s["cid"]] = b
            print(f"  {dt:%H:%M} {'LIVE' if T >= s['start'] else 'pre '} {s['match'][16:56]:40} {s['market'][:20]:20} "
                  f"{s['side'][:18]:18} at {avg:.3f} -> {b['status']} {b.get('pnl', '')}", flush=True)
        t += 600
    C.poly_positions, C.poly_info = orig_pos, orig_info
    bets = list(state["account"]["bets"].values())
    done = [b for b in bets if b["status"] in ("won", "lost")]
    print(f"\n{len(bets)} bets: {sum(b['status'] == 'won' for b in bets)} won, {sum(b['status'] == 'lost' for b in bets)} lost, "
          f"{sum(b['status'] == 'open' for b in bets)} open; P&L ${sum(b.get('pnl', 0) for b in done):+.2f}")
    Path(a.out).write_text(json.dumps(bets))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
