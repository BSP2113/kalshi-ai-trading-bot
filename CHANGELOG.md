# Changelog

All notable changes to the Kalshi AI Trading Bot project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed (2026-06-01)
- **Replaced `SKIP_PREFIXES` blacklist with `SAFE_SERIES` whitelist in Safe Compounder.** Bot now only trades markets whose ticker starts with one of 12 explicitly-approved series prefixes followed by `-`. Anything else is rejected at the candidate filter — including every Kalshi series we've never traded, every long-tail oddity, and (defensively) the very monthly-aggregator tickers added to `SKIP_PREFIXES` earlier today. Approved series were chosen by data: n>=10 historical settlements AND net-positive realized P&L. Lifetime backtest on the 666-settlement history: whitelist would have produced **+$83 on 326 trades**; the excluded 121 distinct series produced **-$239 on 340 trades**. Net swing: ~+$240, or +5.5% return on cost vs. the actual -4.0%. Approved set (with historical n / P&L): `KXWTI 70/+$13.70`, `KXWTIW 41/+$9.46`, `KXBRENTD 42/+$4.58`, `KXBRENTW 18/+$3.44`, `KXAAAGASD 27/+$19.92`, `KXAAAGASW 17/+$12.42`, `KXSILVERD 18/+$1.78`, `KXINXU 44/+$3.33`, `KXBTC 18/+$8.13`, `KXETH 10/+$2.85`, `KXAPRPOTUS 10/+$1.86`, `KXEUROVISIONRANK 11/+$1.95`. Crypto distance gate (`is_crypto_threshold_safe`) still applies *within* whitelisted KXBTC / KXETH. `SKIP_PREFIXES`, `should_skip`, and the MINMON/MAXMON entries added earlier today are deleted — the whitelist's positive-allow semantics supersedes them. Prefix matching uses `ticker.startswith(prefix + "-")` so `KXWTI` does not accidentally swallow `KXWTIW` or `KXWTIMINM`. Caveats: ~80% drop in trade volume by design (idle most of the time); heavy oil/gas concentration (7 of 12 series); no exploration of new Kalshi series until manually approved.

### Fixed (2026-06-01)
- **Crypto monthly-aggregator markets now blocked outright in Safe Compounder.** `parse_crypto_market` in `safe_compounder.py` requires a `-T<price>` or `-B<price>` suffix, but `*MINMON` / `*MAXMON` tickers use a raw-number suffix (e.g. `KXBNBMAXMON-BNB-26MAY31-69000`). They returned `None` from the parser, so the 10% price-distance gate was skipped and these markets traded freely. This produced the worst single-day losses to date: three `KXBNBMAXMON-26MAY31` positions each lost ~$46 (≈ −$138 combined, ~88% of total realized loss). Added `KX{BTC,ETH,SOL,DOGE,BNB,XRP,HYPE}{MINMON,MAXMON}` to `SKIP_PREFIXES`. Monthly aggregators are inherently incompatible with the "near-certain" model — one spike during the month resolves YES.
- **Extended `CRYPTO_TICKER_MAP`** with `KXBNB → BNB`, `KXXRP → XRP`, `KXHYPE → HYPE` for any future daily T/B-suffix markets in these series, and added their CoinGecko IDs (`binancecoin`, `ripple`, `hyperliquid`) to `fetch_crypto_prices`. Missing-price coins fall through to the existing fail-safe (skip).

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