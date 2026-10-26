"""
How much each paper account bets: a share of its current balance, by how strong the signal was in the tests and
by the odds (Pedro, Oct 24 2026: "some matchfixings are clear and others are not; odds are relevant").

Method: per bucket, the test's edge (win rate minus the price paid) is shrunk toward zero by n/(n+40) so small
buckets can't claim much, then turned into a share of the Kelly stake, edge / (1 - price), and rounded to a simple
step: about half Kelly on the strong signals, small fixed stakes on untested or weak ones (raised from quarter Kelly
on Oct 24 2026 at Pedro's request). Every stake is 1-12% of the balance at the time of the bet (minimum $1).
Current steps: Live big money 8% (T3, under 0.65) / 5% (under 0.45) / 3% (0.45-0.80) / no bet at 0.80+; Radar 1.5% LOW,
2% MEDIUM, 3% HIGH or hand-picked; Tennis live 6% W50+ / 4% M25-W35 / 2% M15-W15, x1.5 at $3k+, x2 at $5k+;
Odd shifts 2%; Tennis radar 1.5%. The per-bucket notes below are the evidence.

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

MIN_USD, MAX_PCT = 1.0, 0.12        # raised Oct 24 2026 (Pedro): about half Kelly on the strongest signals


def _price(s):
    return (s.get("prices") or {}).get(s.get("side"))


def live_pct(s):
    p = _price(s)
    if p is None:
        return 0.01
    if s.get("vtier") == "T3" and p < 0.65:
        return 0.08
    if p < 0.45:
        return 0.05
    if p < 0.80:
        return 0.03
    return 0.0                                  # favourites 0.80+: no edge in the test, so no bet (Oct 24 2026)


def radar_pct(s):
    if s.get("live"):
        return live_pct(s)
    tags = " ".join(t for c in s.get("wallets") or [] for t in (c.get("tags") or []))
    if "Kalshi order" in tags:                  # Kalshi pre-match order: $1-2k +8.9% (n=170), $2k+ +23.6% (n=60)
        return 0.04 if (s.get("money") or 0) >= 2000 else 0.02
    if "your list" in tags or "(HIGH)" in tags:
        return 0.03
    return 0.02 if "(MEDIUM)" in tags else 0.015


def itf_level(match):
    """ITF prize level from the title: 'W15 ...', 'M25 ...', 'W75 ...' -> 15 / 25 / 75 (None if not ITF)."""
    import re
    m = re.match(r"\s*[MW](\d+)", match or "")
    return int(m.group(1)) if m else None


def tennis_live_pct(s):
    lvl = itf_level(s.get("match"))
    base = 0.06 if lvl and lvl >= 50 else 0.02 if lvl and lvl <= 15 else 0.04
    usd = s.get("money") or 0
    return base * (2.0 if usd >= 5000 else 1.5 if usd >= 3000 else 1.0)


def stake(pct_fn):
    """A stake function for informed.place_bets: share of the current balance, capped; 0 means no bet."""
    def f(s, bal):
        pct = pct_fn(s)
        return 0.0 if pct <= 0 else max(MIN_USD, round(bal * min(pct, MAX_PCT), 2))
    return f


LIVE = stake(live_pct)
RADAR = stake(radar_pct)
SHIFT = stake(lambda s: 0.02)
TENNIS_LIVE = stake(tennis_live_pct)
TENNIS_RADAR = stake(lambda s: 0.015)
