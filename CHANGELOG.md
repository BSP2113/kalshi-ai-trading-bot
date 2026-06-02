# Changelog

All notable changes to the Kalshi AI Trading Bot project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed (2026-06-02)
- **Restored the `MAX_POSITION_DOLLARS = $25` absolute cap in Safe Compounder.** Reverts the 2026-06-01 change that set it to `None`. Position size is again `min(MAX_POSITION_PCT × bankroll, $25)`; at the ~$1,400 bankroll the $25 cap binds (vs. 3% = ~$42), holding per-position tail risk to ~$25. `MAX_POSITION_PCT` stays 3%. The header log shows `$25.00 abs` as the binding cap again.

### Added (2026-06-01)
- **Cash reserve breaker (breaker #2)** in Safe Compounder, **enforced** (`CASH_RESERVE_PCT = 10%`, `CASH_RESERVE_DRY_RUN = False`). Before placing each order, projects post-order cash (`cash − cycle_cash_spent − cost`) and blocks if it would fall below 10% of total bankroll. Closes the gap that produced 30+ HTTP-400 `insufficient_balance` rejections per cycle from 26MAY29 onward — at that point cash had drained to $88.92 against a $1,372 bankroll (6.5% reserve, well below the new 10% floor). Pairs with the in-cycle counter so multiple orders in one placement loop stack correctly. Stats block now reports `Cash reserve blocked: N`. Toggle `CASH_RESERVE_DRY_RUN = True` for log-only observation mode (matches the `EVENT_CAP_DRY_RUN` pattern).
- **Drawdown breaker (breaker #3)** in Safe Compounder, **enforced** (`DRAWDOWN_THRESHOLD = 10%`). Halts new order placement when total equity (`cash + portfolio_value`) falls 10% below the all-time peak. Peak is persisted across restarts in `data/safe_compounder_breakers.json` (path configurable via `BREAKERS_STATE_PATH`) and **seeded from current equity on first run** — no historical reconstruction. Peak ratchets up only; never decays. **Trip is sticky**: once tripped, the bot stays halted until the state file is deleted, by design — when this fires the strategy needs human review, not a self-resume that walks back into the same trade. Checked once per cycle before any placement work, so the bot still scans the orderbook (cheap) but skips placement (expensive and risky). Targets slow-bleed accumulation, which daily-loss limits miss — the actual failure mode observed in 666 historical settlements: −$156 net realized across many small bets, where the largest single-day drawdown was the 26MAY31 BNB cluster (−$138, ≈10% of bankroll). Header log line on every cycle: `✅ Drawdown breaker OK: equity $X, peak $Y (-Z%)` or `🛑 Drawdown breaker TRIPPED…`.

### Changed (2026-06-01)
- **Removed the `MAX_POSITION_DOLLARS` absolute cap (set to `None`) in Safe Compounder; 3% of bankroll is now the sole position-sizing cap.** Supersedes the `$25` absolute cap added earlier today. Rationale: with trading already restricted to the proven `SAFE_SERIES` whitelist, the per-series risk concentration the dollar cap guarded against is handled at the candidate-filter level, so position size should scale with equity rather than stay pinned at a fixed dollar amount. At the ~$1,400 bankroll the $25 cap had held effective sizing to ~1.8% (vs. the intended 3% = ~$42); removing it lets the full 3% bind at every bankroll level. `MAX_POSITION_PCT` itself is unchanged at 3%. `_calculate_position_size` and the placement header log now treat `MAX_POSITION_DOLLARS is None` as "no absolute cap" (header shows `none abs`). Reintroduce a dollar cap only if a single series shows it needs one.
- **Added `MAX_POSITION_DOLLARS = $25` absolute hard cap per position in Safe Compounder.** Position size is now `min(MAX_POSITION_PCT × bankroll, MAX_POSITION_DOLLARS)` — the percentage and absolute caps both apply, whichever is smaller. At today's ~$1,400 bankroll the absolute cap binds (3% × $1,400 = $42 vs. $25). Below ~$830 bankroll the percentage cap binds first. Targets the 22:1 payoff asymmetry: the same 3-trade BNB cluster that lost $138 on 26MAY31 (3 × ~$46) would have lost ~$75 (3 × $25) under this cap. Honest trade-off: gains scale down proportionally too — backtest of the whitelist + this cap would have realized roughly +$30 to +$50 instead of +$83 uncapped. The asymmetry is preserved (still 22:1) but at one-third the dollar scale. Documented intent: keep cap tight until the whitelist accumulates 100+ trades with positive P&L, then raise. Placement header log now shows both caps and which is binding.
- **Replaced `SKIP_PREFIXES` blacklist with `SAFE_SERIES` whitelist in Safe Compounder.** Bot now only trades markets whose ticker starts with one of 12 explicitly-approved series prefixes followed by `-`. Anything else is rejected at the candidate filter — including every Kalshi series we've never traded, every long-tail oddity, and (defensively) the very monthly-aggregator tickers added to `SKIP_PREFIXES` earlier today. Approved series were chosen by data: n>=10 historical settlements AND net-positive realized P&L. Lifetime backtest on the 666-settlement history: whitelist would have produced **+$83 on 326 trades**; the excluded 121 distinct series produced **-$239 on 340 trades**. Net swing: ~+$240, or +5.5% return on cost vs. the actual -4.0%. Approved set (with historical n / P&L): `KXWTI 70/+$13.70`, `KXWTIW 41/+$9.46`, `KXBRENTD 42/+$4.58`, `KXBRENTW 18/+$3.44`, `KXAAAGASD 27/+$19.92`, `KXAAAGASW 17/+$12.42`, `KXSILVERD 18/+$1.78`, `KXINXU 44/+$3.33`, `KXBTC 18/+$8.13`, `KXETH 10/+$2.85`, `KXAPRPOTUS 10/+$1.86`, `KXEUROVISIONRANK 11/+$1.95`. Crypto distance gate (`is_crypto_threshold_safe`) still applies *within* whitelisted KXBTC / KXETH. `SKIP_PREFIXES`, `should_skip`, and the MINMON/MAXMON entries added earlier today are deleted — the whitelist's positive-allow semantics supersedes them. Prefix matching uses `ticker.startswith(prefix + "-")` so `KXWTI` does not accidentally swallow `KXWTIW` or `KXWTIMINM`. Caveats: ~80% drop in trade volume by design (idle most of the time); heavy oil/gas concentration (7 of 12 series); no exploration of new Kalshi series until manually approved.

### Fixed (2026-06-01)
- **Crypto monthly-aggregator markets now blocked outright in Safe Compounder.** `parse_crypto_market` in `safe_compounder.py` requires a `-T<price>` or `-B<price>` suffix, but `*MINMON` / `*MAXMON` tickers use a raw-number suffix (e.g. `KXBNBMAXMON-BNB-26MAY31-69000`). They returned `None` from the parser, so the 10% price-distance gate was skipped and these markets traded freely. This produced the worst single-day losses to date: three `KXBNBMAXMON-26MAY31` positions each lost ~$46 (≈ −$138 combined, ~88% of total realized loss). Added `KX{BTC,ETH,SOL,DOGE,BNB,XRP,HYPE}{MINMON,MAXMON}` to `SKIP_PREFIXES`. Monthly aggregators are inherently incompatible with the "near-certain" model — one spike during the month resolves YES.
- **Extended `CRYPTO_TICKER_MAP`** with `KXBNB → BNB`, `KXXRP → XRP`, `KXHYPE → HYPE` for any future daily T/B-suffix markets in these series, and added their CoinGecko IDs (`binancecoin`, `ripple`, `hyperliquid`) to `fetch_crypto_prices`. Missing-price coins fall through to the existing fail-safe (skip).

### Added (2026-05-27)
- **Per-event concentration cap (breaker #1)** in Safe Compounder, **enforced** (`EVENT_CAP_PCT = 15%`, `EVENT_CAP_DRY_RUN = False`). Groups markets by event ticker prefix (e.g. `KXWTI-26MAY28H17`) and blocks new orders that would push total cost basis on one event above 15% of bankroll. Known limitation: groups by event, not by category — multiple oil events (daily WTI + weekly WTIW + Brent) can each fill to the cap independently. Toggle `EVENT_CAP_DRY_RUN = True` for log-only observation mode.

### Changed (2026-05-27)
- **Removed `MAX_BET_DOLLARS` $5 hard cap** in Safe Compounder. Position sizing now relies on `MAX_POSITION_PCT = 3%` of bankroll (with half-Kelly when enabled) so bet size scales with the account balance. Reverts the cap added 2026-05-05. Also fixed a dangling `MAX_BET_DOLLARS` reference in `_calculate_position_size` that would have raised `NameError` on every order placement.

### Fixed (2026-05-08)
- **WEATHER detection now matches Kalshi's actual ticker scheme**: `infer_category()` in `category_scorer.py` only matched the substring `TEMP`, so temperature markets like `KXHIGHNY-…` and `KXHIGHDEN-…` (high/low temp by city) were classified as `OTHER` and slipped past the `HARD_BLOCKED_CATEGORIES` check. Added prefix matches for `KXHIGH`, `KXLOW`, `KXTEMP`, `KXSNOW`, `KXRAIN`, `KXSTORM`, `KXHURRICANE`, `KXTORNADO`, `KXWIND`, `KXPRECIP`, `KXFROST`, `KXFLOOD`, `KXDROUGHT`, `KXWEATHER`. `safe_compounder.SKIP_PREFIXES` had the same gap (e.g. `KXHIGHT` did not match `KXHIGHNY`) and was collapsed to the same broad prefix list.

### Fixed (2026-05-07)
- **WEATHER block now enforced at execution layer**: hard-blocked categories were only checked in `market_making.py` and `portfolio_optimization.py`; `quick_flip_scalping.py`, `unified_trading_system.py`, `decide.py`, and `trade.py` bypassed the check entirely. Added the guard to `execute_position()` in `jobs/execute.py` so it applies to every code path.

### Changed (2026-05-05)
- **Intraday-only mode**: market ingestion now filters to markets expiring within 24 hours; markets expiring in under 30 minutes are skipped (`ingest.py`)
- **Raised confidence floor**: `min_confidence_to_trade` 45% → 65%; `min_confidence_threshold` 45% → 65% (`settings.py`)
- **Tightened edge requirements**: `MIN_EDGE_REQUIREMENT` 4% → 8%; high-confidence edge 3% → 7%; medium-confidence edge 5% → 10%; low-confidence edge 8% → 15% (`edge_filter.py`)
- **Raised confidence floor in edge filter**: `MIN_CONFIDENCE_FOR_TRADE` 35% → 60%
- **Max hold time capped at 8 hours** (was 72h), floor raised to 2 hours (`stop_loss_calculator.py`)
- **`max_time_to_expiry_days`**: 14 → 1 → 5h (0.208 days)
- **`min_trade_edge`**: 8% → 12%; `min_confidence_for_large_size`: 50% → 70% (`settings.py`)
- **WEATHER** category remains hard-blocked regardless of score
- **`MAX_BET_DOLLARS`**: hard cap of $5 per bet in Safe Compounder (replaces percentage-only sizing)

### Added
- Initial public release of Kalshi AI Trading Bot
- Multi-agent AI decision engine with Forecaster, Critic, and Trader agents
- Real-time market scanning and analysis
- Portfolio optimization using Kelly Criterion and risk parity
- Live trading integration with Kalshi API
- Web-based dashboard for monitoring and control
- Performance analytics and reporting
- Market making strategy implementation
- Dynamic exit strategies
- Cost optimization for AI usage
- Comprehensive test suite
- Database management with SQLite support
- Configuration management system
- Logging and monitoring capabilities

### Features
- **Beast Mode Trading**: Aggressive multi-strategy trading system
- **Grok-4 Integration**: Primary AI model for market analysis
- **Real-time Dashboard**: Web interface for monitoring and control
- **Portfolio Management**: Advanced position sizing and risk management
- **Market Making**: Automated spread trading and liquidity provision
- **Performance Tracking**: Comprehensive analytics and reporting

### Technical
- Python 3.12+ compatibility
- Async/await architecture for high performance
- Type hints throughout the codebase
- Comprehensive error handling
- Rate limiting and API management
- Modular design for easy extension

## [1.0.0] - 2024-01-XX

### Added
- Initial release
- Core trading system with AI integration
- Multi-agent decision making
- Portfolio optimization
- Real-time market analysis
- Web dashboard
- Performance monitoring
- Database management
- Configuration system
- Testing framework

---

## Version History

### Version 1.0.0
- **Release Date**: January 2024
- **Status**: Initial public release
- **Key Features**: 
  - Multi-agent AI trading system
  - Real-time market analysis
  - Portfolio optimization
  - Web dashboard
  - Performance tracking

---

## Migration Guide

### From Development to Production
1. Set up environment variables in `.env` file
2. Initialize database with `python init_database.py`
3. Configure trading parameters in `src/config/settings.py`
4. Test with paper trading before live trading
5. Monitor performance and adjust settings as needed

---

## Deprecation Notices

No deprecations in current version.

---

## Breaking Changes

No breaking changes in current version.

---

## Known Issues

- Limited to SQLite database (PostgreSQL support planned)
- Requires manual API key management
- Performance may vary based on market conditions

---

## Future Roadmap

### Planned Features
- PostgreSQL database support
- Additional AI models
- Advanced risk management
- Mobile dashboard
- API rate limit optimization
- Enhanced backtesting capabilities

### Version 1.1.0 (Planned)
- Database migration tools
- Enhanced error handling
- Performance optimizations
- Additional trading strategies

### Version 1.2.0 (Planned)
- PostgreSQL support
- Advanced analytics
- Mobile interface
- API improvements 