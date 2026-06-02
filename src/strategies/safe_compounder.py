"""
Safe Compounder Strategy — Ported from ~/dev/apex/safe_compounder.py

NO-side only, edge-based, capital-efficient.

STRATEGY:
- NO side ONLY
- Find near-certain outcomes (EV ~95-99¢)
- Edge = estimated_true_prob - lowest_no_ask > 5¢
- Lowest NO ask must be > 80¢
- Place resting order at lowest_no_ask - 1¢ (maker trade, near-zero fees)
- Position size: max 10% of portfolio value per position (Kelly optional)

KEY INSIGHT: We estimate true probability dynamically:
- YES last price is our primary signal (lower = more certain NO wins)
- Time to expiry amplifies certainty (if YES is at 3¢ with 2 days left, it's ~99%)
- We compare our EV estimate to the actual NO ask price
- Edge = EV - NO ask. Only trade when edge > 5¢.

Integrated with the repo's KalshiClient and DatabaseManager.
Available via: python cli.py run --safe-compounder
"""

import asyncio
import json
import logging
import math
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp
import aiosqlite

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------

# Series whitelist. The bot only trades markets whose ticker starts with
# one of these prefixes followed by "-" (so KXWTI matches KXWTI-... but
# not KXWTIW-... or KXWTIMINM-...). Replaces the previous SKIP_PREFIXES
# blacklist, which was reactive: we kept losing money on new Kalshi
# series until each one was banned by hand.
#
# Selection: n>=10 settlements AND net-positive realized P&L. Across 666
# historical trades, these 12 series produced +$83 on ~$1,500 cost while
# the excluded long tail of 121 distinct series produced -$239. If the
# whitelist had been enforced from the start, lifetime P&L would have
# been +$83 instead of -$156.
SAFE_SERIES = frozenset({
    # Energy & commodities — 90%+ of historical volume.
    "KXWTI",            # WTI daily            n=70  +$13.70  +3.9%/$
    "KXWTIW",           # WTI weekly           n=41  +$ 9.46  +4.2%/$
    "KXBRENTD",         # Brent daily          n=42  +$ 4.58  +6.2%/$
    "KXBRENTW",         # Brent weekly         n=18  +$ 3.44  +6.3%/$
    "KXAAAGASD",        # AAA gas daily        n=27  +$19.92  +6.3%/$
    "KXAAAGASW",        # AAA gas weekly       n=17  +$12.42  +6.8%/$
    "KXSILVERD",        # Silver daily         n=18  +$ 1.78  +7.1%/$
    # Equity index.
    "KXINXU",           # S&P futures          n=44  +$ 3.33  +2.6%/$
    # Crypto dailies — still gated by 10% spot-distance check below.
    "KXBTC",            # BTC daily            n=18  +$ 8.13 +12.2%/$
    "KXETH",            # ETH daily            n=10  +$ 2.85  +5.0%/$
    # Polling / annual events — modest diversification away from energy.
    "KXAPRPOTUS",       # Trump approval poll  n=10  +$ 1.86  +5.9%/$
    "KXEUROVISIONRANK", # Eurovision rankings  n=11  +$ 1.95  +5.9%/$
})

# Crypto tickers that get a price-distance check instead of a blanket skip.
# Only traded when the market threshold is >=CRYPTO_MARGIN away from spot price.
CRYPTO_TICKER_MAP = {
    "KXBTCD": "BTC", "KXBTC": "BTC",
    "KXETHD": "ETH", "KXETH": "ETH",
    "KXSOLD": "SOL", "KXSOL": "SOL",
    "KXDOGE": "DOGE",
    "KXBNB": "BNB",
    "KXXRP": "XRP",
    "KXHYPE": "HYPE",
}
CRYPTO_MARGIN = 0.10  # Threshold must be >=10% away from current spot price (loosened from 15%)

SKIP_TITLE_PHRASES = [
    "mention", "say in", "speech mention", "address mention",
    "temperature", "rainfall", "snowfall", "hurricane", "tornado",
    "flood", "drought", "precipitation",
]

# Thresholds (all in dollar format 0.00-1.00)
MIN_VOLUME = 500           # Raised from 10 — need real liquidity at $5k scale
MIN_NO_ASK = 0.90          # Raised from $0.80 — near-certain outcomes only
MIN_EDGE = 0.03            # Lowered from $0.05 to catch more opportunities
MAX_POSITION_PCT = 0.03    # Percentage cap, applied alongside the dollar cap
# Absolute hard dollar cap per position. Applied as min(pct × bankroll,
# MAX_POSITION_DOLLARS). At the ~$1,400 bankroll the absolute cap binds
# (3% = ~$42, $25 absolute is tighter); below ~$830 bankroll the
# percentage cap binds first. Caps tail risk on the 22:1 payoff
# asymmetry — a losing trade is ~$25 instead of ~$42 uncapped.
# Raise this as the whitelist accumulates evidence at scale.
MAX_POSITION_DOLLARS = 25.0
USE_KELLY = True
MIN_CONFIDENCE = 0.50      # Balanced: filters thin/wide markets without being too restrictive

# Per-event concentration cap. Multiple Kalshi markets in the same event
# (e.g. WTI strikes around one price) are correlated — they all settle on
# the same underlying print. Cap total bankroll exposure per event.
EVENT_CAP_PCT = 0.15
EVENT_CAP_DRY_RUN = False  # When True, log would-have-blocked but still place.

# Cash reserve breaker. Keep at least this fraction of total bankroll
# (cash + portfolio_value) as uncommitted cash. Prevents the bot from
# fully deploying — without this, available cash drains to ~0 and Kalshi
# starts rejecting orders with HTTP 400 "insufficient_balance" (which
# spammed the log on 26MAY29 — 30+ rejections in a single cycle).
CASH_RESERVE_PCT = 0.10
CASH_RESERVE_DRY_RUN = False  # When True, log would-have-blocked but still place.

# Drawdown breaker. Halts new order placement if total equity falls
# DRAWDOWN_THRESHOLD below the all-time peak. Peak is seeded from current
# equity on first run and only ratchets up. Trip is STICKY — once tripped,
# the bot stays halted until BREAKERS_STATE_PATH is deleted (manual reset
# is a feature, not a bug: when this trips, the strategy needs human
# review, not a self-resume that walks back into the same trade).
# Targets slow-bleed accumulation (which daily-loss limits miss), not
# single-day blowups.
DRAWDOWN_THRESHOLD = 0.10
BREAKERS_STATE_PATH = "data/safe_compounder_breakers.json"


# -----------------------------------------------------------------------
# Core math
# -----------------------------------------------------------------------

def in_whitelist(ticker: str) -> bool:
    """True when the ticker belongs to a SAFE_SERIES prefix. Matches on
    `prefix + "-"` so KXWTI does not accidentally swallow KXWTIW or
    KXWTIMINM — Kalshi tickers always have a dash after the series root."""
    upper = ticker.upper()
    return any(upper.startswith(p + "-") for p in SAFE_SERIES)


def _event_of(ticker: str) -> str:
    """Strip the trailing strike suffix to get the event ticker.
    e.g. KXWTI-26MAY28H17-T84.99 -> KXWTI-26MAY28H17
    All markets in one event share the same underlying — correlated risk."""
    return ticker.rsplit("-", 1)[0]


def _load_breakers_state() -> Dict:
    try:
        with open(BREAKERS_STATE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_breakers_state(state: Dict) -> None:
    os.makedirs(os.path.dirname(BREAKERS_STATE_PATH) or ".", exist_ok=True)
    with open(BREAKERS_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def check_drawdown_breaker(cash_cents: int, portfolio_cents: int) -> Tuple[bool, str]:
    """Returns (allowed_to_place_orders, status_message).

    Reads/writes BREAKERS_STATE_PATH as a side effect:
    - First call ever: seeds peak from current equity.
    - Equity above stored peak: ratchets peak up.
    - Equity below peak by DRAWDOWN_THRESHOLD: flips tripped=True.
    - tripped=True: stays tripped until the state file is deleted.
    """
    equity = cash_cents + portfolio_cents
    state = _load_breakers_state()
    now_utc = datetime.now(timezone.utc).isoformat()

    # First run — seed peak from current.
    if "peak_equity_cents" not in state:
        state.update({
            "peak_equity_cents": equity,
            "peak_timestamp_utc": now_utc,
            "tripped": False,
        })
        _save_breakers_state(state)
        return True, (
            f"Drawdown breaker: seeded peak at ${equity/100:.2f} "
            f"(state file created at {BREAKERS_STATE_PATH})"
        )

    # Sticky trip — manual reset only.
    if state.get("tripped", False):
        return False, (
            f"Drawdown breaker TRIPPED at {state.get('tripped_at_utc')}: "
            f"equity ${state.get('tripped_equity_cents', 0)/100:.2f} = "
            f"-{state.get('tripped_drawdown_pct', 0)*100:.1f}% from peak "
            f"${state['peak_equity_cents']/100:.2f}. "
            f"Manual reset required — delete {BREAKERS_STATE_PATH}."
        )

    peak = state["peak_equity_cents"]

    # New peak — ratchet up.
    if equity > peak:
        state["peak_equity_cents"] = equity
        state["peak_timestamp_utc"] = now_utc
        _save_breakers_state(state)
        return True, (
            f"Drawdown breaker: new peak ${equity/100:.2f} "
            f"(previous ${peak/100:.2f})"
        )

    # Below peak — check drawdown.
    drawdown = (peak - equity) / peak if peak > 0 else 0.0
    if drawdown >= DRAWDOWN_THRESHOLD:
        state.update({
            "tripped": True,
            "tripped_at_utc": now_utc,
            "tripped_equity_cents": equity,
            "tripped_drawdown_pct": drawdown,
        })
        _save_breakers_state(state)
        return False, (
            f"Drawdown breaker TRIPPED: equity ${equity/100:.2f} = "
            f"-{drawdown*100:.1f}% from peak ${peak/100:.2f} "
            f"(threshold {DRAWDOWN_THRESHOLD*100:.0f}%). "
            f"Manual reset — delete {BREAKERS_STATE_PATH}."
        )

    return True, (
        f"Drawdown breaker OK: equity ${equity/100:.2f}, peak ${peak/100:.2f} "
        f"(-{drawdown*100:.1f}% / threshold -{DRAWDOWN_THRESHOLD*100:.0f}%)"
    )


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _pos_count(p: Dict) -> float:
    """Position count, supporting both legacy int 'position' and new 'position_fp'."""
    if "position_fp" in p:
        return _to_float(p["position_fp"])
    return _to_float(p.get("position", 0))


def _pos_cost_cents(p: Dict) -> int:
    """Cost basis of an open position in cents. Uses market_exposure_dollars
    (cash currently committed to the position). Falls back to face value if
    that field is missing."""
    exposure = p.get("market_exposure_dollars")
    if exposure is not None:
        return int(round(_to_float(exposure) * 100))
    # Fallback: count * 100¢ (face value — conservative overestimate)
    return int(abs(_pos_count(p)) * 100)


def _order_cost_cents(o: Dict) -> int:
    """Committed cost of a resting order in cents. Defensive against the API
    returning either cent-int or dollar-string price fields."""
    count = _to_float(o.get("remaining_count", o.get("count", 0)))
    # Try dollar-string fields first, fall back to legacy cent ints.
    side = o.get("side", "no")
    if side == "no":
        price = o.get("no_price_dollars")
        if price is not None:
            return int(round(count * _to_float(price) * 100))
        return int(count * _to_float(o.get("no_price", 0)))
    price = o.get("yes_price_dollars")
    if price is not None:
        return int(round(count * _to_float(price) * 100))
    return int(count * _to_float(o.get("yes_price", 0)))


def parse_crypto_market(ticker: str) -> Optional[Tuple[str, str, float]]:
    """
    Parse a crypto Kalshi ticker into (symbol, direction, threshold).

    KXBTCD-26MAY0517-T83249.99 -> ("BTC", "T", 83249.99)  T = YES if price exceeds threshold
    KXBTCD-26MAY0517-B83249.99 -> ("BTC", "B", 83249.99)  B = YES if price stays below threshold

    Returns None if ticker isn't a recognised crypto market.
    """
    upper = ticker.upper()
    symbol = None
    for prefix, sym in CRYPTO_TICKER_MAP.items():
        if upper.startswith(prefix.upper()):
            symbol = sym
            break
    if symbol is None:
        return None
    m = re.search(r"-([TB])([\d.]+)$", ticker)
    if not m:
        return None
    return symbol, m.group(1), float(m.group(2))


def is_crypto_threshold_safe(
    symbol: str, direction: str, threshold: float, prices: Dict[str, float]
) -> bool:
    """
    Return True if the market threshold is >=CRYPTO_MARGIN away from spot
    in the direction that favours a NO win.

    direction "T": market resolves YES if price > threshold.
                   NO wins when price stays BELOW threshold.
                   Safe when threshold is >=15% ABOVE current price.

    direction "B": market resolves YES if price < threshold.
                   NO wins when price stays ABOVE threshold.
                   Safe when current price is >=15% ABOVE threshold.
    """
    current = prices.get(symbol)
    if current is None or current <= 0:
        return False
    if direction == "T":
        return threshold >= current * (1 + CRYPTO_MARGIN)
    else:
        return current >= threshold * (1 + CRYPTO_MARGIN)


async def fetch_crypto_prices() -> Dict[str, float]:
    """
    Fetch BTC/ETH/SOL spot prices from CoinGecko (no API key required).
    Returns {} on any failure so callers can fail-safe by skipping crypto.
    """
    url = "https://api.coingecko.com/api/v3/simple/price"
    coin_map = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
        "DOGE": "dogecoin",
        "BNB": "binancecoin",
        "XRP": "ripple",
        "HYPE": "hyperliquid",
    }
    params = {"ids": ",".join(coin_map.values()), "vs_currencies": "usd"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=6)
            ) as resp:
                data = await resp.json()
                # Any missing coin (CoinGecko outage, delisting, new asset) is
                # simply omitted — its markets will fail is_crypto_threshold_safe
                # and be skipped, which is the safe behaviour.
                return {
                    sym: float(data[gid]["usd"])
                    for sym, gid in coin_map.items()
                    if gid in data and "usd" in data[gid]
                }
    except Exception as e:
        logger.warning("Could not fetch crypto prices (%s) — crypto markets skipped", e)
        return {}



def estimate_true_no_prob(yes_last: float, hours_to_expiry: float) -> float:
    """
    Estimate the true probability that NO wins.
    Returns estimated true NO probability in dollars (0.00-1.00).
    """
    base_prob = 1.0 - yes_last

    if hours_to_expiry <= 0:
        return base_prob

    if hours_to_expiry <= 24:
        if yes_last <= 0.05:
            return min(0.99, base_prob + 0.04)
        elif yes_last <= 0.10:
            return min(0.98, base_prob + 0.03)
        elif yes_last <= 0.15:
            return min(0.97, base_prob + 0.02)
        else:
            return min(0.96, base_prob + 0.01)
    elif hours_to_expiry <= 72:
        if yes_last <= 0.05:
            return min(0.99, base_prob + 0.03)
        elif yes_last <= 0.10:
            return min(0.97, base_prob + 0.02)
        else:
            return base_prob + 0.01
    elif hours_to_expiry <= 168:
        if yes_last <= 0.05:
            return min(0.98, base_prob + 0.02)
        elif yes_last <= 0.10:
            return min(0.96, base_prob + 0.01)
        else:
            return base_prob
    else:
        if yes_last <= 0.03:
            return min(0.97, base_prob + 0.01)
        return base_prob


def kelly_fraction(prob_win: float, payout_ratio: float) -> float:
    """Kelly fraction for a binary bet."""
    if payout_ratio <= 0 or prob_win <= 0:
        return 0.0
    prob_lose = 1.0 - prob_win
    f = (prob_win * payout_ratio - prob_lose) / payout_ratio
    return max(0.0, f)


def market_confidence_score(ticker: str, orderbook: dict, market: dict) -> Tuple[float, str]:
    """Return (confidence_score 0-1, reason_str) for a market."""
    reasons = []

    # Handle both new and old orderbook formats
    no_side = orderbook.get("no_dollars", orderbook.get("no", []))
    yes_side = orderbook.get("yes_dollars", orderbook.get("yes", []))

    all_levels = []
    for price_data, qty_data in yes_side:
        try:
            # Handle both old [price_cents, qty] and new [price_dollars_string, size_string]
            price = float(price_data)
            qty = int(qty_data)
            # Convert cents to dollars if needed
            if price > 1.0:
                price = price / 100.0
            all_levels.append((1.0 - price, qty))  # Convert YES to NO price in dollars
        except (ValueError, TypeError):
            continue
    
    for price_data, qty_data in no_side:
        try:
            price = float(price_data)
            qty = int(qty_data)
            # Convert cents to dollars if needed
            if price > 1.0:
                price = price / 100.0
            all_levels.append((price, qty))
        except (ValueError, TypeError):
            continue

    if all_levels:
        best_ask = min(p for p, q in all_levels)
        total_vol = sum(q for _, q in all_levels)
        vol_within_3c = sum(q for p, q in all_levels if p <= best_ask + 0.03)  # 3¢ = $0.03
        depth_ratio = vol_within_3c / max(total_vol, 1)
    else:
        depth_ratio = 0.0
        reasons.append("no book")

    best_no_ask = None
    if yes_side:
        try:
            highest_yes_bid = max(float(p) for p, q in yes_side)
            # Convert cents to dollars if needed
            if highest_yes_bid > 1.0:
                highest_yes_bid = highest_yes_bid / 100.0
            best_no_ask = 1.0 - highest_yes_bid
        except (ValueError, TypeError):
            pass
    
    best_no_bid = 0
    if no_side:
        try:
            best_no_bid = max(float(p) for p, q in no_side)
            # Convert cents to dollars if needed
            if best_no_bid > 1.0:
                best_no_bid = best_no_bid / 100.0
        except (ValueError, TypeError):
            pass

    if best_no_ask and best_no_bid > 0:
        spread = best_no_ask - best_no_bid
        spread_pct = spread / max(best_no_ask, 0.01)
        spread_score = max(0, 1.0 - (spread_pct / 0.10))
        if spread_pct > 0.05:
            reasons.append("wide spread")
    else:
        spread_score = 0.3
        if not reasons:
            reasons.append("unclear spread")

    volume = float(market.get("volume_fp", 0) or market.get("volume", 0) or 0)
    days_to_expiry = market.get("_days_to_expiry", 30)
    vol_per_day = volume / max(days_to_expiry, 1)
    volume_score = min(1.0, vol_per_day / 50.0)
    if vol_per_day < 10:
        reasons.append("thin volume")

    # Handle both new and old price formats
    yes_last = float(market.get("last_price_dollars", 0) or market.get("last_price", 0) or 0)
    # Convert old cent format to dollar format if needed
    if yes_last > 1.0:
        yes_last = yes_last / 100.0
    
    if best_no_ask:
        price_gap = abs(best_no_ask - (1.0 - yes_last))
        stability_score = max(0, 1.0 - (price_gap / 0.15))  # 15¢ = $0.15
        if price_gap > 0.08:  # 8¢ = $0.08
            reasons.append("price gap")
    else:
        stability_score = 0.3

    score = (
        depth_ratio * 0.30
        + spread_score * 0.30
        + volume_score * 0.25
        + stability_score * 0.15
    )

    reason_str = ", ".join(reasons) if reasons else "ok"
    return round(score, 3), reason_str


# -----------------------------------------------------------------------
# SafeCompounder class
# -----------------------------------------------------------------------

class SafeCompounder:
    """
    Edge-based NO-side strategy integrated with repo's KalshiClient.

    Usage:
        compounder = SafeCompounder(client=kalshi_client, db_path="trading_system.db")
        await compounder.run(dry_run=False)
    """

    def __init__(
        self,
        client,  # KalshiClient instance
        db_path: str = "trading_system.db",
        dry_run: bool = True,
        min_no_ask: int = MIN_NO_ASK,
        min_edge: int = MIN_EDGE,
        max_position_pct: float = MAX_POSITION_PCT,
        use_kelly: bool = USE_KELLY,
        min_confidence: float = MIN_CONFIDENCE,
    ):
        self.client = client
        self.db_path = db_path
        self.dry_run = dry_run
        self.min_no_ask = min_no_ask
        self.min_edge = min_edge
        self.max_position_pct = max_position_pct
        self.use_kelly = use_kelly
        self.min_confidence = min_confidence

    async def run(self, dry_run: Optional[bool] = None) -> Dict:
        """
        Full scan: fetch → filter → orderbook check → place maker orders.
        Returns stats dict.
        """
        if dry_run is not None:
            self.dry_run = dry_run

        start = time.time()

        logger.info("=" * 70)
        logger.info("SAFE COMPOUNDER v5 — EDGE-BASED NO-SIDE")
        logger.info(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info(
            "Rules: NO only | ask > $%.2f | edge > $%.2f | max %.0f%%/position | maker orders",
            self.min_no_ask, self.min_edge, self.max_position_pct * 100,
        )
        logger.info("=" * 70)

        # Get portfolio state
        bal = await self.client.get_balance()
        portfolio = bal.get("portfolio_value", 0)
        cash = bal.get("balance", 0)

        print(f"\n💰 Cash: ${cash/100:.2f} | Portfolio: ${portfolio/100:.2f} | "
              f"Total: ${(cash+portfolio)/100:.2f}\n", flush=True)

        # Step 0: Cancel legacy YES orders
        print("🧹 Step 0: Cancel legacy YES orders...", flush=True)
        cancelled = await self._cancel_yes_orders()

        # Fetch crypto spot prices once per cycle (used by Step 2 filter)
        print("\n💱 Fetching crypto spot prices...", flush=True)
        crypto_prices = await fetch_crypto_prices()
        if crypto_prices:
            price_str = " | ".join(f"{k}=${v:,.0f}" for k, v in crypto_prices.items())
            print(f"  {price_str}", flush=True)
        else:
            print("  (unavailable — crypto markets will be skipped this cycle)", flush=True)

        # Step 1: Fetch all markets
        print("\n📡 Step 1: Fetching all active markets...", flush=True)
        markets = await self._fetch_all_markets()
        print(f"  Fetched {len(markets)} markets", flush=True)

        # Step 2: Filter NO candidates
        print("\n🔍 Step 2: Finding NO-side candidates (YES ≤ $0.10)...", flush=True)
        candidates = self._find_no_candidates(markets, crypto_prices)

        # Step 3: Orderbook + edge check
        print(f"\n📊 Step 3: Checking orderbooks for edge ≥ ${self.min_edge:.2f}...", flush=True)
        opportunities = await self._check_orderbook_and_price(candidates)

        # Display top opportunities
        sorted_opps = sorted(
            opportunities, key=lambda x: (-x["edge"], -x["annualized_roi"])
        )
        print(f"\n📋 Top Opportunities:", flush=True)
        for opp in sorted_opps[:20]:
            print(
                f"  NO ask:${opp['lowest_no_ask']:.2f} → our:${opp['our_price']:.2f} | "
                f"EV:${opp['true_no_prob']:.2f} edge:${opp['edge']:.2f} | "
                f"YES@${opp['yes_last']:.2f} | {opp['roi_pct']:.1f}% "
                f"({opp['annualized_roi']:.0f}%ann) | "
                f"{opp['days_to_expiry']}d | vol:{opp['volume']} | {opp['ticker']}",
                flush=True,
            )
            print(f"    {opp['title']}", flush=True)

        # Step 4: Place orders
        print(f"\n🚀 Step 4: Placing maker orders (ask - $0.01)...", flush=True)
        stats = await self._place_resting_orders(sorted_opps, portfolio, cash)

        elapsed = time.time() - start
        bal = await self.client.get_balance()

        print(f"\n{'='*70}", flush=True)
        print(f"📊 SAFE COMPOUNDER REPORT", flush=True)
        print(f"{'='*70}", flush=True)
        print(f"  Markets scanned:      {len(markets)}", flush=True)
        print(f"  NO candidates:        {len(candidates)}", flush=True)
        print(f"  With edge > ${self.min_edge:.2f}:      {len(opportunities)}", flush=True)
        print(f"  Orders placed:        {stats['placed']}", flush=True)
        print(f"  Instantly filled:     {stats['filled']}", flush=True)
        print(f"  Skipped (existing):   {stats['skipped_existing']}", flush=True)
        cap_label = "would-block" if EVENT_CAP_DRY_RUN else "blocked"
        print(
            f"  Event cap {cap_label:<12}{stats.get('event_cap_dry_warn', 0) if EVENT_CAP_DRY_RUN else stats.get('skipped_event_cap', 0)}",
            flush=True,
        )
        reserve_label = "would-block" if CASH_RESERVE_DRY_RUN else "blocked"
        print(
            f"  Cash reserve {reserve_label:<9}{stats.get('cash_reserve_dry_warn', 0) if CASH_RESERVE_DRY_RUN else stats.get('skipped_cash_reserve', 0)}",
            flush=True,
        )
        print(f"  Errors:               {stats['errors']}", flush=True)
        print(f"  Capital deployed:     ${stats['total_deployed']/100:.2f}", flush=True)
        print(f"  Potential profit:     ${stats['total_potential_profit']/100:.2f}", flush=True)
        print(f"  YES orders cancelled: {cancelled}", flush=True)
        print(f"  Cash:                 ${bal.get('balance', 0)/100:.2f}", flush=True)
        print(f"  Portfolio:            ${bal.get('portfolio_value', 0)/100:.2f}", flush=True)
        print(f"  Elapsed:              {elapsed:.0f}s", flush=True)
        print(f"{'='*70}\n", flush=True)

        return stats

    async def _fetch_all_markets(self) -> List[Dict]:
        """Fetch all active markets from Kalshi via events API.
        
        The /markets endpoint now only returns MVE (parlay) tickers (KXMVE*).
        Real individual markets live under events, so we fetch events with
        nested markets to get the actual tradeable universe.
        Falls back to /markets if events API fails.
        """
        all_markets = []
        seen_tickers = set()
        
        # Primary: fetch via events API (gets real individual markets)
        cursor = None
        page = 0
        try:
            while True:
                params = {"status": "open", "limit": 100, "with_nested_markets": "true"}
                if cursor:
                    params["cursor"] = cursor
                
                resp = await self.client._make_authenticated_request(
                    "GET", "/trade-api/v2/events", params=params
                )
                events = resp.get("events", [])
                if not events:
                    break
                
                for event in events:
                    for m in event.get("markets", []):
                        ticker = m.get("ticker", "")
                        if ticker and ticker not in seen_tickers:
                            seen_tickers.add(ticker)
                            # Carry event category into market for filtering
                            m["_event_category"] = event.get("category", "")
                            m["_event_title"] = event.get("title", "")
                            all_markets.append(m)
                
                cursor = resp.get("cursor")
                if not cursor:
                    break
                
                page += 1
                if page > 100:  # Safety cap
                    break
                
                await asyncio.sleep(0.1)
        except Exception as e:
            logger.warning("Events API failed, falling back to /markets: %s", e)
        
        # Fallback: also fetch /markets for any we missed (includes MVE)
        if len(all_markets) < 100:
            logger.info("Few markets from events (%d), also fetching /markets", len(all_markets))
            cursor = None
            page = 0
            while True:
                try:
                    params = {"status": "open", "limit": 200}
                    if cursor:
                        params["cursor"] = cursor
                    
                    resp = await self.client.get_markets(**params)
                    markets = resp.get("markets", [])
                    for m in markets:
                        ticker = m.get("ticker", "")
                        if ticker and ticker not in seen_tickers:
                            seen_tickers.add(ticker)
                            all_markets.append(m)
                    
                    cursor = resp.get("cursor")
                    if not cursor or not markets:
                        break
                    
                    page += 1
                    if page > 50:
                        break
                    
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logger.error("Error fetching markets page %d: %s", page, e)
                    break
        
        logger.info("Fetched %d unique markets (%d from events)", len(all_markets), len(seen_tickers))
        return all_markets

    def _find_no_candidates(
        self, markets: List[Dict], crypto_prices: Optional[Dict[str, float]] = None
    ) -> List[Dict]:
        """Filter markets to NO-side candidates."""
        candidates = []
        now = datetime.now(timezone.utc)
        crypto_prices = crypto_prices or {}

        for m in markets:
            ticker = m.get("ticker", "")
            if not in_whitelist(ticker):
                continue

            # Crypto gate: even within whitelisted KXBTC / KXETH, require
            # threshold to be >=CRYPTO_MARGIN from spot price.
            crypto_info = parse_crypto_market(ticker)
            if crypto_info is not None:
                symbol, direction, threshold = crypto_info
                if not is_crypto_threshold_safe(symbol, direction, threshold, crypto_prices):
                    continue

            title_lower = m.get("title", "").lower()
            if any(phrase in title_lower for phrase in SKIP_TITLE_PHRASES):
                continue

            if int(float(m.get("volume_fp", 0) or m.get("volume", 0) or 0)) < MIN_VOLUME:
                continue

            yes_last = float(m.get("last_price_dollars", 0) or m.get("last_price", 0) or 0)
            # Convert old cent format to dollar format if needed
            if yes_last > 1.0:
                yes_last = yes_last / 100.0
            if yes_last > 0.10:  # Only consider markets with YES ≤ $0.10 (tightened from $0.20)
                continue

            close_time = m.get("close_time", "")
            hours_to_expiry = 720
            if close_time:
                try:
                    expiry = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                    hours_to_expiry = max(0, (expiry - now).total_seconds() / 3600)
                except Exception:
                    pass

            if hours_to_expiry <= 0:
                continue

            true_no_prob = estimate_true_no_prob(yes_last, hours_to_expiry)

            candidates.append({
                **m,
                "_true_no_prob": true_no_prob,
                "_hours_to_expiry": round(hours_to_expiry, 1),
                "_days_to_expiry": round(hours_to_expiry / 24, 1),
            })

        logger.info("Found %d NO-side candidates (YES last <= $0.10)", len(candidates))
        
        # Sort by estimated edge potential: lowest YES price + highest volume + soonest expiry
        # Then cap to top 500 to keep orderbook checks under ~1 minute
        MAX_ORDERBOOK_CHECKS = 500
        if len(candidates) > MAX_ORDERBOOK_CHECKS:
            candidates.sort(key=lambda c: (
                -c["_true_no_prob"],  # Highest estimated NO probability first
                -float(c.get("volume_fp", 0) or c.get("volume", 0) or 0),  # Highest volume
                c["_hours_to_expiry"],  # Soonest expiry
            ))
            logger.info("Capping to top %d candidates (from %d) for orderbook checks",
                        MAX_ORDERBOOK_CHECKS, len(candidates))
            candidates = candidates[:MAX_ORDERBOOK_CHECKS]
        
        return candidates

    async def _check_orderbook_and_price(self, candidates: List[Dict]) -> List[Dict]:
        """Check orderbooks and find trades with sufficient edge."""
        opportunities = []

        for i, m in enumerate(candidates):
            ticker = m["ticker"]
            true_no_prob = m["_true_no_prob"]

            try:
                ob_resp = await self.client.get_orderbook(ticker, depth=10)
                # Handle both new and old orderbook formats
                ob = ob_resp.get("orderbook_fp", ob_resp.get("orderbook", {}))
                # No extra sleep — client already has 0.5s rate limiter
                if (i + 1) % 50 == 0:
                    logger.info("Orderbook progress: %d/%d checked", i + 1, len(candidates))
            except Exception as e:
                logger.debug("Orderbook fetch failed for %s: %s", ticker, e)
                continue

            conf_score, conf_reason = market_confidence_score(ticker, ob, m)
            if conf_score < self.min_confidence:
                logger.debug(
                    "Low confidence (%.2f) %s — %s", conf_score, ticker, conf_reason
                )
                continue

            # Handle both new and old orderbook formats
            yes_bids = ob.get("yes_dollars", ob.get("yes", []))
            no_bids = ob.get("no_dollars", ob.get("no", []))

            lowest_no_ask = None
            if yes_bids:
                try:
                    highest_yes_bid = max(float(b[0]) for b in yes_bids)
                    # Convert cents to dollars if needed
                    if highest_yes_bid > 1.0:
                        highest_yes_bid = highest_yes_bid / 100.0
                    lowest_no_ask = 1.0 - highest_yes_bid
                except (ValueError, TypeError):
                    pass

            best_no_bid = 0
            if no_bids:
                try:
                    best_no_bid = max(float(b[0]) for b in no_bids)
                    # Convert cents to dollars if needed
                    if best_no_bid > 1.0:
                        best_no_bid = best_no_bid / 100.0
                except (ValueError, TypeError):
                    pass

            if lowest_no_ask is None and best_no_bid > 0:
                lowest_no_ask = best_no_bid + 0.02  # 2¢ = $0.02

            if lowest_no_ask is None:
                continue

            if lowest_no_ask < self.min_no_ask:
                continue

            edge = true_no_prob - lowest_no_ask
            if edge < self.min_edge:
                continue

            our_price = lowest_no_ask - 0.01  # 1¢ = $0.01
            if our_price < self.min_no_ask:
                continue

            profit_per_contract = 1.0 - our_price
            roi_pct = profit_per_contract / our_price * 100
            days = m["_days_to_expiry"] if m["_days_to_expiry"] > 0 else 1
            annualized_roi = (profit_per_contract / our_price) * (365 / days) * 100

            yes_last_val = float(m.get("last_price_dollars", 0) or m.get("last_price", 0) or 0)
            # Convert cents to dollars if needed
            if yes_last_val > 1.0:
                yes_last_val = yes_last_val / 100.0
            
            opportunities.append({
                "ticker": ticker,
                "title": m.get("title", "")[:70],
                "side": "no",
                "yes_last": yes_last_val,
                "true_no_prob": true_no_prob,
                "lowest_no_ask": lowest_no_ask,
                "our_price": our_price,
                "edge": edge,
                "profit": profit_per_contract,
                "roi_pct": roi_pct,
                "annualized_roi": annualized_roi,
                "volume": int(float(m.get("volume_fp", 0) or m.get("volume", 0) or 0)),
                "days_to_expiry": m["_days_to_expiry"],
                "close_time": m.get("close_time", "")[:10],
                "best_no_bid": best_no_bid,
            })

            if (i + 1) % 25 == 0:
                logger.info(
                    "Checked %d/%d orderbooks, %d viable",
                    i + 1, len(candidates), len(opportunities),
                )

        logger.info(
            "%d opportunities with edge > $%.2f", len(opportunities), self.min_edge
        )
        return opportunities

    async def _place_resting_orders(
        self, opportunities: List[Dict], portfolio: int, cash: int
    ) -> Dict:
        """Place NO-side resting orders at lowest_ask - 1¢."""
        # --- Breaker #3: drawdown ---
        # Checked once per cycle, before any placement work or API calls
        # for positions/orders. If tripped, we short-circuit and return
        # an empty stats dict so the calling loop continues to scan
        # (cheap) but skips placement (expensive and risky).
        allowed, msg = check_drawdown_breaker(cash, portfolio)
        print(f"  {'✅' if allowed else '🛑'} {msg}", flush=True)
        if not allowed:
            return {
                "placed": 0,
                "skipped_existing": 0,
                "skipped_size": 0,
                "skipped_event_cap": 0,
                "event_cap_dry_warn": 0,
                "skipped_cash_reserve": 0,
                "cash_reserve_dry_warn": 0,
                "skipped_drawdown": 1,
                "filled": 0,
                "errors": 0,
                "total_potential_profit": 0,
                "total_deployed": 0,
            }

        # Get existing positions and orders
        try:
            positions_resp = await self.client.get_positions()
            positions = positions_resp.get("market_positions", [])
            pos_tickers = {
                p["ticker"] for p in positions if abs(_pos_count(p)) > 0
            }
        except Exception:
            positions = []
            pos_tickers = set()

        try:
            orders_resp = await self.client.get_orders(status="resting")
            existing_orders = orders_resp.get("orders", [])
            ord_tickers = {o["ticker"] for o in existing_orders}
        except Exception:
            existing_orders = []
            ord_tickers = set()

        # Build per-event exposure map (in cents) from current open positions.
        # Resting orders not yet filled contribute their committed cost too.
        event_exposure_cents: Dict[str, int] = defaultdict(int)
        for p in positions:
            if abs(_pos_count(p)) == 0:
                continue
            event_exposure_cents[_event_of(p["ticker"])] += _pos_cost_cents(p)
        for o in existing_orders:
            event_exposure_cents[_event_of(o["ticker"])] += _order_cost_cents(o)

        # Track in-cycle additions so multiple orders to the same event stack.
        cycle_event_adds: Dict[str, int] = defaultdict(int)
        # Cash committed during this cycle. Kalshi reserves cash on maker-order
        # placement (not just on fill), so each placed order reduces the
        # effective cash available for subsequent orders in the same cycle.
        cycle_cash_spent_cents = 0

        stats = {
            "placed": 0,
            "skipped_existing": 0,
            "skipped_size": 0,
            "skipped_event_cap": 0,
            "event_cap_dry_warn": 0,
            "skipped_cash_reserve": 0,
            "cash_reserve_dry_warn": 0,
            "skipped_drawdown": 0,  # 0 here: breaker not tripped this cycle
            "filled": 0,
            "errors": 0,
            "total_potential_profit": 0,
            "total_deployed": 0,
        }

        total = portfolio + cash
        cap_mode = "DRY-LOG ONLY" if EVENT_CAP_DRY_RUN else "ENFORCED"
        reserve_mode = "DRY-LOG ONLY" if CASH_RESERVE_DRY_RUN else "ENFORCED"
        min_cash_cents = int(total * CASH_RESERVE_PCT)
        pct_cap_dollars = total * self.max_position_pct / 100
        if MAX_POSITION_DOLLARS is None:
            position_cap_binding = pct_cap_dollars
            position_cap_source = f"{self.max_position_pct*100:.0f}% × bankroll"
            abs_cap_label = "none"
        else:
            position_cap_binding = min(pct_cap_dollars, MAX_POSITION_DOLLARS)
            position_cap_source = (
                f"${MAX_POSITION_DOLLARS:.2f} absolute"
                if MAX_POSITION_DOLLARS < pct_cap_dollars
                else f"{self.max_position_pct*100:.0f}% × bankroll"
            )
            abs_cap_label = f"${MAX_POSITION_DOLLARS:.2f}"
        print(
            f"\n{'='*70}\nPLACING MAKER ORDERS — Portfolio: ${portfolio/100:.2f} | "
            f"Cash: ${cash/100:.2f} | {'DRY RUN' if self.dry_run else 'LIVE'}\n"
            f"Max per position: ${position_cap_binding:.2f} "
            f"(binding: {position_cap_source}; other: "
            f"{abs_cap_label} abs / ${pct_cap_dollars:.2f} pct)\n"
            f"Per-event cap: {EVENT_CAP_PCT*100:.0f}% of bankroll ({cap_mode})\n"
            f"Cash reserve: {CASH_RESERVE_PCT*100:.0f}% of bankroll = ${min_cash_cents/100:.2f} ({reserve_mode})\n"
            f"{'='*70}\n",
            flush=True,
        )

        for opp in opportunities:
            ticker = opp["ticker"]

            if ticker in pos_tickers or ticker in ord_tickers:
                stats["skipped_existing"] += 1
                continue

            contracts = self._calculate_position_size(opp, portfolio, cash)
            if contracts < 1:
                stats["skipped_size"] += 1
                continue

            price = opp["our_price"]
            cost = contracts * price * 100  # Convert dollars to cents for cost calculation
            profit = contracts * opp["profit"] * 100  # Convert dollars to cents for profit calculation

            # --- Breaker #1: per-event concentration cap ---
            event = _event_of(ticker)
            existing_exp = event_exposure_cents[event] + cycle_event_adds[event]
            projected_exp = existing_exp + cost
            cap_cents = total * EVENT_CAP_PCT
            if projected_exp > cap_cents:
                msg = (
                    f"  🟡 EVENT-CAP {'WOULD-BLOCK' if EVENT_CAP_DRY_RUN else 'BLOCKED'}: "
                    f"{event} → ${projected_exp/100:.2f} "
                    f"({projected_exp/total*100:.1f}% of bankroll, cap {EVENT_CAP_PCT*100:.0f}%) | "
                    f"this order: NO x{contracts} @ ${price:.2f} on {ticker}"
                )
                print(msg, flush=True)
                if EVENT_CAP_DRY_RUN:
                    stats["event_cap_dry_warn"] += 1
                    # Fall through and place the order anyway — observation mode.
                else:
                    stats["skipped_event_cap"] += 1
                    continue

            # --- Breaker #2: cash reserve ---
            # Projected cash if this order is placed (Kalshi reserves cash
            # on placement, not on fill). Block if it would push effective
            # cash below the configured reserve.
            effective_cash_after = cash - cycle_cash_spent_cents - cost
            if effective_cash_after < min_cash_cents:
                msg = (
                    f"  🟡 CASH-RESERVE {'WOULD-BLOCK' if CASH_RESERVE_DRY_RUN else 'BLOCKED'}: "
                    f"effective cash ${effective_cash_after/100:.2f} < reserve "
                    f"${min_cash_cents/100:.2f} ({CASH_RESERVE_PCT*100:.0f}% of bankroll) | "
                    f"this order: NO x{contracts} @ ${price:.2f} on {ticker} (cost ${cost/100:.2f})"
                )
                print(msg, flush=True)
                if CASH_RESERVE_DRY_RUN:
                    stats["cash_reserve_dry_warn"] += 1
                    # Fall through and place anyway — observation mode.
                else:
                    stats["skipped_cash_reserve"] += 1
                    continue

            cycle_event_adds[event] += cost
            cycle_cash_spent_cents += cost

            if self.dry_run:
                kelly_info = ""
                if self.use_kelly:
                    true_prob = opp["true_no_prob"]  # Already in 0-1 format
                    odds = (1.0 - price) / price  # Dollar format
                    kf = kelly_fraction(true_prob, odds)
                    kelly_info = f" kelly:{kf:.1%}"
                print(
                    f"  🏷️ [DRY] NO x{contracts} @ ${price:.2f} | "
                    f"ask:${opp['lowest_no_ask']:.2f} EV:${opp['true_no_prob']:.2f} "
                    f"edge:${opp['edge']:.2f} | "
                    f"+${profit/100:.2f} ({opp['roi_pct']:.1f}% / {opp['annualized_roi']:.0f}%ann) | "
                    f"{opp['days_to_expiry']}d{kelly_info}",
                    flush=True,
                )
                print(f"    {opp['ticker']} — {opp['title']}", flush=True)
                stats["placed"] += 1
                stats["total_potential_profit"] += profit
                stats["total_deployed"] += cost
                continue

            try:
                # Convert dollar price to cents for API call
                price_cents = int(price * 100)
                client_order_id = str(uuid.uuid4())
                r = await self.client.place_order(
                    ticker=ticker,
                    client_order_id=client_order_id,
                    side="no",
                    action="buy",
                    count=contracts,
                    type_="limit",
                    no_price=price_cents,
                )
                order = r.get("order", {})
                status = order.get("status", "?")
                filled = order.get("fill_count", 0)

                if filled > 0:
                    stats["filled"] += filled
                    print(
                        f"  🎯 FILLED NO x{filled}/{contracts} @ ${price:.2f} | "
                        f"edge:${opp['edge']:.2f} +${filled * opp['profit']/100:.2f} | {ticker}",
                        flush=True,
                    )
                else:
                    print(
                        f"  ✅ NO x{contracts} @ ${price:.2f} | {status} | "
                        f"edge:${opp['edge']:.2f} {opp['roi_pct']:.1f}% | {ticker}",
                        flush=True,
                    )

                stats["placed"] += 1
                stats["total_potential_profit"] += profit
                stats["total_deployed"] += cost
                ord_tickers.add(ticker)
                await asyncio.sleep(0.2)

            except Exception as e:
                print(f"  ❌ {ticker}: {e}", flush=True)
                stats["errors"] += 1
                await asyncio.sleep(0.3)

        return stats

    def _calculate_position_size(self, opp: Dict, portfolio: int, cash: int) -> int:
        """Size each position using Kelly or fixed fraction, capped by both
        the percentage-of-bankroll limit and the absolute dollar limit."""
        total = portfolio + cash
        pct_cap_cents = int(total * self.max_position_pct)
        if MAX_POSITION_DOLLARS is None:
            max_position_value = pct_cap_cents
        else:
            abs_cap_cents = int(MAX_POSITION_DOLLARS * 100)
            max_position_value = min(pct_cap_cents, abs_cap_cents)
        price = opp["our_price"]  # Already in dollar format

        if self.use_kelly:
            true_prob = opp["true_no_prob"]  # Already in 0-1 format
            odds = (1.0 - price) / price  # Dollar format
            kf = kelly_fraction(true_prob, odds)
            half_kelly_f = kf * 0.5
            kelly_position = int(total * half_kelly_f)
            position_value = min(kelly_position, max_position_value)
        else:
            position_value = max_position_value

        # Convert price to cents for position calculation
        price_cents = int(price * 100)
        contracts = max(1, position_value // price_cents)
        contracts = min(contracts, 200)
        return contracts

    async def _cancel_yes_orders(self) -> int:
        """Cancel any resting YES-side orders (legacy)."""
        try:
            orders_resp = await self.client.get_orders(status="resting")
            orders = orders_resp.get("orders", [])
            yes_orders = [o for o in orders if o.get("side") == "yes"]
            cancelled = 0
            for o in yes_orders:
                try:
                    await self.client.cancel_order(o["order_id"])
                    yes_price = o.get('yes_price', 0)
                    if isinstance(yes_price, (int, float)) and yes_price > 0:
                        # Convert cents to dollars if needed for display
                        if yes_price > 1.0:
                            price_display = f"${yes_price/100:.2f}"
                        else:
                            price_display = f"${yes_price:.2f}"
                    else:
                        price_display = "?"
                    print(
                        f"  🗑️ Cancelled YES: {o['ticker']} @ {price_display}",
                        flush=True,
                    )
                    cancelled += 1
                    await asyncio.sleep(0.15)
                except Exception as e:
                    logger.warning("Cancel failed %s: %s", o["ticker"], e)
            if not yes_orders:
                print("  No legacy YES orders.", flush=True)
            return cancelled
        except Exception as e:
            logger.error("Error cancelling YES orders: %s", e)
            return 0

    async def check_fills(self) -> None:
        """Check recent fills and resting orders."""
        bal = await self.client.get_balance()
        portfolio = bal.get("portfolio_value", 0)
        cash = bal.get("balance", 0)
        print(
            f"💰 Cash: ${cash/100:.2f} | Portfolio: ${portfolio/100:.2f} | "
            f"Total: ${(cash+portfolio)/100:.2f}",
            flush=True,
        )

        try:
            orders_resp = await self.client.get_orders(status="resting")
            resting = orders_resp.get("orders", [])
            no_resting = [o for o in resting if o.get("side") == "no"]
            yes_resting = [o for o in resting if o.get("side") == "yes"]
            print(
                f"📋 Resting: {len(no_resting)} NO, {len(yes_resting)} YES",
                flush=True,
            )
        except Exception:
            pass

        try:
            fills_resp = await self.client.get_fills(limit=20)
            fill_list = fills_resp.get("fills", [])
            print(f"\n📊 Last 20 fills:", flush=True)
            for f in fill_list:
                ticker = f.get("ticker", "")
                side = f.get("side", "")
                count = f.get("count", 0)
                price = f.get("yes_price", f.get("no_price", 0))
                created = f.get("created_time", "")[:16]
                # Convert cents to dollars if needed for display
                if isinstance(price, (int, float)) and price > 1.0:
                    price_display = f"${price/100:.2f}"
                else:
                    price_display = f"${price:.2f}" if isinstance(price, (int, float)) else f"{price}¢"
                print(f"  {created} | {side} x{count} @ {price_display} | {ticker}", flush=True)
        except Exception:
            pass
