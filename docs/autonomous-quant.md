# Autonomous Quant Trading

How the semi-automatic trading loop works, what was verified and what was not.

## Initial configuration

A new installation starts with: virtual capital 1,000 EUR, base currency EUR, semi-automatic mode, 2% risk per trade (20 EUR), live trading
disabled. These are ordinary settings (Settings page), stored in the database on first use, not constants in the trading code.
Automatic mode cannot be selected until the box "I have reviewed the daily-loss, drawdown and exposure limits" is ticked.

## The loop

1. **Data** arrives as live ticks (`app/market_data/live.py`) or as stored bars from a download.
2. A **strategy** (deployed by hand, or deployed by a scan because it passed validation) produces a signal on a completed bar.
3. **Sizing**: `quantity = equity x risk % / (stop distance + estimated cost per unit)`, converted to the base currency, capped by the
   exposure limits, rounded down to the lot. Costs are spread (the live one when known), slippage and commission on both sides, and
   currency conversion when the instrument is not in EUR. All four are settings.
4. The **risk engine** judges the order. It is independent of strategies and of the opportunity engine and neither can change its limits.
5. In semi-automatic mode the order becomes a **proposal**. Nothing is sent.
6. A person approves or rejects one proposal at a time. On approval the platform checks again: the proposal has not expired
   (15 minutes by default; orders that only close a position never expire), a live-fed instrument has a fresh tick, the price has not
   moved by more than 25% of the stop distance, and the risk engine still accepts it on the current price. If anything fails the
   proposal is cancelled and nothing is sent.
7. The order goes to the broker adapter with a client order id, so a retry can never create a second order. A purchase gets its
   protective stop (and target) as soon as it fills.

Pause and Emergency Stop both engage the kill switch: no new risk can be taken. Positions keep their stops; whether working orders
are cancelled or positions closed follows the emergency policy in Settings (default: nothing is liquidated).

## Autopilot

Off by default; switched on from the Autonomous Quant page. Every cycle (15 minutes by default) it does what would otherwise need clicks:

| Step | What it does | Needs |
|---|---|---|
| Exchange rates | Fetches the European Central Bank's daily reference rates | internet |
| Universe | When fewer instruments than configured have prices, adds the most traded US stocks (Alpaca's screener) and downloads their history | Alpaca key |
| Prices | Brings daily prices up to date once a day | Alpaca key |
| Candidates | Keeps the most traded instruments above the price and traded-value floors that the account can trade, plus anything with a live feed | - |
| Validation | Runs the walk-forward validation of each chosen strategy on each candidate; redoes it when it is older than 30 days | - |
| Live prices | Subscribes instruments that have a validated strategy | a live source |
| Scan | Creates a proposal wherever a validated strategy opens a position on its latest bar | - |

Each step reports what it did or why it was skipped, and one failing step does not stop the others. Every minute, independently of the
cycle, lapsed proposals are removed and orders working at an external broker are followed up.

What it does not do: it never changes the trading mode or a risk limit, and in semi-automatic mode it sends no order. Its output is
proposals; a signal whose proposal was rejected or lapsed is not proposed again for the same bar. If the user selects automatic mode
(Settings, after confirming the limits), orders of deployed strategies are sent to paper accounts without asking; real-money orders
still wait for approval.

## One data source: Alpaca

A default installation uses Alpaca for everything: the instrument list (`/v2/assets`), daily history, live prices and trading.
One key and secret, entered once: the credentials of a linked Alpaca account are also used for market data.
Charts are drawn with TradingView's open-source Lightweight Charts; TradingView is a charting component, not a data source.

Consequences:

- The universe is what Alpaca can trade: US stocks and ETFs on NYSE, Nasdaq, NYSE Arca, NYSE American and Cboe, plus crypto pairs.
  No European or other markets (CSV import remains available for those).
- Alpaca publishes no instrument type, ISIN or FIGI. The type (ETF, preferred, ADR, warrant...) is inferred from the asset's name and
  marked as inferred; an asset whose name says nothing is treated as common stock.
- The connectors for Tiingo, Alpha Vantage, EODHD, Finnhub and OpenFIGI stay in the code, switched off and hidden. Set
  `QAT_ENABLED_PROVIDERS` to a list of names, or `all`, to switch them back on.

## Live market data

| Source | Covers | Transport | Label |
|---|---|---|---|
| Alpaca crypto | Crypto pairs quoted in USD | WebSocket; REST polling when a WebSocket cannot be opened. Works without a key at a slow rate | LIVE |
| Alpaca stocks | US stocks and ETFs, **IEX exchange only** on the free plan (not the consolidated tape) | same | LIVE (IEX) |
| Finnhub | US trades, when no Alpaca key is present | WebSocket only | LIVE |
| Everything else | European and other markets | none free | END OF DAY (stored bars) |

Every tick records provider, the provider's timestamp, the time it was received and the feed class. A price with no update for 60
seconds is STALE: it is shown as such and nothing is sized or approved on it. The browser receives ticks over server-sent events
(`GET /api/live/stream`); the chart's last candle moves without polling.

One-minute bars are built from ticks, stored (`realtime`), and drive deployed strategies, paper fills and stops. Higher timeframes are
aggregated from them when the bucket completes.

## Strategy modules

Five signal families (trend following, momentum, mean reversion, breakout, volatility) containing the 17 strategies of the library, and
four horizons (scalping 1m-5m, intraday 15m-1h, swing 4h-1d, position 1d-1w). A horizon is a timeframe range with its own stop and target
distances applied to the same signal families; it is not separate signal logic.

## Validation

A scan proposes a strategy for an instrument and timeframe only if a walk-forward validation on that series passed all of:
market (not simulated) prices; parameters found in all 4 periods; profitable in at least 3 of 4 unseen periods; at least 20 trades on
unseen data; positive out-of-sample return; out-of-sample drawdown at most 25%; at least 30% of the in-sample result survived.
A validation is valid for 30 days. Passing is not a promise of profit.

The scan also requires: prices not out of date, a live feed for scalping and intraday, a market regime the family is meant for, an
entry on the latest completed bar, the instrument tradable at the chosen account. Each reason for not proposing is listed.

## Alpaca

`app/brokers/alpaca_adapter.py`: account, buying power, positions, orders (market, limit, stop, stop-limit), status, cancellation,
replacement, tradability / shortability / fractionability checks, market clock. Paper and live are separate endpoints with separate
credentials; the live endpoint is reachable only through the platform's live gate, which is closed.

Virtual capital: the configured capital is converted once into the account currency at the rate recorded when the account is linked;
the reported equity is that amount plus the broker account's profit and loss since then. The broker's own equity and buying power are
shown separately and never used as the user's capital. Virtual deposits and withdrawals are recorded with their exchange rate.

Other real-money brokers plug in the same way: implement `BrokerAdapter`, add a branch in `TradingRuntime.connect_broker`. CCXT
exchanges and Interactive Brokers already exist.

## What was verified, and how

| Item | Verified |
|---|---|
| Tick hub, minute bars, staleness, provenance | Automated tests |
| Alpaca WebSocket protocol (auth, subscribe, trades, quotes) | Automated test against a local server speaking the protocol |
| Alpaca REST polling fallback | Automated test, and **live against Alpaca's real crypto endpoint** (no key) |
| Tick to browser over server-sent events | Automated test over real HTTP; live in a browser with real BTC/USD prices |
| Live bars drive strategies, proposals, paper fills and stops | Automated tests; live run with real prices |
| Sizing with costs, stale-data and spread guards, proposal expiry and re-check | Automated tests |
| Alpaca broker adapter | Automated tests against an emulated Alpaca API. **Not exercised with real Alpaca credentials** |
| Alpaca WebSocket against the real service | **Not verified**: the development environment blocks WebSocket connections |
| Alpaca stock data (IEX), Finnhub stream against the real services | **Not verified** (need keys; Finnhub needs WebSocket) |

## Limits

- No free real-time source for non-US stocks. They stay END OF DAY.
- Alpaca trades US stocks, ETFs and crypto only; everything else in the catalog is analysable, not tradable there.
- Alpaca does not accept plain stop orders for crypto: the platform then watches the stop itself and closes at the bar close beyond it.
- Whole shares for stocks. With 20 EUR of risk, expensive shares or wide stops can produce a size below one share: no proposal.
- Virtual equity converts the broker's profit and loss at the current exchange rate, not trade by trade.
- Without an Alpaca key, crypto prices are polled every few seconds (delay about 1-4 s). With a key and a network that allows
  WebSockets they are pushed.
- No macroeconomic or fundamental data source is connected; the opportunity engine uses price, volume, volatility and regime only.
- Backtests of a single strategy size positions without the cost adjustment used live (costs are still charged in the simulation).
- Background jobs run inside the API process; one API process is assumed.
