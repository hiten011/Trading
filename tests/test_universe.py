"""Universe resolution across its sources and fallbacks."""

import pickle

from custom import universe
from tests.factories import make_frame


def _write_cache(directory, symbols):
    path = directory / "stock_data_01012024.pkl"
    with open(path, "wb") as handle:
        pickle.dump({symbol: make_frame([100.0, 101.0]) for symbol in symbols}, handle)
    return path


def test_from_file_reads_symbols_and_ignores_comments(tmp_path):
    path = tmp_path / "universe.txt"
    path.write_text("# a comment\n\nRELIANCE\ntcs\nINFY.NS  # inline comment\n")
    assert universe.from_file(str(path)) == ["INFY", "RELIANCE", "TCS"]


def test_from_file_on_a_missing_file_is_empty():
    assert universe.from_file("/nonexistent/universe.txt") == []


def test_from_cache_lists_every_symbol(tmp_path):
    _write_cache(tmp_path, ["RELIANCE", "TCS.NS", "infy"])
    assert universe.from_cache([str(tmp_path)]) == ["INFY", "RELIANCE", "TCS"]


def test_from_cache_without_a_cache_is_empty(tmp_path):
    assert universe.from_cache([str(tmp_path)]) == []


def test_from_cache_survives_a_corrupt_pickle(tmp_path):
    (tmp_path / "stock_data_01012024.pkl").write_bytes(b"not a pickle")
    assert universe.from_cache([str(tmp_path)]) == []


def test_auto_means_everything_in_the_cache(tmp_path):
    _write_cache(tmp_path, ["RELIANCE", "TCS"])
    # None is the "no filtering, take the lot" signal to the data layer.
    assert universe.resolve(mode="auto", data_dirs=[str(tmp_path)]) is None


def test_file_mode_uses_the_file(tmp_path):
    _write_cache(tmp_path, ["RELIANCE", "TCS"])
    path = tmp_path / "universe.txt"
    path.write_text("TCS\n")
    resolved = universe.resolve(mode="file", data_dirs=[str(tmp_path)], universe_file=str(path))
    assert resolved == ["TCS"]


def test_file_mode_falls_back_when_the_file_is_empty(tmp_path):
    _write_cache(tmp_path, ["RELIANCE", "TCS"])
    path = tmp_path / "universe.txt"
    path.write_text("# nothing here\n")
    assert universe.resolve(mode="file", data_dirs=[str(tmp_path)], universe_file=str(path)) is None


def test_pkscreener_mode_falls_back_to_the_cache_when_unavailable(tmp_path, monkeypatch):
    _write_cache(tmp_path, ["RELIANCE"])
    monkeypatch.setattr(universe, "from_pkscreener", lambda index_option: [])
    assert universe.resolve(mode="pkscreener", data_dirs=[str(tmp_path)]) is None


def test_pkscreener_mode_uses_the_fetcher_when_it_works(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "from_pkscreener", lambda index_option: ["A", "B"])
    assert universe.resolve(mode="pkscreener", data_dirs=[str(tmp_path)]) == ["A", "B"]


def test_no_cache_and_no_fetcher_resolves_to_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "from_pkscreener", lambda index_option: [])
    assert universe.resolve(mode="auto", data_dirs=[str(tmp_path)]) is None
