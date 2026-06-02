"""
Backtest the 4 proposed circuit breakers against the last N days of fills.

Read-only — does not modify any live bot state. Pulls fills + per-market
settlement results from Kalshi, replays them chronologically, and for each
fill evaluates whether each breaker would have blocked it. Computes the
counterfactual P&L delta (forgone profit + avoided loss) per breaker.

Usage:
    python scripts/backtest_breakers.py --days 23 --starting-bankroll 91.00

Assumptions / known limitations:
  - Bankroll is reconstructed by walking forward from --starting-bankroll.
    Deposits / withdrawals during the window are NOT modeled. If you added
    money mid-window, pass --starting-bankroll for the actual day-0 cash
    and the trajectory will diverge from reality after the deposit.
  - "Bot-eligible fill" = side==no AND action==buy AND no_price >= 90¢.
    Other fills (manual YES bets, manual cheap NO buys, sells) flow
    through the bankroll model but are NOT subject to breakers.
  - portfolio_value at any moment = sum(open_count * 100¢) for NO positions
    (face value). The live bot's `portfolio_value` from Kalshi uses last
    price, which for $0.90+ NO bets is ~face value, so close enough.
  - Per-event grouping uses the ticker up to the last '-' segment, e.g.
    KXWTI-26MAY28H17-T84.99 → event "KXWTI-26MAY28H17".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Make `src` importable when running from repo root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from src.clients.kalshi_client import KalshiClient  # noqa: E402

CACHE_DIR = ROOT / "data" / "backtest_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Breaker thresholds (matching circuit-breakers-plan memory)
EVENT_CAP_PCT = 0.15        # per-event concentration cap
DAILY_LOSS_PCT = 0.05       # daily loss circuit breaker
DRAWDOWN_HALT_PCT = 0.15    # drawdown halt
CASH_RESERVE_PCT = 0.10     # cash reserve floor


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

async def fetch_all_fills(
    client: KalshiClient, min_ts: int, max_ts: int
) -> List[Dict]:
    """Paginated fills fetch using the underlying authenticated request method
    (the wrapper doesn't expose min_ts / cursor)."""
    fills: List[Dict] = []
    cursor: Optional[str] = None
    page = 0
    while True:
        params: Dict = {"limit": 1000, "min_ts": min_ts, "max_ts": max_ts}
        if cursor:
            params["cursor"] = cursor
        resp = await client._make_authenticated_request(
            "GET", "/trade-api/v2/portfolio/fills", params=params
        )
        batch = resp.get("fills", [])
        fills.extend(batch)
        page += 1
        cursor = resp.get("cursor") or None
        print(f"  page {page}: +{len(batch)} fills (total {len(fills)})")
        if not cursor or not batch:
            break
    return fills


async def fetch_settlements(
    client: KalshiClient, tickers: List[str]
) -> Dict[str, Dict]:
    """Fetch each unique market's data (for settlement result + time). Cached."""
    cache_path = CACHE_DIR / "markets.json"
    cache: Dict[str, Dict] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())

    # Re-fetch anything not finalized yet, OR cached without settlement_ts
    # (older cache entries didn't store it).
    missing = [
        t for t in tickers
        if (
            t not in cache
            or cache[t].get("status") != "finalized"
            or "settlement_ts" not in cache[t]
        )
    ]
    print(f"  {len(tickers) - len(missing)} cached, {len(missing)} to fetch")
    for i, ticker in enumerate(missing):
        try:
            resp = await client.get_market(ticker)
            m = resp.get("market", {}) if isinstance(resp, dict) else {}
            cache[ticker] = {
                "ticker": ticker,
                "status": m.get("status"),
                "result": m.get("result") or None,
                "settlement_ts": m.get("settlement_ts"),
                "title": m.get("title", ""),
            }
            if (i + 1) % 25 == 0:
                print(f"  fetched {i + 1}/{len(missing)}")
                cache_path.write_text(json.dumps(cache, indent=2))
        except Exception as e:
            print(f"  WARN {ticker}: {e}")
            cache[ticker] = {"ticker": ticker, "status": "error", "result": None}
    cache_path.write_text(json.dumps(cache, indent=2))
    return cache


# ---------------------------------------------------------------------------
# Timeline reconstruction
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """Open exposure on one market (one side only — we don't net YES vs NO).
    Counts can be fractional; we keep cents as float internally and round only
    at display time."""
    ticker: str
    side: str
    count: float = 0.0
    cost_cents: float = 0.0  # total cash spent on this position

    @property
    def face_value_cents(self) -> float:
        return self.count * 100.0


@dataclass
class State:
    cash_cents: float
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl_cents: float = 0.0  # cumulative

    @property
    def portfolio_value_cents(self) -> float:
        # Use face value (count * 100¢) — close to "last price" for deep NO bets.
        return sum(p.face_value_cents for p in self.positions.values())

    @property
    def bankroll_cents(self) -> float:
        return self.cash_cents + self.portfolio_value_cents

    def event_exposure_cents(self, event: str) -> float:
        return sum(
            p.cost_cents for tkr, p in self.positions.items() if event_of(tkr) == event
        )


def event_of(ticker: str) -> str:
    """KXWTI-26MAY28H17-T84.99 → KXWTI-26MAY28H17"""
    return ticker.rsplit("-", 1)[0]


def parse_ts(s: str) -> datetime:
    # Kalshi returns ISO 8601 with trailing Z
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _f(v) -> float:
    """Parse a Kalshi dollar-string field to float (e.g. '0.9500' -> 0.95).
    Returns 0.0 on missing / unparseable."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fill_count(fill: Dict) -> float:
    return _f(fill.get("count_fp", fill.get("count", 0)))


def fill_price_dollars(fill: Dict) -> float:
    side = fill.get("side", fill.get("outcome_side", ""))
    if side == "no":
        return _f(fill.get("no_price_dollars", fill.get("no_price", 0)))
    return _f(fill.get("yes_price_dollars", fill.get("yes_price", 0)))


def fill_cost_cents(fill: Dict) -> float:
    return fill_count(fill) * fill_price_dollars(fill) * 100.0


def fill_fee_cents(fill: Dict) -> float:
    return _f(fill.get("fee_cost", 0)) * 100.0


def fill_side(fill: Dict) -> str:
    return fill.get("side") or fill.get("outcome_side") or ""


def is_bot_eligible(fill: Dict) -> bool:
    """A fill that the safe_compounder would have placed (and thus that
    breakers would gate). Heuristic: NO buy at >=$0.90."""
    return (
        fill_side(fill) == "no"
        and fill.get("action", "buy") == "buy"
        and _f(fill.get("no_price_dollars", fill.get("no_price", 0))) >= 0.90
    )


# ---------------------------------------------------------------------------
# Breaker simulation
# ---------------------------------------------------------------------------

@dataclass
class BreakerResult:
    name: str
    blocked: List[Dict] = field(default_factory=list)  # full fill records
    max_observed: float = 0.0
    notes: List[str] = field(default_factory=list)

    def block(self, fill: Dict):
        self.blocked.append(fill)


def simulate(
    fills: List[Dict],
    settlements: Dict[str, Dict],
    starting_bankroll_cents: int,
) -> Tuple[State, Dict[str, BreakerResult], List[Dict]]:
    """Walk chronologically through fills + settlements.

    Returns:
        final state, breaker results, list of (event, settlement_time) tuples
        we observed.
    """
    # Build event stream: fills + settlements interleaved by time.
    last_fill_time_per_market: Dict[str, datetime] = {}
    for f in fills:
        t = parse_ts(f["created_time"])
        prev = last_fill_time_per_market.get(f["ticker"])
        if prev is None or t > prev:
            last_fill_time_per_market[f["ticker"]] = t

    events: List[Tuple[datetime, str, Dict]] = []
    for f in fills:
        events.append((parse_ts(f["created_time"]), "fill", f))
    for ticker, mkt in settlements.items():
        if mkt.get("status") != "finalized":
            continue
        # Prefer real settlement_ts from the market; fall back to last fill + 1h
        st = mkt.get("settlement_ts")
        if st:
            settle_t = parse_ts(st)
        elif ticker in last_fill_time_per_market:
            settle_t = last_fill_time_per_market[ticker] + timedelta(hours=1)
        else:
            continue
        events.append((settle_t, "settle", {"ticker": ticker, **mkt}))
    events.sort(key=lambda x: x[0])

    state = State(cash_cents=starting_bankroll_cents)
    breakers = {
        "event_cap": BreakerResult("Per-event concentration cap (15%)"),
        "daily_loss": BreakerResult("Daily loss circuit breaker (5%)"),
        "drawdown": BreakerResult("Drawdown halt (15%)"),
        "cash_reserve": BreakerResult("Cash reserve floor (10%)"),
    }

    peak_bankroll = state.bankroll_cents or starting_bankroll_cents
    daily_realized: Dict[str, float] = defaultdict(float)
    daily_starting_bankroll: Dict[str, float] = {}

    for t, kind, payload in events:
        date_str = t.date().isoformat()
        if date_str not in daily_starting_bankroll:
            daily_starting_bankroll[date_str] = state.bankroll_cents or starting_bankroll_cents

        if kind == "fill":
            fill = payload
            ticker = fill["ticker"]
            event = event_of(ticker)
            cost = fill_cost_cents(fill)
            fee = fill_fee_cents(fill)
            side = fill_side(fill)
            count = fill_count(fill)

            # --- Evaluate breakers BEFORE applying the fill ---
            if is_bot_eligible(fill):
                bankroll = state.bankroll_cents or starting_bankroll_cents

                # #1 per-event cap
                exposure_after = state.event_exposure_cents(event) + cost
                if exposure_after > bankroll * EVENT_CAP_PCT:
                    breakers["event_cap"].block(fill)
                pct = exposure_after / bankroll if bankroll else 0
                if pct > breakers["event_cap"].max_observed:
                    breakers["event_cap"].max_observed = pct

                # #2 daily loss
                day_pnl = daily_realized[date_str]
                day_start = daily_starting_bankroll[date_str]
                if day_pnl < -day_start * DAILY_LOSS_PCT:
                    breakers["daily_loss"].block(fill)
                drawdown_today = -day_pnl / day_start if day_start else 0
                if drawdown_today > breakers["daily_loss"].max_observed:
                    breakers["daily_loss"].max_observed = drawdown_today

                # #3 drawdown halt
                dd = (peak_bankroll - bankroll) / peak_bankroll if peak_bankroll else 0
                if dd > DRAWDOWN_HALT_PCT:
                    breakers["drawdown"].block(fill)
                if dd > breakers["drawdown"].max_observed:
                    breakers["drawdown"].max_observed = dd

                # #4 cash reserve
                cash_after = state.cash_cents - cost
                if cash_after < bankroll * CASH_RESERVE_PCT:
                    breakers["cash_reserve"].block(fill)
                ratio = state.cash_cents / bankroll if bankroll else 1
                # track *min* observed cash ratio (re-using max field, negated logic in report)
                if ratio < breakers["cash_reserve"].max_observed or breakers["cash_reserve"].max_observed == 0:
                    breakers["cash_reserve"].max_observed = ratio

            # --- Apply fill to state ---
            action = fill.get("action", "buy")
            pos = state.positions.get(ticker)
            if action == "sell":
                # Sell: cash credited, position reduced, realize partial P&L.
                state.cash_cents += (cost - fee)
                if pos is not None and pos.count > 0:
                    # Proportional cost basis on the chunk being sold.
                    portion = min(count, pos.count) / pos.count if pos.count else 0
                    basis_sold = pos.cost_cents * portion
                    pos.count -= min(count, pos.count)
                    pos.cost_cents -= basis_sold
                    realized = cost - basis_sold - fee
                    state.realized_pnl_cents += realized
                    daily_realized[date_str] += realized
                    if pos.count <= 0.0001:
                        state.positions.pop(ticker, None)
            else:
                # Buy: cash debited, position grows.
                state.cash_cents -= (cost + fee)
                if pos is None:
                    pos = Position(ticker=ticker, side=side)
                    state.positions[ticker] = pos
                if pos.side != side:
                    # Opposite-side fill on the same market — rare. Overwrite.
                    pos.side = side
                pos.count += count
                pos.cost_cents += cost

            if state.bankroll_cents > peak_bankroll:
                peak_bankroll = state.bankroll_cents

        elif kind == "settle":
            ticker = payload["ticker"]
            result = payload.get("result")  # "yes" / "no" / None
            pos = state.positions.pop(ticker, None)
            if pos is None or not result:
                continue
            won = (pos.side == result)
            payout = pos.count * 100.0 if won else 0.0
            pnl = payout - pos.cost_cents
            state.cash_cents += payout
            state.realized_pnl_cents += pnl
            daily_realized[date_str] += pnl
            if state.bankroll_cents > peak_bankroll:
                peak_bankroll = state.bankroll_cents

    return state, breakers, events


# ---------------------------------------------------------------------------
# Counterfactual P&L
# ---------------------------------------------------------------------------

def counterfactual_pnl_cents(
    blocked_fills: List[Dict], settlements: Dict[str, Dict]
) -> Tuple[float, float, float]:
    """Returns (forgone_profit, avoided_loss, net_delta) in cents.
    Positive net_delta = breaker would have HURT us (we missed gains).
    Negative net_delta = breaker would have HELPED us (we avoided losses).
    """
    forgone = 0.0
    avoided = 0.0
    for f in blocked_fills:
        ticker = f["ticker"]
        mkt = settlements.get(ticker, {})
        if mkt.get("status") != "finalized":
            continue
        cost = fill_cost_cents(f)
        count = fill_count(f)
        side = fill_side(f)
        won = (side == mkt.get("result"))
        pnl = (count * 100.0 - cost) if won else (-cost)
        if pnl > 0:
            forgone += pnl
        else:
            avoided += -pnl
    return forgone, avoided, forgone - avoided


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def fmt_dollars(cents: int | float) -> str:
    return f"${cents / 100:,.2f}"


def print_report(
    state: State,
    breakers: Dict[str, BreakerResult],
    settlements: Dict[str, Dict],
    fills: List[Dict],
    starting_bankroll_cents: int,
    days: int,
):
    print("\n" + "=" * 72)
    print(f"BACKTEST REPORT — last {days} days")
    print("=" * 72)
    bot_fills = [f for f in fills if is_bot_eligible(f)]
    other_fills = [f for f in fills if not is_bot_eligible(f)]
    print(f"\nTotal fills processed: {len(fills)}")
    print(f"  Bot-eligible (NO buy ≥ 90¢): {len(bot_fills)}")
    print(f"  Other (manual / sells / cheap NO / YES): {len(other_fills)}")
    print(f"\nStarting bankroll: {fmt_dollars(starting_bankroll_cents)}")
    print(f"Ending bankroll:   {fmt_dollars(state.bankroll_cents)}")
    print(f"Realized P&L:      {fmt_dollars(state.realized_pnl_cents)}")

    print("\n" + "-" * 72)
    print("PER-BREAKER IMPACT")
    print("-" * 72)
    print(
        f"{'Breaker':<40} {'Blocked':>8} {'Forgone':>10} "
        f"{'Avoided':>10} {'Net Δ':>10}"
    )
    print("-" * 72)
    for key, br in breakers.items():
        forgone, avoided, net = counterfactual_pnl_cents(br.blocked, settlements)
        print(
            f"{br.name:<40} {len(br.blocked):>8} "
            f"{fmt_dollars(forgone):>10} {fmt_dollars(avoided):>10} "
            f"{fmt_dollars(net):>10}"
        )

    print("\nLegend: Forgone = profit we'd have missed | Avoided = losses we'd have skipped")
    print("        Net Δ > 0 means the breaker would have HURT returns.")
    print("        Net Δ < 0 means the breaker would have HELPED returns.\n")

    print("-" * 72)
    print("MAX OBSERVED (peak stress on each breaker, ignoring whether it tripped)")
    print("-" * 72)
    print(f"  Highest single-event exposure:  {breakers['event_cap'].max_observed * 100:.1f}% of bankroll  (threshold: {EVENT_CAP_PCT * 100:.0f}%)")
    print(f"  Worst single-day drawdown:      {breakers['daily_loss'].max_observed * 100:.1f}% of day-start  (threshold: {DAILY_LOSS_PCT * 100:.0f}%)")
    print(f"  Worst peak-to-trough drawdown:  {breakers['drawdown'].max_observed * 100:.1f}%  (threshold: {DRAWDOWN_HALT_PCT * 100:.0f}%)")
    print(f"  Lowest cash ratio observed:     {breakers['cash_reserve'].max_observed * 100:.1f}% of bankroll  (floor: {CASH_RESERVE_PCT * 100:.0f}%)")

    # Top-blocked events for breaker #1 (since user expected it to be the worst offender)
    if breakers["event_cap"].blocked:
        evt_counts: Dict[str, int] = defaultdict(int)
        evt_cost: Dict[str, int] = defaultdict(int)
        for f in breakers["event_cap"].blocked:
            ev = event_of(f["ticker"])
            evt_counts[ev] += 1
            evt_cost[ev] += fill_cost_cents(f)
        print("\n  Top 10 events that breaker #1 would have throttled:")
        ranked = sorted(evt_counts.items(), key=lambda x: -x[1])[:10]
        for ev, n in ranked:
            print(f"    {ev:<35} {n:>3} fills blocked, {fmt_dollars(evt_cost[ev])} of cost")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=23)
    ap.add_argument(
        "--starting-bankroll", type=float, default=91.00,
        help="Estimated bankroll (cash + open positions) at day 0 in dollars",
    )
    ap.add_argument("--refresh-fills", action="store_true",
                    help="Re-fetch fills even if cache exists")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    min_dt = now - timedelta(days=args.days)
    min_ts = int(min_dt.timestamp())
    max_ts = int(now.timestamp())

    client = KalshiClient()

    # Fetch fills (with simple cache for reruns)
    fills_cache = CACHE_DIR / f"fills_{args.days}d.json"
    if fills_cache.exists() and not args.refresh_fills:
        print(f"Loading cached fills from {fills_cache}")
        fills = json.loads(fills_cache.read_text())
    else:
        print(f"Fetching fills from {min_dt.isoformat()} to {now.isoformat()}")
        fills = await fetch_all_fills(client, min_ts, max_ts)
        fills_cache.write_text(json.dumps(fills, indent=2))
        print(f"Saved {len(fills)} fills to {fills_cache}")

    # Sort defensively
    fills.sort(key=lambda f: f["created_time"])

    # Fetch settlements for every unique ticker
    tickers = sorted({f["ticker"] for f in fills})
    print(f"\nFetching settlements for {len(tickers)} unique markets")
    settlements = await fetch_settlements(client, tickers)
    n_settled = sum(1 for m in settlements.values() if m.get("status") == "finalized")
    print(f"  {n_settled}/{len(tickers)} finalized")

    # Simulate
    starting_cents = int(round(args.starting_bankroll * 100))
    print(f"\nSimulating from ${args.starting_bankroll:.2f} starting bankroll...")
    state, breakers, _ = simulate(fills, settlements, starting_cents)

    # Report
    print_report(state, breakers, settlements, fills, starting_cents, args.days)

    await client.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
