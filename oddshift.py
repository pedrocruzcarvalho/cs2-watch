"""
Odd shifts: during a match, the price says one thing and the scoreboard says the opposite.

Examples Pedro sent (Oct 2026): Damajuana won map 1 13-8 and led map 2, yet traded at 35% (your end won);
Revise led map 3 7-3 at 19% (Privateer won); G2 Ares led map 3 14-12 at 0.3% (mellren won). Someone knows.

Model: Polymarket's live score ("013-008|1-0|Bo3": rounds in the current map | maps | format, home team first).
Each round is a coin tilted by the teams' pre-match strength (from the last pre-kickoff price), a map is first to 13
(12-12 counts as a coin flip), a series is best of N. That gives a fair chance for each team from the scoreboard.

Rules (fixed on 2026-10-20, before any result; NOT backtested, because Polymarket keeps no score history. Every
live snapshot is stored so it can be tested later):
  signal   live T3-T6 match (Valve ranking), match-winner market: the team AHEAD on the scoreboard (model >= 60%)
           trades at least 35 points below the model
  bet      $20 on the team the market prefers (the one behind on the scoreboard), once per match, at the next scan,
           filled against the real order book; skipped at 0.97+. $300 paper bankroll.
State: "account" (bets), "pre" (last pre-kickoff price per match), "snaps" (live snapshots). KV bundle part
state:oddshift.
"""

from functools import lru_cache

import informed as I

GAP = 0.35
MODEL_MIN = 0.60
TIERS = {"T3", "T4", "T5", "T6"}                     # T3+ only (Pedro, Oct 24 2026)
KEEP_SNAPS = 30 * 86400


def parse_score(s):
    """'6-1|0-0|Bo3' -> (6, 1, 0, 0, 3); None if it can't be read or carries no live data.
    The feed shows '000-000' when it has no round score: some matches only ever report maps ('000-000|1-0|Bo3')
    while the price follows the rounds being played. Without rounds the model is blind inside the current map and
    every swing looks like an odd shift, so these are skipped. Real round scores come unpadded ('6-1')."""
    try:
        cur, maps, fmt = s.split("|")
        if cur == "000-000":                                         # padded zeros = the feed has no round score
            return None
        ra, rb = (int(x) for x in cur.split("-"))
        ma, mb = (int(x) for x in maps.split("-"))
        return ra, rb, ma, mb, int(fmt.lower().replace("bo", ""))
    except Exception:
        return None


@lru_cache(maxsize=None)
def _ot(x, y, p):
    """Overtime block (6 rounds, first to 4; 3-3 starts a new block): chance to win from x-y."""
    if x >= 4:
        return 1.0
    if y >= 4:
        return 0.0
    if x == 3 and y == 3:
        return _ot_even(p)
    return p * _ot(x + 1, y, p) + (1 - p) * _ot(x, y + 1, p)


@lru_cache(maxsize=None)
def _map_win(ra, rb, p):
    """Chance the team with ra rounds wins a first-to-13 map (MR12, overtime at 12-12), each round won with p."""
    if ra >= 12 and rb >= 12:
        x, y = ra - 12, rb - 12
        while x >= 3 and y >= 3:
            x, y = x - 3, y - 3
        return _ot(x, y, p) if (x, y) != (0, 0) else _ot_even(p)
    if ra >= 13:
        return 1.0
    if rb >= 13:
        return 0.0
    return p * _map_win(ra + 1, rb, p) + (1 - p) * _map_win(ra, rb + 1, p)


def _ot_even(p):
    """From 12-12 (or any 3-3 in overtime): repeat blocks until someone takes 4 of 6."""
    w = l = 0.0                                                      # chance a block ends won / lost (not 3-3)
    stack = [(0, 0, 1.0)]
    while stack:
        x, y, pr = stack.pop()
        if x == 4:
            w += pr
        elif y == 4:
            l += pr
        elif x == 3 and y == 3:
            continue
        else:
            stack += [(x + 1, y, pr * p), (x, y + 1, pr * (1 - p))]
    return w / (w + l)


def _series(ma, mb, need, q):
    """Chance to win the series from ma-mb in maps, needing `need` maps, each map won with probability q."""
    if ma >= need:
        return 1.0
    if mb >= need:
        return 0.0
    return q * _series(ma + 1, mb, need, q) + (1 - q) * _series(ma, mb + 1, need, q)


def round_p(pre, bo):
    """Per-round edge that reproduces the pre-match series price (bisection)."""
    pre = min(max(pre, 0.05), 0.95)
    lo, hi = 0.3, 0.7
    for _ in range(30):
        mid = (lo + hi) / 2
        q = _map_win(0, 0, round(mid, 4))
        if _series(0, 0, bo // 2 + 1, q) < pre:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 4)


def model(score, pre_home):
    """Fair chance the home team wins the series, from the live score and the pre-match price."""
    ra, rb, ma, mb, bo = score
    p = round_p(pre_home, bo)
    q_now = _map_win(ra, rb, p)
    q = _map_win(0, 0, p)
    need = bo // 2 + 1
    return q_now * _series(ma + 1, mb, need, q) + (1 - q_now) * _series(ma, mb + 1, need, q)


VOID_BEFORE = 1792590076        # bets placed before the round-score fix were triggered by map-only scores: refunded


def void_blind(state):
    """Refund bets placed before VOID_BEFORE (Oct 21 2026): they came from scores without rounds, the bug above."""
    n = 0
    for b in state.get("account", {}).get("bets", {}).values():
        if b.get("placed_at", 0) < VOID_BEFORE and b["status"] in ("open", "won", "lost"):
            b.update(status="void", pnl=0.0, reason="score feed had no round data (bug, refunded)")
            n += 1
    return n


def signals(state, infos, vtier_of, now):
    pre, snaps = state.setdefault("pre", {}), state.setdefault("snaps", [])
    out = []
    for i in infos:
        if i.get("venue") != "poly" or i.get("market") != "Match Winner" or len(i.get("teams") or []) != 2:
            continue
        home, away = i["teams"]
        px = (i.get("prices") or {}).get(home)
        if px is None:
            continue
        key = i["cid"]
        if i["start"] > now:                                         # remember the last pre-kickoff price
            pre[key] = [round(px, 4), i["start"]]
            continue
        sc = parse_score(i.get("score") or "")
        if not i.get("live") or not sc or key not in pre or i.get("winner"):
            continue
        m = model(sc, pre[key][0])
        snaps.append([round(now), key, i.get("score"), round(px, 4), round(m, 4)])
        lead, lead_px, lead_m = (home, px, m) if m >= 0.5 else (away, 1 - px, 1 - m)
        side = away if lead == home else home
        vt = vtier_of(i["match"])
        reasons = []
        if vt not in TIERS:
            reasons.append(f"match tier {vt} (only T3-T6)")
        if lead_m < MODEL_MIN:
            reasons.append(f"scoreboard too close ({lead} {lead_m:.0%} by the model)")
        elif lead_m - lead_px < GAP:
            reasons.append(f"price {lead_px:.0%} vs model {lead_m:.0%} for {lead}: gap under {GAP * 100:.0f} points")
        ra, rb, ma, mb, bo = sc
        out.append(dict(match=i["match"], start=i["start"], market="Match Winner", cid=key, slug=i.get("ref"),
                        url=i.get("url"), teams=i["teams"], prices=i.get("prices") or {}, tokens=i.get("tokens") or {},
                        side=side, money=0, other_money=0, wallets=[], against=[], vtier=vt,
                        score=dict(home=home, away=away, rounds=[ra, rb], maps=[ma, mb], bo=bo),
                        leader=lead, leader_price=round(lead_px, 3), leader_model=round(lead_m, 3),
                        gap=round(lead_m - lead_px, 3), pre_home=pre[key][0], active=not reasons, why_not=reasons))
    state["snaps"] = [s for s in snaps if s[0] >= now - KEEP_SNAPS]
    state["pre"] = {k: v for k, v in pre.items() if v[1] >= now - 2 * 86400}
    return sorted(out, key=lambda s: (not s["active"], -s["gap"]))


def explain(b, now):
    sc = b.get("score") or {}
    r, mp = sc.get("rounds", ["?", "?"]), sc.get("maps", ["?", "?"])
    head = (f"{I.money(b['stake'])} on {b['side']} to beat {b.get('leader')}: the scoreboard favoured {b.get('leader')} "
            f"but the market priced it far lower.")
    why = [f"Score when bet: {sc.get('home')} {r[0]}-{r[1]} {sc.get('away')} in the current map, maps {mp[0]}-{mp[1]} "
           f"(best of {sc.get('bo')}).",
           f"From that score and the pre-match price, {b.get('leader')} should have been about {b.get('leader_model', 0):.0%}. "
           f"The market had it at {b.get('leader_price', 0):.0%}, a gap of {b.get('gap', 0) * 100:.0f} points.",
           "Rule: a gap of 35+ points on a live T3-T6 match. The bet follows the market, on the theory that someone "
           "knows the leading team will not finish the job."]
    if b["status"] != "skipped":
        why.append(f"Bet {I.money(b['stake'])} at {b['price']:.3f}, filled against the real order book, fee included. "
                   f"If {b['side']} wins it pays {I.money(b['shares'])}.")
    res = {"won": f"{b['side']} won: paid {I.money(b.get('shares', 0))}, profit {I.money(b.get('pnl', 0))}.",
           "lost": f"{b.get('winner')} won: the {I.money(b['stake'])} stake was lost.",
           "skipped": "Not placed: no one was selling on the order book.", "open": "Not settled yet.",
           "void": f"Refunded: {b.get('reason') or 'cancelled'}."}.get(b["status"], b["status"])
    return dict(headline=head, sections=[
        dict(title="Why this bet", bullets=why),
        dict(title="Why it could be wrong", bullets=[
            "Not backtested: Polymarket keeps no score history, so this rule is tested live from today.",
            "The score can lag the game, and a big gap can also be a stand-in, a disconnect or a lineup issue the model "
            "doesn't know about."]),
        dict(title="Result", bullets=[res])])


def dashboard(state, sigs, now):
    view = I.account_view(state, sigs, now, explain, "SHIFT", ("T3", "T4", "T5", "T6"),
                          ("-", "-"))
    return dict(view, rules=dict(gap=GAP, model_min=MODEL_MIN, stake=I.STAKE, start=I.START_BALANCE),
                snaps=len(state.get("snaps", [])))
