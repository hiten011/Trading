"""
LIVE check: actually calls the real Telegram Bot API.

Every other test in this suite is hermetic -- synthetic candles, a fake
Telegram session, no network, no secrets. This file is the one deliberate
exception: it exists so `make test` can prove, on demand, that a real message
reaches your real phone -- not just that our code *would* format one correctly.

It skips itself (not fails) when no credentials are available, so the default
`pytest tests` run stays safe for a fresh clone, a contributor without your
bot token, or a pull request from a fork:

    secrets/.env.dev      TOKEN + chat_idADMIN           (local)
    environment            TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   (CI)

Run just this file to check credentials without the rest of the suite:
    pytest tests/test_live_telegram.py -v -rs
"""

from __future__ import annotations

import os

import pytest

from custom.config import Settings
from custom.notify import TelegramError, TelegramNotifier


@pytest.fixture(autouse=True)
def _clean_env():
    """Override conftest's _clean_env: this module deliberately wants your
    real credentials, from the real environment and the real secrets/.env.dev."""
    yield


@pytest.fixture(scope="module")
def live_settings():
    # This module's own _clean_env override above means conftest's version
    # never runs for these tests, so ENV_DEV_CANDIDATES is untouched here --
    # a plain from_env() sees your real environment and real .env.dev file.
    settings = Settings.from_env()
    if not settings.telegram_configured:
        pytest.skip(
            "No Telegram credentials configured -- set TOKEN + chat_idADMIN in "
            "secrets/.env.dev, or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in the "
            "environment, to run this live check."
        )
    return settings


@pytest.fixture(scope="module")
def live_notifier(live_settings):
    return TelegramNotifier(token=live_settings.telegram_token, chat_id=live_settings.telegram_chat_id)


def test_the_bot_token_is_valid(live_notifier):
    """getMe: confirms TOKEN is real and belongs to a live bot."""
    username = live_notifier.check()
    assert username


def test_a_real_message_is_delivered(live_notifier):
    """The actual ask: send a real message so you can see it land in Telegram."""
    origin = os.environ.get("GITHUB_RUN_ID")
    where = f"GitHub Actions (run {origin})" if origin else "make test"
    try:
        live_notifier.send_message(
            f"<b>PKScreener alerts: test suite passed.</b>\nSent by: {where}"
        )
    except TelegramError as exc:
        pytest.fail(
            f"Telegram is configured (token + chat id both present) but delivery "
            f"failed: {exc}. If this says 'chat not found', message the bot "
            f"once from that chat first -- Telegram won't let a bot speak first."
        )
