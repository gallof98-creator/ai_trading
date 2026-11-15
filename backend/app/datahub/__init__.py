"""Data platform: research datasets from free, official APIs, kept in a Parquet lake and queried with DuckDB.

    providers -> ingestion (async, rate limited) -> validation and normalisation -> lake (Parquet) + catalog (Postgres)
              -> versioned features -> research, backtesting, Quant AI

This package is read-only with respect to trading. It imports nothing from `brokers`, `execution`,
`risk`, `strategies` or `signals`, holds no trading credentials and can never place an order.
Every stored row keeps the source it came from and the time at which it became known, so a
backtest can use exactly what was available at each moment and nothing later.
"""
