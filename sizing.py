"""
How much each paper account bets: a share of its current balance, by how strong the signal was in the tests and
by the odds (Pedro, Oct 24 2026: "some matchfixings are clear and others are not; odds are relevant").

Method: per bucket, the test's edge (win rate minus the price paid) is shrunk toward zero by n/(n+40) so small
buckets can't claim much, then turned into a quarter of the Kelly stake, edge / (1 - price), and rounded down to a
simple step. Every stake is 1-8% of the balance at the time of the bet (minimum $1).

Live big money, 83 in-play bets of $4k+ on T3-T6 (live_research.py, 60 days):
  price < 0.45   n=18  edge +25 pts   ->  3%
  0.45-0.65      n=21  edge +9.5      ->  2%
  0.65-0.80      n=19  edge +6.6      ->  2%
  0.80+          n=25  edge -1        ->  1% (no edge measured; still bet small to keep testing)
  T3 and < 0.65  n=23  edge +26..+38  ->  5%
Tennis live, ITF in-play $2k+ on the underdog (< 0.45): n=56, ROI +46%, both halves +45/+50  ->  3%
Radar pre-match big bets: no edge in 60 days of CS2 data  ->  1% LOW, 1.5% MEDIUM, 2% HIGH; hand-picked wallets  ->  2%
Tennis live by ITF level (in-play underdog $1k+, 107 bets): W50+ +52% (both halves +), M25/W35 +4%, M15/W15 -14%
  ->  base 4% / 3% / 1.5%; by trigger size ($1-2k +2.5%, $2-3k +44%, $3-5k +62%)  ->  x1 / x1.5 ($3k+) / x2 ($5k+)
  (lower tiers are NOT better: the lowest ITF level and CS2 T4-T6 did worse than ITF W50+ and CS2 T3)
Odd shifts: no backtest  ->  1.5%.  Tennis radar (pre-match): tested negative  ->  1%.
"""

MIN_USD, MAX_PCT = 1.0, 0.08


def _price(s):
    return (s.get("prices") or {}).get(s.get("side"))


def live_pct(s):
    p = _price(s)
    if p is None:
        return 0.01
    if s.get("vtier") == "T3" and p < 0.65:
        return 0.05
    if p < 0.45:
        return 0.03
    if p < 0.80:
        return 0.02
    return 0.01


def radar_pct(s):
    if s.get("live"):
        return live_pct(s)
    tags = " ".join(t for c in s.get("wallets") or [] for t in (c.get("tags") or []))
    if "your list" in tags or "(HIGH)" in tags:
        return 0.02
    return 0.015 if "(MEDIUM)" in tags else 0.01


def itf_level(match):
    """ITF prize level from the title: 'W15 ...', 'M25 ...', 'W75 ...' -> 15 / 25 / 75 (None if not ITF)."""
    import re
    m = re.match(r"\s*[MW](\d+)", match or "")
    return int(m.group(1)) if m else None


def tennis_live_pct(s):
    lvl = itf_level(s.get("match"))
    base = 0.04 if lvl and lvl >= 50 else 0.015 if lvl and lvl <= 15 else 0.03
    usd = s.get("money") or 0
    return base * (2.0 if usd >= 5000 else 1.5 if usd >= 3000 else 1.0)


def stake(pct_fn):
    """A stake function for informed.place_bets: share of the current balance, capped."""
    return lambda s, bal: max(MIN_USD, round(bal * min(pct_fn(s), MAX_PCT), 2))


LIVE = stake(live_pct)
RADAR = stake(radar_pct)
SHIFT = stake(lambda s: 0.015)
TENNIS_LIVE = stake(tennis_live_pct)
TENNIS_RADAR = stake(lambda s: 0.01)
