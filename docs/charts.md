# Charts and the datafeed

The Chart page is a view on the platform's own data. Nothing is fetched from TradingView's website or from any unofficial API.

## Which TradingView product is used, and why

| Product | Licence | Used here |
|---|---|---|
| Lightweight Charts | Open source (Apache-2.0); attribution to TradingView required and shown | Yes |
| Advanced Charts | Granted by TradingView to companies on request; the free tier is for public websites with visible attribution, not private or paywalled use, and not for individuals | No |
| Trading Platform | Separate paid licence | No |

This platform is private and behind a login, so it does not meet the conditions of the free Advanced Charts tier as published by TradingView
(checked November 2026; confirm with TradingView before relying on it). What Advanced Charts would add: drawing tools, its library of
built-in indicators, saved layouts.

The backend follows the shape of TradingView's Datafeed API, so the chart component can be swapped if a licence is obtained:

| Datafeed concept | Endpoint |
|---|---|
| `onReady` | `GET /api/charts/config` |
| `searchSymbols` | `GET /api/charts/search?q=&exchange=&type=&sector=&has_data=` |
| `resolveSymbol` | `GET /api/charts/symbols?symbol=` |
| `getBars` | `GET /api/charts/history?symbol=&resolution=&from=&to=&countback=` -> `{s, t, o, h, l, c, v}` |
| `subscribeBars` | none: the page polls `history` every 20 seconds |

## Symbols

`symbol` may be a platform instrument id (`STOCK:AAPL`), a catalog id (`sec:123`), an ISIN, a FIGI, `EXCHANGE:TICKER` or a bare ticker.
When several listings share an identifier, active listings that are in the trading universe win and the answer says how many others exist.
The reply carries ticker, exchange, MIC, ISIN, FIGI, each provider's own symbol, and the provenance of the stored prices.

## Resolutions

1, 5, 15, 30, 60 and 240 minutes, daily, weekly, monthly. A resolution is offered only when stored bars can build it exactly: weekly and
monthly from daily bars, 5-minute from 1-minute bars. Intraday charts are never interpolated from daily bars.
The data providers connected today deliver daily bars only; intraday bars exist only for instruments fed by a connected broker.

## Studies

`GET /api/charts/studies` returns moving averages, Bollinger bands, RSI, MACD, ATR, the trend (price against its 50- and 200-bar averages),
the regime, support and resistance, and the signals recorded for the instrument. Support and resistance are clusters (within 1.5%) of swing
highs and lows of the last 250 bars; a swing counts only after five later bars confirm it. These are descriptions, not forecasts.

## Orders from the chart

`POST /api/charts/orders` accepts `buy` (quantity and a protective stop are mandatory) and `close`. Paper accounts only: a live account is
refused. The order goes through the same route as every other order (risk engine, then the broker adapter), is priced for the risk check on the
last stored close, is refused if that close is more than 30 days old, and fills at the open of the next bar, never at the price on screen.

## Limits

- No real-time prices for stocks: the badge on the chart says what the data is (historical, delayed, real-time, simulated) and how old the last bar is.
- No push streaming; the last bar is refreshed by polling.
- No drawing tools and no user-defined indicators.
- Sector search works only for instruments whose sector is known; the free sources connected do not supply sectors.
- An instrument that is in the catalog but not in the trading universe can be found and inspected, but has no chart until it is added and prices are downloaded.
