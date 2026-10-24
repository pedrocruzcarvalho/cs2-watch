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
Radar pre-match big bets: no edge in 60 days of CS2 data  ->  1%; hand-picked wallets  ->  2%
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
    return 0.02 if "your list" in tags else 0.01


def stake(pct_fn):
    """A stake function for informed.place_bets: share of the current balance, capped."""
    return lambda s, bal: max(MIN_USD, round(bal * min(pct_fn(s), MAX_PCT), 2))


LIVE = stake(live_pct)
RADAR = stake(radar_pct)
SHIFT = stake(lambda s: 0.015)
TENNIS_LIVE = stake(lambda s: 0.03)
TENNIS_RADAR = stake(lambda s: 0.01)
