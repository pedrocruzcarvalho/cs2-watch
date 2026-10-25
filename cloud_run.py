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
import radar_account as RA
import sizing as SZ
import sniper as SN
import tennis_watch as TW
import wallet_history as WH
import wallets as W

KEYS = {"state:ledger": "ledger.json", "state:wallets": "wallets.json", "state:baseline": "baseline.json",
        "state:meta": "meta.json", "state:oddspapi": "oddspapi.json", "state:watch_auto": "watch_auto.json",
        "watchlist": "watchlist.json", "state:profiles": "profiles.json", "state:vrs": "vrs.json",
        "state:informed": "informed.json", "state:livemoney": "livemoney.json", "state:oddshift": "oddshift.json", "state:radar": "radar.json", "state:sniper": "sniper.json", "state:tradar": "tradar.json", "state:tlive": "tlive.json"}
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
LIVE_TIERS = {"T3", "T4", "T5", "T6"}                        # the radar watches in-play money on these only (Pedro)
LIVE_LEVELS = [("HIGH", 25000), ("MEDIUM", 10000), ("LOW", 4000)]   # a big in-play bet
LIST_MIN = 900
FRESH_START = "2026-10-24b"    # Pedro: clear every account back to $300 (second fresh start, radar pre-match cut)


def restart(state, now):
    """Once per FRESH_START: archive the account's bets and open a fresh $300 account (other state, e.g. odd-shift
    snapshots, is kept). Each account remembers its own restart, so one failing section can't skip it."""
    if state.get("fresh_start") == FRESH_START:
        return
    state["fresh_start"] = FRESH_START
    old = state.get("account")
    if old and old.get("bets"):
        state.setdefault("archive", []).append(dict(closed_at=now, opened_at=old.get("opened_at"),
                                                    bets=list(old["bets"].values())))
    state["account"] = dict(opened_at=now, bets={})
TOP_TIERS = ("T1", "T2", "T2*")                # never bet by Radar or Live big money (Pedro, Oct 22 2026: T3+ only)


def drop_top_tiers(state, log, name):
    """Once: settled bets on T1/T2 matches are removed (refunded, hidden) and the balance recalculates (Pedro)."""
    if state.get("t12_removed"):
        return
    n = 0
    for b in state.get("account", {}).get("bets", {}).values():
        if b.get("vtier") in TOP_TIERS and b["status"] in ("won", "lost"):
            b.update(status="void", pnl=0.0, reason="tier 1/2 match, removed (only T3+ is bet)")
            n += 1
    state["t12_removed"] = True
    log(f"{name}: removed {n} settled T1/T2 bets")                  # a hand-picked wallet triggers the radar from this size (Pedro, Oct 21 2026)


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


def radar(rows, infos, qual, snipers, watch, now, profiles=None, limit=40):
    """The radar (Pedro's rule, Oct 21 2026, "that's literally it"):
      big bet     any big, out-of-place bet (the scanner's LOW/MEDIUM/HIGH; Kalshi MEDIUM+), whoever placed it:
                  before kickoff and during the match, T3-T6 only (T1/T2 never, Pedro Oct 22 2026), where a big in-play bet is
                  $4k+ LOW, $10k+ MEDIUM, $25k+ HIGH
      your list   a hand-picked wallet betting $900+
    Shown on the page and bet by the radar account."""
    profiles, snipers = profiles or {}, snipers or {}
    groups = {}

    def add(match, market, start, url, item, score, live=False):
        g = groups.setdefault((match, market, live), dict(match=match, market=market, start=start, url=url, live=live,
                                                          vtier=C.vtier_fix(match), items=[], score=0))
        g["items"].append(item)
        g["score"] += score

    def tags_for(w, usd, level, venue):
        tags, score = [], 0
        wt = watch_tag(w, watch) if w else None
        if wt and wt["kind"] == "watched" and usd >= LIST_MIN:
            tags.append(wt); score += 8
        if level and (venue != "kalshi" or level in ("MEDIUM", "HIGH")):
            tags.append(dict(kind="big", label=f"big bet ({level})")); score += 1 + LEVEL_RANK.get(level, 0)
        if tags and venue == "kalshi":
            tags.insert(0, dict(kind="kalshi", label="Kalshi"))
        return tags, score

    def who_line(w):
        if not w:
            return "anonymous (Kalshi)"
        if w in snipers:
            p = snipers[w]
            return (f"sniper: {p['n']} CS2 bets in its whole history, won {p['won']:.0%} ({p['edge'] * 100:+.0f} pts over "
                    f"its prices), CS2 profit ${p.get('cs2_pnl', 0):,.0f}")
        return who(w, profiles, qual)

    seen = set()
    for r in rows:
        w = (r.get("wallet") or "").lower()
        if r["start"] <= now or not (w or r.get("venue") == "kalshi"):
            continue
        tags, score = tags_for(w, r["usd"], r.get("level"), r.get("venue"))
        if not tags:
            continue
        seen.add((w, r["match"], r["market"], r["outcome"]))
        add(r["match"], r["market"], r["start"], r.get("url"),
            dict(wallet=r.get("wallet") or "", name=r.get("name"), outcome=r["outcome"], usd=round(r["usd"]),
                 avg=round(r.get("avg_price") or 0, 3), tags=tags, why=(r.get("why") or [])[:6], who=who_line(w)), score)
    for i in infos:                                     # smaller bets of followed wallets, which rows leave out
        if i.get("venue") != "poly" or i["start"] <= now:
            continue
        for w, _, o, usd, avg, name in i.get("allpos") or []:
            if (w, i["match"], i["market"], o) in seen:
                continue
            tags, score = tags_for(w, usd, None, "poly")
            if not tags:
                continue
            add(i["match"], i["market"], i["start"], i.get("url"),
                dict(wallet=w, name=name, outcome=o, usd=round(usd), avg=round(avg, 3), tags=tags, why=[], who=who_line(w)),
                score)
    for i in infos:                                     # in-play money, T3-T6 (and T2*) only (Pedro, Oct 21 2026)
        if i.get("venue") != "poly" or not i["start"] <= now < i["start"] + 5 * 3600 or i.get("winner")                 or C.vtier_fix(i["match"]) not in LIVE_TIERS:
            continue
        for w, _, o, usd, avg, name, last in i.get("livepos") or []:
            lvl = next((l for l, v in LIVE_LEVELS if usd >= v), None)
            tags, score = tags_for(w, usd, lvl, "poly")
            if not tags:
                continue
            add(i["match"], i["market"], i["start"], i.get("url"),
                dict(wallet=w, name=name, outcome=o, usd=round(usd), avg=round(avg, 3), tags=tags, at=last,
                     why=[f"${usd:,.0f} bought during the match"], who=who_line(w)), score, live=True)
    out = sorted((g for g in groups.values() if g["vtier"] not in TOP_TIERS),       # T3+ only (Pedro, Oct 22 2026)
                 key=lambda g: (not g["live"], -g["score"], g["start"]))[:limit]
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

    # Informed wallets and Snipers were switched off at Pedro's request (Oct 24 2026); their stored state is kept.
    reset = meta.get("reset") != RESET                             # one-time restart of all paper accounts
    if reset:
        log(f"resetting every paper account to ${I.START_BALANCE:.0f} ({RESET})")
        meta["reset"] = RESET
    ts = now.timestamp()
    fresh = meta.get("fresh_start") != FRESH_START              # every account back to $300 once, history archived

    # ---- live big money: the second paper account (live_money.py) ----
    lst = {} if reset else O.load(work / "livemoney.json")
    drop_top_tiers(lst, log, "live money")
    restart(lst, ts)
    lsigs = LM.signals(infos, C.vtier_fix, ts)
    I.settle(lst, infos, ts, fetch_winner, log=log)
    I.place_bets(lst, lsigs, ts, C.book_asks, B.fill, log=log, live=True, tag="live", stake_fn=SZ.LIVE)
    (work / "livemoney.json").write_text(json.dumps(lst))
    live = LM.dashboard(lst, lsigs, ts)
    log(f"live money: {len(lsigs)} markets with in-play money, {sum(s['active'] for s in lsigs)} active signals, "
        f"balance ${live['stats']['balance']:.2f}")

    # ---- odd shifts: price vs scoreboard (oddshift.py) ----
    ost = O.load(work / "oddshift.json")
    if reset:
        ost.pop("account", None)
    restart(ost, ts)
    if OS.void_blind(ost):
        log("odd shifts: refunded bets placed on scores without rounds")
    osigs = OS.signals(ost, infos, C.vtier_fix, ts)
    I.settle(ost, infos, ts, fetch_winner, log=log)
    I.place_bets(ost, osigs, ts, C.book_asks, B.fill, log=log, live=True, tag="odd shift", stake_fn=SZ.SHIFT)
    (work / "oddshift.json").write_text(json.dumps(ost))
    shift = OS.dashboard(ost, osigs, ts)
    log(f"odd shifts: {len(osigs)} live matches with a score, {sum(s['active'] for s in osigs)} active signals, "
        f"{shift['snaps']} snapshots kept, balance ${shift['stats']['balance']:.2f}")

    # ---- radar account: bets what the pre-match radar shows (radar_account.py) ----
    rad = radar(rows, infos, {}, {}, C.WATCH, ts, profiles)
    rst = {} if reset else O.load(work / "radar.json")
    seed = store.get("seed:radar_backfill") if not rst.get("backfilled") else None
    if seed:
        log(f"radar account: added {RA.merge_backfill(rst, json.loads(seed))} replayed bets (today's rules)")
    drop_top_tiers(rst, log, "radar account")
    n = RA.restore_big(rst)
    if n:
        log(f"radar account: restored {n} big-money bets that had been refunded")
    restart(rst, ts)
    rsigs = RA.signals(rad, infos, ts, skip_tiers=TOP_TIERS, pre_match_only_list=True)
    I.settle(rst, infos, ts, fetch_winner, log=log)
    I.place_bets(rst, rsigs, ts, C.book_asks, B.fill, log=log, tag="radar", stake_fn=SZ.RADAR)
    I.place_bets(rst, rsigs, ts, C.book_asks, B.fill, log=log, live=True, tag="radar live", stake_fn=SZ.RADAR)
    (work / "radar.json").write_text(json.dumps(rst))
    placed = rst.get("account", {}).get("bets", {})
    bet_on = {(x["match"], x["market"]) for x in rsigs if x["active"] or x["cid"] in placed}
    rad = [g for g in rad if (g["match"], g["market"]) in bet_on]     # only what the radar account bets (Pedro)
    radar_acct = RA.dashboard(rst, rsigs, ts, ("T3", "T4", "T5", "T6"))
    log(f"radar account: {len(rad)} radar markets, {sum(s['active'] for s in rsigs)} active signals, "
        f"balance ${radar_acct['stats']['balance']:.2f}")

    # ---- tennis, separate from CS2: radar + radar account (tennis_watch.py) ----
    tennis = None
    try:
        tinfos, trows = TW.scan(now, C.WATCH, log=log)
        trad = TW.radar(trows, C.WATCH, ts)
        # the tennis radar account was switched off (Pedro, Oct 24 2026: pre-match tennis big bets lost -31% in 30 days);
        # the radar list stays on the page as information
        tlst = O.load(work / "tlive.json")                        # tennis live: ITF underdogs bought in-play
        restart(tlst, ts)
        tlsigs = TW.live_signals(tinfos, ts)
        I.settle(tlst, tinfos, ts, fetch_winner, log=log)
        I.place_bets(tlst, tlsigs, ts, C.book_asks, B.fill, log=log, live=True, tag="tennis live",
                     stake_fn=SZ.TENNIS_LIVE)
        (work / "tlive.json").write_text(json.dumps(tlst))
        tennis = dict(radar=[], live_acct=I.account_view(tlst, tlsigs, ts, TW.live_explain, "TLIVE", ("ITF",),
                                               ("1 wallet", "2+ wallets")),
                      scanned=len(tinfos))
        log(f"tennis live: {len(tlsigs)} ITF matches with in-play money, {sum(s['active'] for s in tlsigs)} active "
            f"signals, balance ${tennis['live_acct']['stats']['balance']:.2f}")
    except Exception as e:                                 # tennis must never stop the CS2 tick
        log(f"tennis failed: {e!r}")

    if fresh:
        meta["fresh_start"] = FRESH_START
        log(f"fresh start: every paper account back to $300 ({FRESH_START}), old bets archived")
    meta["last_run"] = now.timestamp()
    meta["runs"] = meta.get("runs", 0) + 1
    dash = dict(at=now.timestamp(), opened_at=live["opened_at"], took=round(time.time() - t0, 1), runs=meta["runs"],
                stats=live["stats"], series=live["series"], bets=live["bets"], signals=live["signals"],
                scanned=len({m["match_id"] for m in matches}), flagged=sum(1 for s in lsigs if s["active"]),
                watch=watch_view(auto_watched, manual, rows, profiles),
                live=live, shift=shift, radar=rad, radar_acct=radar_acct, tennis=tennis)
    stats = live["stats"]
    (work / "meta.json").write_text(json.dumps(meta))

    parts = {key: (work / name).read_text() for key, name in KEYS.items()
             if key != "watchlist" and (work / name).exists()}
    store.put(BUNDLE, gzip.compress(json.dumps(parts).encode()))
    store.put("dashboard", json.dumps(dash, default=str).encode())
    log(f"tick done in {time.time() - t0:.0f}s: {len(matches)} markets, {dash['flagged']} live signals, "
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
