"""The cache reader has to cope with every shape PKScreener writes."""

import os
import pickle
import time

import numpy as np
import pandas as pd
import pytest

from custom import data
from tests.factories import make_frame


def test_to_frame_accepts_a_dataframe():
    frame = data.to_frame(make_frame([100.0, 101.0, 102.0]))
    assert list(frame.columns) == data.OHLCV_COLUMNS
    assert len(frame) == 3


def test_to_frame_accepts_the_split_dict_pkscreener_also_writes():
    original = make_frame([100.0, 101.0, 102.0])
    frame = data.to_frame(original.to_dict("split"))
    assert frame is not None
    assert frame["Close"].tolist() == [100.0, 101.0, 102.0]


def test_to_frame_accepts_a_column_keyed_dict():
    original = make_frame([100.0, 101.0])
    frame = data.to_frame(original.to_dict())
    assert frame is not None
    assert len(frame) == 2


def test_to_frame_normalises_lowercase_columns():
    original = make_frame([100.0, 101.0]).rename(columns=str.lower)
    frame = data.to_frame(original)
    assert frame is not None
    assert list(frame.columns) == data.OHLCV_COLUMNS


def test_to_frame_rejects_frames_missing_a_column():
    original = make_frame([100.0, 101.0]).drop(columns=["Volume"])
    assert data.to_frame(original) is None


def test_to_frame_rejects_junk():
    assert data.to_frame(None) is None
    assert data.to_frame("not a frame") is None
    assert data.to_frame({}) is None
    assert data.to_frame(pd.DataFrame()) is None


def test_to_frame_sorts_and_deduplicates_the_index():
    frame = make_frame([100.0, 101.0, 102.0])
    shuffled = pd.concat([frame.iloc[[2]], frame.iloc[[0]], frame.iloc[[1]], frame.iloc[[2]]])
    result = data.to_frame(shuffled)
    assert result.index.is_monotonic_increasing
    assert not result.index.duplicated().any()
    assert len(result) == 3


def test_to_frame_drops_rows_without_a_close():
    frame = make_frame([100.0, 101.0, 102.0])
    frame.loc[frame.index[1], "Close"] = np.nan
    result = data.to_frame(frame)
    assert len(result) == 2


def _write_cache(directory, payload, name):
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        pickle.dump(payload, handle)
    return path


def test_find_cache_file_prefers_the_newest(tmp_path):
    _write_cache(tmp_path, {"A": make_frame([1.0])}, "stock_data_01012024.pkl")
    time.sleep(0.01)
    newest = _write_cache(tmp_path, {"A": make_frame([1.0])}, "stock_data_02012024.pkl")
    assert data.find_cache_file(str(tmp_path)) == newest


def test_find_cache_file_does_not_confuse_intraday_for_daily(tmp_path):
    daily = _write_cache(tmp_path, {"A": make_frame([1.0])}, "stock_data_01012024.pkl")
    time.sleep(0.01)
    _write_cache(tmp_path, {"A": make_frame([1.0])}, "intraday_stock_data_01012024.pkl")
    assert data.find_cache_file(str(tmp_path)) == daily
    assert "intraday" in data.find_cache_file(str(tmp_path), intraday=True)


def test_find_cache_file_returns_none_when_empty(tmp_path):
    assert data.find_cache_file(str(tmp_path)) is None


def test_load_from_cache_reads_every_symbol(tmp_path):
    payload = {
        "RELIANCE": make_frame(np.linspace(100, 120, 60)),
        "TCS.NS": make_frame(np.linspace(200, 220, 60)).to_dict("split"),
        "BROKEN": {"nonsense": 1},
    }
    _write_cache(tmp_path, payload, "stock_data_01012024.pkl")
    frames = data.load_from_cache(str(tmp_path))
    assert set(frames) == {"RELIANCE", "TCS"}  # .NS stripped, junk skipped


def test_load_from_cache_filters_to_requested_symbols(tmp_path):
    payload = {
        "RELIANCE": make_frame(np.linspace(100, 120, 60)),
        "TCS": make_frame(np.linspace(200, 220, 60)),
    }
    _write_cache(tmp_path, payload, "stock_data_01012024.pkl")
    frames = data.load_from_cache(str(tmp_path), symbols=["tcs"])
    assert set(frames) == {"TCS"}


def test_load_from_cache_respects_the_lookback(tmp_path):
    payload = {"RELIANCE": make_frame(np.linspace(100, 200, 300))}
    _write_cache(tmp_path, payload, "stock_data_01012024.pkl")
    frames = data.load_from_cache(str(tmp_path), lookback_days=50)
    assert len(frames["RELIANCE"]) == 50


def test_load_candles_raises_a_useful_error_with_no_cache(tmp_path):
    with pytest.raises(data.NoDataAvailable, match="data-refresh"):
        data.load_candles(str(tmp_path), symbols=None)


def test_liquidity_filters_drop_penny_illiquid_and_short_histories():
    frames = {
        "GOOD": make_frame(np.full(60, 500.0), np.full(60, 1_000_000.0)),
        "PENNY": make_frame(np.full(60, 3.0), np.full(60, 1_000_000.0)),
        "ILLIQUID": make_frame(np.full(60, 500.0), np.full(60, 100.0)),
        "TOOSHORT": make_frame(np.full(10, 500.0), np.full(10, 1_000_000.0)),
        "EXPENSIVE": make_frame(np.full(60, 500_000.0), np.full(60, 1_000_000.0)),
    }
    kept = data.apply_liquidity_filters(
        frames, min_price=20, max_price=100_000, min_avg_volume=100_000
    )
    assert set(kept) == {"GOOD"}


def test_liquidity_filters_keep_everything_when_wide_open():
    frames = {"A": make_frame(np.full(60, 1.0), np.full(60, 1.0))}
    kept = data.apply_liquidity_filters(frames, min_price=0, max_price=float("inf"), min_avg_volume=0)
    assert set(kept) == {"A"}


# --- multiple cache directories -------------------------------------------

def test_find_cache_file_searches_every_directory(tmp_path):
    """`pkscreenercli.py -d` writes to actions-data-download, a normal scan to
    results/Data. Both have to be found."""
    results = tmp_path / "results" / "Data"
    downloads = tmp_path / "actions-data-download"
    results.mkdir(parents=True)
    downloads.mkdir()
    _write_cache(downloads, {"A": make_frame([1.0])}, "stock_data_01012024.pkl")

    found = data.find_cache_file([str(results), str(downloads)])
    assert found is not None
    assert "actions-data-download" in found


def test_find_cache_file_picks_the_newest_across_directories(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    _write_cache(first, {"A": make_frame([1.0])}, "stock_data_01012024.pkl")
    time.sleep(0.01)
    newest = _write_cache(second, {"A": make_frame([1.0])}, "stock_data_02012024.pkl")
    assert data.find_cache_file([str(first), str(second)]) == newest


def test_a_colon_separated_string_is_treated_as_a_list(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    expected = _write_cache(second, {"A": make_frame([1.0])}, "stock_data_01012024.pkl")
    assert data.find_cache_file(f"{first}:{second}") == expected


def test_missing_directories_are_skipped_not_fatal(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    expected = _write_cache(real, {"A": make_frame([1.0])}, "stock_data_01012024.pkl")
    assert data.find_cache_file(["/nope/nowhere", str(real)]) == expected
