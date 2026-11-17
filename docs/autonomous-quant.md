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
| ETFs | Keeps a fixed, editable list of ETFs (SPY, QQQ, IWM and others) in the universe as ETFs and downloads their history | Alpaca key |
| Prices | Brings daily prices up to date once a day | Alpaca key |
| Crypto | Adds the configured crypto pairs (BTC/USD, ETH/USD and others, quoted in USD) and refreshes their daily history from Alpaca's crypto data; the still-forming bar of the current day is never stored | - |
| Candidates | Keeps the most traded instruments above the price and traded-value floors that the account can trade, plus anything with a live feed | - |
| Results, fundamentals, news | SEC filing index and reported figures of the followed US stocks (at most 45 seconds of requests per cycle), recent Alpaca news | SEC contact; Alpaca key for news |
| Live prices | Subscribes instruments that have a validated strategy | a live source |
| Scan | Creates a proposal wherever a validated strategy opens a position on its latest bar | - |
| Validation | Runs the walk-forward validation of each chosen strategy on each candidate; redoes it when it is older than 30 days or its filters changed. At most 60% of the cycle (at least 2 minutes); what is left waits for the next cycle | - |
| Scan again | Only when the validation produced setups that may trade | - |

The scan runs before the validation, so a cycle busy re-validating never delays an entry. The panel shows how long each
step took.

Each step reports what it did or why it was skipped, and one failing step does not stop the others. Every minute, independently of the
cycle, lapsed proposals are removed and orders working at an external broker are followed up.

Order handling is the user's choice, made with an explicit confirmation in the Autopilot panel (or under Settings): by default every
order waits for approval; with "send paper orders by itself" the scan and deployed strategies send orders to paper accounts without
asking, still sized and judged by the risk engine and protected by a stop. Orders for real-money accounts wait for approval in both cases.

What it does not do: it never changes the trading mode or a risk limit, and in semi-automatic mode it sends no order. Its output is
proposals; a signal whose proposal was rejected or lapsed is not proposed again for the same bar. If the user selects automatic mode
(Settings, after confirming the limits), orders of deployed strategies are sent to paper accounts without asking; real-money orders
still wait for approval.

## One data source: Alpaca

A default installation uses Alpaca for everything: the instrument list (`/v2/assets`), daily history, live prices and trading.
One key and secret, entered once: the credentials of a linked Alpaca account are also used for market data.
Charts are drawn with TradingView's open-source Lightweight Charts; TradingView is a charting component, not a data source.

From the Markets page, Alpaca can import its symbol list (US stocks and ETFs, or crypto pairs quoted in USD) and download daily history for
all of them; "all markets" in a download means every market Alpaca covers. Alpaca has no forex trading and no forex list: forex is not
available from this source. Exchange rates used for valuation come from the European Central Bank.

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

### Official bars

One-minute bars are no longer rebuilt from ticks where Alpaca publishes them. A few seconds after every minute closes, the bars of
all monitored instruments are downloaded in one request per hundred symbols (`market_data/intraday.py`) and take the same path as
before: stored, then deployed strategies, paper fills and stops. The bar of a deployed intraday timeframe is downloaded too, not
aggregated. A bar rebuilt from ticks is stored only for minutes in which Alpaca could not be reached, and is replaced by the
published one afterwards. Stops and working orders are still checked on every live tick-minute.

Monitored: instruments with a live subscription or a deployment, and every instrument with a stored intraday history.

| `source` on a bar | Feed | Used for |
|---|---|---|
| `alpaca:iex` | IEX exchange only; real time on the free plan; volume is IEX's share only | The minute refresh; intraday history by default |
| `alpaca:sip-delayed` | Consolidated tape; the free plan may read only data older than 15 minutes | Intraday history when chosen (setting "Intraday stock history from") |
| `alpaca:crypto` | Alpaca's crypto venue; real time | Crypto, refresh and history |
| `live:alpaca-stocks`, `live:alpaca-crypto` | Rebuilt here from live ticks | Fallback minutes only |

Stock bars are split-adjusted and limited to the regular session (09:30-16:00 New York). History defaults to IEX so that a strategy
is validated on the same feed its live signals come from; with the consolidated history, the newest bars of a series are IEX until
the next history run replaces them.

Restart: each request starts from the newest stored bar of the series, so minutes missed while the application was off are fetched
by the first refresh (up to seven days back). A late bar is stored and warms the indicators but never trades. The intraday history
(6 months by default for the intraday timeframes chosen in the Autopilot, or `POST /api/live/intraday/backfill`) is downloaded
backwards from the present, so an interrupted download leaves one contiguous block and is continued, not restarted. Rate limits
(HTTP 429) and server errors are retried with a pause; the API key is sent in headers only and never logged.

The dashboard shows, per instrument, when data last arrived, from which feed, and the newest bar of each timeframe
(`GET /api/live/freshness`).

Not exercised against Alpaca from the development environment: the request parameters are those of the endpoints the platform
already uses for daily bars; whether the consolidated history is readable on a given plan is detected at run time (a refusal falls
back to IEX and says so).

### Which instruments get live prices

Every followed instrument (the Autopilot's candidates, up to "Instruments followed"), in order of importance: open positions and
working orders first, then instruments with a validated or candidate strategy, then the other candidates, most traded first.
Alpaca's free plan streams at most 30 stock symbols over its WebSocket (verified live: about 0.3 s from trade to dashboard). The
first 30 in that order stream; the others are polled through Alpaca's REST "latest" endpoints every few seconds. When priorities
change (a position opens on a polled stock) the instruments are moved between the two. The limit is a setting, for paid plans.
Crypto pairs are not counted in the stock limit.

## Strategy modules

Five signal families (trend following, momentum, mean reversion, breakout, volatility) containing the 17 strategies of the library, and
four horizons (scalping 1m-5m, intraday 15m-1h, swing 4h-1d, position 1d-1w). A horizon is a timeframe range with its own stop and target
distances applied to the same signal families; it is not separate signal logic.

## Validation

A scan proposes a strategy for an instrument and timeframe only if a walk-forward validation on that series passed every check
below. A validation is valid for 30 days. Passing is not a promise of profit.

| # | Check (criteria version 2) | What it guards against |
|---|---|---|
| 1 | Market prices, not simulated | Testing on invented data |
| 2 | Parameters found in all 4 periods | Too little activity to judge |
| 3 | Profitable in at least 3 of 4 unseen periods | One lucky period |
| 4 | At least 20 trades on unseen data | Conclusions from a handful of trades |
| 5 | Positive return on unseen data | - |
| 6 | Drawdown on unseen data at most 25% | Profit bought with unacceptable losses |
| 7 | At least 30% of the in-sample result survived | Overfitting to the training period |
| 8 | Statistically significant: p-value at most 5% | Luck. The p-value is the chance that a strategy with no edge shows this Sharpe ratio on this many unseen days, allowing for skewed and fat-tailed returns (probabilistic Sharpe ratio) |
| 9 | Still profitable with trading costs doubled (spread, slippage, commissions) | An edge that only exists at optimistic costs |
| 10 | At least 60% of the parameter settings profitable on unseen data | A result that only one setting produces |
| 11 | Still profitable without its single best trade | A result that is one trade |

Then, across everything tested, **multiple testing**: with hundreds of strategy / instrument / timeframe combinations, some pass
checks 1 to 11 by chance. Among all setups tested, only those that survive a false-discovery-rate control at 10% (Benjamini-Hochberg
on the p-values) stay "validated"; one that passed on its own but not across the family is reported as a candidate, with that
reason. The more setups are tested, the stronger a result has to be. The Autopilot also reports the Sharpe ratio the best of that
many worthless strategies would be expected to show. A verdict recorded under the previous criteria (seven checks) is not trusted:
it is due for a new test and is not traded meanwhile.

Two levels of evidence, measured rather than assumed. On twelve years of real daily prices of four US stocks (AAPL, TSLA, VTI, AMZN)
and all 17 strategies, no pair reached twenty out-of-sample trades in the three most recent years: a daily strategy trades one
instrument too rarely. Hence:

| Level | Requirements | Where it may trade |
|---|---|---|
| validated | all eleven checks, and the multiple-testing control | any account |
| candidate | market prices, profitable in 3 of 4 unseen periods, at least 8 unseen trades, positive return, drawdown at most 25%, still profitable with costs doubled and without its best trade | paper accounts only |
| exploration | failed validation, but: market prices, profitable in 2 of 4 unseen periods, at least 5 unseen trades, positive return after costs, drawdown at most 25% | paper accounts only, and only when "Exploration" is switched on |

"Paper accounts" means the internal simulated account or a broker's paper environment (Alpaca paper, an exchange testnet):
anything without a live-trading authorization. Paper accounts follow the trading mode; in automatic mode their orders are sent
without asking. Real-money accounts always wait for approval unless that default is changed, and they never receive a candidate
or exploration entry: the scan skips them, and the trading engine refuses such an entry again when the order is created and when
it is approved, so a deployment started on paper cannot trade real money if its account is later switched to live.

Exploration exists to get many more trades and real-time evidence on setups the validation could not settle. It is not evidence
of an edge, and with costs included the expected result is negative. Every exploratory proposal says "EXPLORATION (failed
validation, paper only)", and closed trades are reported per evidence level on the Autonomous page (validated, candidate,
exploration), so exploratory results never mix with the others. For many trades a day, combine it with intraday timeframes
(15m, 1h), small positions (per-instrument limit 5-10%, risk per trade 0.5-1%) and crypto pairs, which trade around the clock.

The counts in the next paragraph were measured with the previous criteria; the stricter ones can only validate fewer.

On that same data, long-only: 1 pair validated, 11 candidates, 52 rejected. Candidates are weaker evidence, and when many pairs are
tested some reach the level by chance; every proposal says which level it rests on. Candidates can be switched off in the Autopilot settings.

Exits are part of what the validation chooses (criteria version 3). In each walk-forward period it tries the stop at 1.5 and
2.5 ATR (and the horizon's own), and the take profit at half, once and one and a half times the horizon's target, or none at all
(the position then closes only on the strategy's exit or the stop). For 15m/30m/1h that is 1.5, 3, 4.5 ATR or none; for 5m
0.75, 1.5, 2.25 or none. The values chosen in sample are judged on the unseen period that follows, and the proposal uses the
ones the validation picked. The strategies' own parameters keep their defaults. More choices make a lucky in-sample fit more
likely; the parameter-robustness and multiple-testing checks count every combination tried. Raising the criteria version
sends every earlier validation back to be tested again.

Strategies are validated and traded long-only by default: a small cash account cannot sell short, and a rule should be tested the way
it will be traded. Validation uses the most recent 1,500 bars (about six years of daily data); intraday timeframes use the most recent
20,000 bars (about a year of five-minute bars). Significance is always judged on daily returns, so a short intraday history cannot
pass on the number of bars alone.

Validations run side by side on several processor cores (all but one by default, setting "Processor cores for validation"). The
results are identical to running them one after the other; if worker processes cannot be started, that is what happens.

The universe grows in steps: 30 instruments to start, then up to 40 more a day towards the limit (200 by default), and only while
the previous cycle finished its validations. Alpaca's screener lists at most 100 names per ranking; both rankings together give
about 150 different names a day, so the upper part of the universe fills over several days.

The market regime is reported on every proposal but is not a gate by default: the validation tested the rule in every regime, so
filtering on it would trade a different rule from the one tested. It can be made a requirement in the Autopilot settings.

One entry per instrument: when several strategies signal the same stock, the one with the strongest evidence gets the proposal.

The scan also requires: prices not out of date, a live feed for scalping and intraday, a market regime the family is meant for, the
instrument tradable at the chosen account, and an entry that is still current: the strategy took its position on the latest bar or at
most three bars ago, and the price has not moved by more than half the stop distance since. Each reason for not proposing is listed,
and the Autopilot panel counts where the strategy/instrument pairs stopped (not validated, stale prices, regime, no entry now...).
An entry is identified by the bar that opened it, so it is proposed once, however many scans see it.

A proposal lives for the configured time (15 minutes) or half a bar of its timeframe, whichever is longer, up to a day: twelve hours
for a daily signal. Price and risk are checked again when it is approved.

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

## Fractional shares

US stocks and ETFs trade in fractions of a share (0.0001) when "Fractional shares" is on in Settings; it is on for new installations
and for installations saved before the option existed. With 1,000 EUR and at most 25% per instrument, a position is worth 250 EUR:
without fractions, any stock above that price could never be bought. At Alpaca, fractional quantities are sent as market orders for
the day; a protective stop for a fractional position is not left resting at the broker (the platform watches it and closes at the
bar that crosses it), and orders worth less than 1 USD are refused.

## How positions are closed

| Exit | Closes by itself? |
|---|---|
| Protective stop | Yes, always: resting at the broker, or watched by the platform on live prices |
| Take profit | Yes, always (resting, or watched by the platform) |
| Exit signalled by the strategy | Semi-automatic: a closing proposal that waits for approval and never lapses. With "close on exit signal" switched on (Settings), it is sent without asking on paper accounts; never without approval on a real-money account |
| Emergency Stop | Follows the emergency policy in Settings (default: blocks new risk, closes nothing) |

If a stop or target closes a position while its exit proposal is waiting, the proposal is withdrawn.

For an instrument with live prices, the live minutes fill orders and trigger stops, while the downloaded daily bars still reach daily
strategies (signals only), so their exits are produced. Before this was fixed, a paper order on such an instrument never filled.

## Exits the broker cannot hold

Some exits cannot rest at Alpaca: a stop for a fractional quantity, a plain stop for crypto, and a take-profit next to a stop on the same
shares (Alpaca reserves the shares for the first resting sell order). The platform watches these itself: on every live minute, and on
every stored bar, a position whose price is through its stop or target is closed at market. A closing order already on its way is never
sent twice. This only works while prices arrive, so:

- every minute the platform asks for live prices for any such position that has none;
- if no live source covers the instrument, a critical notification says that the position has no working stop.

At approval, the quantity is reduced (never increased) if the price has moved so that the proposed quantity no longer fits the exposure
room or the risk budget; if nothing tradable fits, the proposal is cancelled.

## Before real money

Live trading is switched off and nothing in this document changes that. The Alpaca adapter and the WebSocket stream have been tested
against emulations, not against Alpaca itself. Before any real money, on the Alpaca **paper** account, check at least: the account
connects and shows the broker's equity; an approved order appears at Alpaca with the right quantity; its fill is booked here and the
books reconcile; a whole-share position gets its resting stop at Alpaca; a fractional position is closed by the platform when its
stop is crossed; an order approved after hours behaves as expected; a restart keeps positions, stops and the virtual capital;
Emergency Stop blocks new orders. Then several weeks of paper results compared with simply holding an index ETF.

## Exposure counts orders on their way

The risk engine judges every new order against open positions plus entry orders already sent and not yet filled. Before this, several
orders sent in the same moment could each fit a limit and together exceed it.

## Entry filters: quarterly results, fundamentals, news

Three filters keep strategies out of entries the price data alone cannot see. They only remove entries: they never open,
enlarge or keep a position, and never block an order that closes one. The user's own orders are not filtered.

| Filter | Data | Rule (defaults, editable in the Autopilot settings) | In the validation |
|---|---|---|---|
| Quarterly results | SEC filing index: 8-K item 2.02 (or 10-Q/10-K) acceptance times | no new position for 24 hours after a release, nor from 2 days before the next expected one (last release + the usual interval) until it happens (at most 5 days late) | yes |
| Fundamentals | SEC XBRL figures as published (point in time) | long entries of daily/weekly strategies in US stocks only when the latest annual report shows a profit, positive equity, revenue down no more than 10% and liabilities at most 10 times equity | yes |
| News | Alpaca news (Benzinga), the Alpaca key already stored | no new position for 60 minutes after a news item about the instrument (items tagged with more than 3 instruments are market news and do not count) | no: live only |

The validation applies the first two exactly as live trading does: a position whose opening bar falls in a blocked period
is removed from the backtest, and the strategy waits for its next signal (it never enters late). Each validation records
which filters it included; when that changes (data for a stock arrives, a filter is switched), the setup is tested again.
Within a level, the scan looks first at companies with stronger reported fundamentals (growth, margin, return on equity).

The SEC requires every request to name who is asking, with a contact e-mail ("Contact given to the SEC"; the
QAT_SEC_USER_AGENT setting takes precedence). The SEC may refuse a generic address. Data are refreshed by the Autopilot's
"Results, fundamentals, news" step: filing index daily, reported figures after each new 10-Q/10-K (at least monthly), news
every cycle. ETFs and crypto have no results or reports: only the news pause applies to them.

## Positions you close or open yourself at the broker

You may close, reduce or open positions directly in the broker's own app or website (Alpaca). On every round of order
checks the platform compares its books with the broker's positions:

* **closed or reduced there**: booked as an exit with the reason `closed_at_broker`, at the broker's own fill price for
  that order (the latest filled order on the instrument that the platform did not send); if none is found, at the last
  known price, and the alert says so. The position's stops and targets are cancelled, closing proposals withdrawn, and a
  partial close resizes the stop of what is left (manual positions are reduced first, then the smallest);
* **opened there**: followed as a manual position (`manual:broker`), without a stop;
* **reversed** (long to short or the other way): left as a discrepancy for you to look at.

A difference is taken over only when it is seen again at least 20 seconds later on freshly read figures, and never from
figures the broker could not refresh, so a fill of the platform's own order is never mistaken for your action. While it
waits for that confirmation (at most five minutes), and while one of the platform's orders on that instrument is on its
way, it does not trip the reconciliation breaker. Resetting that breaker first takes in what you did at the broker.

Before any closing order is sent to a broker that does not enforce reduce-only itself (Alpaca), the platform reads the
position actually held there: if it is gone, nothing is sent; if it is smaller, only what is held is sold. A position you
already closed can therefore never be sold again into a short.

## Capital flows and crypto fees

The daily-loss and drawdown limits compare equity with the start of the day and with its peak. An account that is
connected or disconnected, or whose capital basis changes (a broker's own balance replaced by a virtual capital, a virtual
deposit or withdrawal), moves those reference levels by the same amount: it is capital moving, not a gain or a loss. An
account that leaves does so at its current value, so a loss it made that day still counts. After the update that
introduced this, the reference levels start again once from the current equity.

Alpaca, like most crypto venues, takes its trading fee out of the coins bought. A long crypto holding at a broker that is
smaller than the books by at most 1% is booked as that fee (the books take the broker's quantity, the difference is a
commission at the average price). More coins than the books, a short, or a larger gap remain a discrepancy and stop new risk.

## Limits

- Crypto is followed whatever its traded value: the volume in Alpaca's crypto bars is Alpaca's own venue, a small part of the market.
  On the internal paper account, fills are limited to a tenth of each bar's volume, so a crypto order on one-minute bars can fill in several pieces.
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
