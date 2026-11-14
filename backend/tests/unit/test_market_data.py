from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.database.base import Database
from app.domain.fees import CommissionModel
from app.domain.models import AssetClass, Instrument
from app.instruments.registry import InstrumentRegistry, UnknownInstrumentError, UnmappedSymbolError, default_instruments
from app.market_data.calendars import get_calendar
from app.market_data.futures import build_continuous
from app.market_data.fx import FxRateProvider, FxRateUnavailable, parse_ecb_reference_rates
from app.market_data.quality import DataKind, validate_bars
from app.market_data.sources import load_csv, synthetic_bars
from app.market_data.store import BarStore
from app.market_data.timeframes import align_higher_timeframe, resample

UTC = timezone.utc


def bars(n=48, tf="1h", start="2024-01-01"):
    return synthetic_bars(tf, n, seed=1, start=start).bars


# ---------- timeframes ----------
def test_resample_aggregates_correctly_and_drops_incomplete_bar():
    df = bars(50)  # 50 hourly bars: 12 complete 4h bars + 2 leftover hours
    out = resample(df, "1h", "4h")
    assert len(out) == 12
    first = df.iloc[:4]
    assert out.iloc[0].tolist() == pytest.approx([first.open.iloc[0], first.high.max(), first.low.min(), first.close.iloc[-1], first.volume.sum()])
    assert len(resample(df, "1h", "4h", drop_incomplete=False)) == 13
    with pytest.raises(ValueError):
        resample(df, "1h", "5m")


def test_higher_timeframe_alignment_never_looks_ahead():
    ltf = bars(48)
    htf = resample(ltf, "1h", "4h")
    merged = align_higher_timeframe(ltf, htf, "1h", "4h")
    # Hours 0-2 of the first 4h bar: no completed 4h bar exists yet.
    assert merged["4h_close"].iloc[:3].isna().all()
    # The 03:00 bar closes at 04:00, exactly when the first 4h bar closes -> it may use it.
    assert merged["4h_close"].iloc[3] == htf.close.iloc[0] == ltf.close.iloc[3]
    assert merged["4h_close"].iloc[6] == htf.close.iloc[0]  # 06:00 bar still sees only the first 4h bar
    # Changing FUTURE data must not change already-aligned values.
    ltf2 = ltf.copy()
    ltf2.iloc[24:, :4] *= 2
    merged2 = align_higher_timeframe(ltf2, resample(ltf2, "1h", "4h"), "1h", "4h")
    pd.testing.assert_frame_equal(merged.iloc[:24], merged2.iloc[:24])


# ---------- calendars ----------
def test_calendars_handle_sessions_weekends_and_dst():
    nyse = get_calendar("XNYS")
    assert nyse.is_open(datetime(2024, 1, 3, 14, 30, tzinfo=UTC))  # 09:30 EST
    assert not nyse.is_open(datetime(2024, 1, 3, 14, 29, tzinfo=UTC))
    assert nyse.is_open(datetime(2024, 7, 3, 13, 30, tzinfo=UTC))  # 09:30 EDT (DST shifts UTC open)
    assert not nyse.is_open(datetime(2024, 7, 3, 20, 0, tzinfo=UTC))  # 16:00 EDT close is exclusive
    assert not nyse.is_open(datetime(2024, 1, 6, 15, 0, tzinfo=UTC))  # Saturday
    fx = get_calendar("FX")
    assert fx.is_open(datetime(2024, 1, 3, 3, 0, tzinfo=UTC))
    assert not fx.is_open(datetime(2024, 1, 6, 12, 0, tzinfo=UTC))  # Saturday
    assert not fx.is_open(datetime(2024, 1, 5, 22, 30, tzinfo=UTC))  # Friday after 17:00 NY
    assert fx.is_open(datetime(2024, 1, 7, 22, 30, tzinfo=UTC))  # Sunday after 17:00 NY
    assert get_calendar("24x7").is_open(datetime(2024, 1, 6, 12, 0, tzinfo=UTC))
    cme = get_calendar("CME")
    assert not cme.is_open(datetime(2024, 1, 3, 22, 30, tzinfo=UTC))  # daily 16:00-17:00 CT break
    with pytest.raises(ValueError):
        nyse.is_open(datetime(2024, 1, 3, 14, 30))


# ---------- instruments ----------
def test_registry_maps_symbols_and_refuses_to_guess():
    reg = InstrumentRegistry()
    for inst in default_instruments():
        reg.register(inst)
    reg.map_symbol("binance", "CRYPTO:BTC/USDT", "BTCUSDT")
    assert reg.to_broker("binance", "CRYPTO:BTC/USDT") == "BTCUSDT"
    assert reg.from_broker("binance", "BTCUSDT") == "CRYPTO:BTC/USDT"
    with pytest.raises(UnmappedSymbolError):
        reg.to_broker("ibkr", "CRYPTO:BTC/USDT")
    with pytest.raises(UnknownInstrumentError):
        reg.map_symbol("binance", "CRYPTO:DOGE/USDT", "DOGEUSDT")
    assert {i.asset_class for i in reg.all()} == set(AssetClass)


def test_instrument_rounding_and_notional():
    es = Instrument(id="FUTURE:ES", asset_class=AssetClass.FUTURE, symbol="ES", quote_currency="USD", tick_size=0.25, lot_size=1,
                    contract_multiplier=50)
    assert es.round_qty(2.9) == 2 and es.round_qty(-2.9) == -2 and es.round_qty(0.9) == 0
    assert es.round_price(5000.13) == 5000.25
    assert es.notional(2, 5000) == 500_000
    btc = Instrument(id="C", asset_class=AssetClass.CRYPTO, symbol="BTC/USDT", quote_currency="USDT", lot_size=0.001)
    assert btc.round_qty(0.3) == pytest.approx(0.3) and btc.round_qty(0.0019) == pytest.approx(0.001)


def test_commission_models():
    assert CommissionModel(rate=0.001).cost(2, 30_000) == pytest.approx(60)
    assert CommissionModel(per_unit=0.005, minimum=1).cost(50, 100) == 1.0
    assert CommissionModel(per_unit=0.005, minimum=1).cost(1000, 100) == 5.0
    assert CommissionModel(per_unit=2.25).cost(-3, 5000, multiplier=50) == 6.75
    assert CommissionModel(rate=0.001, minimum=1).cost(0, 100) == 0


# ---------- quality ----------
def test_validation_detects_duplicates_gaps_and_bad_rows_without_inventing_data():
    df = bars(24)
    broken = pd.concat([df.iloc[:10], df.iloc[9:10], df.iloc[12:]])  # duplicate 09:00; 10:00 and 11:00 missing
    broken.iloc[2, broken.columns.get_loc("high")] = broken.iloc[2]["low"] / 2  # high below low
    broken.iloc[5, broken.columns.get_loc("close")] = np.nan
    clean, report = validate_bars(broken.sample(frac=1, random_state=0), "1h")
    assert report.was_unsorted and clean.index.is_monotonic_increasing
    assert report.duplicates_removed == 1 and report.invalid_rows_removed == 2
    assert report.missing_bars == 4  # two genuine gaps + the two rejected rows
    assert not report.ok and len(clean) == 20
    assert clean.isna().sum().sum() == 0


def test_validation_rejects_naive_timestamps_and_normalizes_timezones():
    df = bars(5)
    with pytest.raises(ValueError, match="naive"):
        validate_bars(df.tz_localize(None), "1h")
    clean, report = validate_bars(df.tz_convert("Europe/Rome"), "1h")
    assert str(clean.index.tz) == "UTC" and report.timezone_converted_from == "Europe/Rome"
    assert (clean.index == df.index).all()
    with pytest.raises(ValueError, match="grid"):
        validate_bars(df.shift(freq="7min"), "1h")


def test_missing_bar_detection_respects_trading_calendar():
    idx = pd.date_range("2024-01-01", "2024-01-31", freq="1D", tz="UTC")
    weekdays = idx[idx.day_of_week < 5]
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0}, index=weekdays)
    assert validate_bars(df, "1d", get_calendar("XNYS"))[1].missing_bars == 0  # weekends are not gaps
    assert validate_bars(df, "1d", get_calendar("24x7"))[1].missing_bars == 8  # but for crypto they are
    assert validate_bars(df.drop(df.index[5]), "1d", get_calendar("XNYS"))[1].missing_bars == 1


# ---------- FX ----------
def t(day, hour=12):
    return datetime(2024, 1, day, hour, tzinfo=UTC)


def test_fx_uses_latest_rate_at_or_before_timestamp_never_a_future_one():
    fx = FxRateProvider()
    fx.add_rate("EUR", "USD", t(2), 1.10, "test")
    fx.add_rate("EUR", "USD", t(4), 1.20, "test")
    assert fx.quote("EUR", "USD", t(3)).rate == 1.10
    assert fx.quote("EUR", "USD", t(4)).rate == 1.20
    assert fx.quote("USD", "EUR", t(3)).rate == pytest.approx(1 / 1.10)
    with pytest.raises(FxRateUnavailable):
        fx.quote("EUR", "USD", t(1))  # only future rates exist
    with pytest.raises(FxRateUnavailable, match="older than"):
        fx.quote("EUR", "USD", t(20))  # stale


def test_fx_triangulation_pegs_and_costs():
    fx = FxRateProvider(conversion_cost_bps=10, pegs={"USDT": "USD"})
    fx.add_rate("EUR", "USD", t(2), 1.25, "ecb")
    fx.add_rate("EUR", "GBP", t(2), 0.80, "ecb")
    assert fx.quote("USD", "GBP", t(2)).rate == pytest.approx(0.64)
    assert fx.quote("USDT", "EUR", t(2)).rate == pytest.approx(0.8)
    assert fx.convert(1000, "USD", "EUR", t(2)) == pytest.approx(800 * (1 - 0.001))
    assert fx.convert(-1000, "USD", "EUR", t(2)) == pytest.approx(-800 * (1 + 0.001))  # losses get larger, not smaller
    assert fx.convert(1000, "EUR", "EUR", t(2)) == 1000  # no cost without a conversion
    assert fx.convert(1000, "USD", "EUR", t(2), with_cost=False) == pytest.approx(800)
    with pytest.raises(FxRateUnavailable):
        FxRateProvider().quote("USDT", "EUR", t(2))  # no peg configured -> no silent 1:1 assumption
    with pytest.raises(ValueError):
        fx.add_rate("EUR", "USD", datetime(2024, 1, 1), 1.1, "x")


def test_parse_ecb_xml():
    xml = """<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
    <Cube><Cube time="2024-03-15"><Cube currency="USD" rate="1.0892"/><Cube currency="JPY" rate="162.03"/></Cube></Cube></gesmes:Envelope>"""
    rows = parse_ecb_reference_rates(xml)
    assert rows == [("EUR", "USD", datetime(2024, 3, 15, 15, tzinfo=UTC), 1.0892), ("EUR", "JPY", datetime(2024, 3, 15, 15, tzinfo=UTC), 162.03)]


# ---------- sources / store ----------
def test_synthetic_data_is_deterministic_valid_and_labelled_simulated():
    a, b = synthetic_bars("1h", 500, seed=7), synthetic_bars("1h", 500, seed=7)
    assert a.kind is DataKind.SIMULATED and a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != synthetic_bars("1h", 500, seed=8).fingerprint()
    assert validate_bars(a.bars, "1h")[1].ok


def test_csv_import_requires_timezone(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("timestamp,open,high,low,close,volume\n2024-01-02 09:30:00,10,11,9,10.5,100\n2024-01-02 09:35:00,10.5,11,10,10.8,50\n")
    with pytest.raises(ValueError, match="timezone"):
        load_csv(path)
    ds = load_csv(path, tz="America/New_York")
    assert ds.kind is DataKind.HISTORICAL and ds.bars.index[0] == pd.Timestamp("2024-01-02 14:30", tz="UTC")


def test_bar_store_roundtrip_is_idempotent_and_keeps_provenance():
    db = Database("sqlite://")
    db.create_all()
    store, ds = BarStore(), synthetic_bars("1h", 100, seed=3)
    with db.session() as s:
        store.save(s, "CRYPTO:BTC/USDT", "1h", ds)
        store.save(s, "CRYPTO:BTC/USDT", "1h", ds)  # re-import must not duplicate
    with db.session() as s:
        loaded = store.load(s, "CRYPTO:BTC/USDT", "1h")
        assert loaded.kind is DataKind.SIMULATED and loaded.fingerprint() == ds.fingerprint()
        part = store.load(s, "CRYPTO:BTC/USDT", "1h", start=ds.bars.index[10], end=ds.bars.index[19])
        assert len(part.bars) == 10
        assert store.load(s, "CRYPTO:ETH/USDT", "1h") is None
        assert store.coverage(s)[0]["bars"] == 100


# ---------- futures ----------
def test_continuous_futures_back_adjustment_preserves_price_changes():
    idx = pd.date_range("2024-01-01", periods=40, freq="1D", tz="UTC")
    base = pd.Series(np.linspace(100, 139, 40), index=idx)

    def contract(offset, sl):
        p = base[sl] + offset
        return pd.DataFrame({"open": p, "high": p + 1, "low": p - 1, "close": p, "volume": 1.0})

    front, back = contract(0, slice(0, 25)), contract(3, slice(10, 40))  # back contract trades 3 higher (contango)
    series, rolls = build_continuous([("H", idx[24], front), ("M", idx[39] + pd.Timedelta(days=30), back)], roll_days_before_expiry=5)
    assert len(rolls) == 1 and rolls[0].gap == pytest.approx(3) and rolls[0].ts == idx[20]
    assert series.index.is_unique and len(series) == 40
    assert series.close.diff().dropna().to_numpy() == pytest.approx(np.ones(39))  # no artificial jump at the roll
    assert series.close.iloc[-1] == pytest.approx(base.iloc[-1] + 3)  # latest prices are real, history is shifted
    with pytest.raises(ValueError, match="overlap"):
        build_continuous([("H", idx[24], front), ("M", idx[39], contract(3, slice(30, 40)))])


def test_fx_uses_the_newest_observation_whichever_way_the_pair_was_recorded():
    from datetime import datetime, timedelta, timezone

    from app.market_data.fx import FxRateProvider, FxRateUnavailable

    t0 = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
    fx = FxRateProvider(max_age=timedelta(days=5))
    fx.add_rate("USD", "EUR", t0, 0.90, "manual")
    assert fx.quote("USD", "EUR", t0 + timedelta(days=1)).rate == 0.90
    with pytest.raises(FxRateUnavailable):
        fx.quote("USD", "EUR", t0 + timedelta(days=30))
    fx.add_rate("EUR", "USD", t0 + timedelta(days=29), 1.25, "ecb")  # published the other way round, a month later
    q = fx.quote("USD", "EUR", t0 + timedelta(days=30))
    assert q.rate == pytest.approx(0.8) and q.source == "ecb"       # the stale manual rate no longer hides the fresh one
    assert fx.quote("EUR", "USD", t0 + timedelta(days=30)).rate == 1.25
    assert fx.quote("USD", "EUR", t0 + timedelta(days=1)).rate == 0.90  # no look-ahead: an earlier moment still sees the earlier rate
