import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Keep a developer's real .env.dev / PKS_* vars out of the tests.

    Blocks both routes a real secret could leak in: environment variables,
    and the actual secrets/.env.dev file sitting in the repo (which is real
    once anyone has followed the setup instructions). tests/test_live_telegram.py
    is the one deliberate exception -- it overrides this fixture locally
    because it specifically wants your real credentials.
    """
    for key in list(os.environ):
        if key.startswith("PKS_") or key in (
            "TOKEN", "CHAT_ID", "chat_idADMIN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        ):
            monkeypatch.delenv(key, raising=False)

    import custom.config as config

    monkeypatch.setattr(config, "ENV_DEV_CANDIDATES", ())


@pytest.fixture
def fake_strategies(monkeypatch):
    """Register throwaway strategy modules without shipping them to users.

    ``importlib.import_module`` returns anything already in ``sys.modules``, so
    a module registered under ``custom.strategies.<name>`` loads exactly like a
    real file would -- including the contract checks in ``strategies.load``.
    """
    import sys
    import types

    import pandas as pd

    from custom.strategies.base import Signal

    def register(name, evaluate=None, **attributes):
        module = types.ModuleType(f"custom.strategies.{name}")
        module.NAME = attributes.pop("NAME", name)
        module.DESCRIPTION = attributes.pop("DESCRIPTION", "test double")
        if evaluate is not None:
            module.evaluate = evaluate
        for key, value in attributes.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, f"custom.strategies.{name}", module)
        return module

    def never(symbol, df):
        return None

    def always(symbol, df):
        # Score varies by symbol so ranking is observable.
        return Signal(symbol, "BUY", float(df["Close"].iloc[-1]), "always", score=len(symbol))

    def boom(symbol, df):
        raise ValueError("indicator blew up")

    def wrong_type(symbol, df):
        return "not a Signal"

    register("never_matches", never)
    register("always_matches", always)
    register("always_raises", boom)
    register("returns_wrong_type", wrong_type)
    register("no_evaluate")
    return register
