"""Settings resolution and the Telegram transport."""

import pytest

from custom import config
from custom.notify import MAX_MESSAGE_LENGTH, TelegramError, TelegramNotifier, escape, split_message


# --- chat id handling ------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5058733760", "5058733760"),          # personal id, left alone
        ("-1001785195297", "-1001785195297"),  # already signed
        ("1001785195297", "-1001785195297"),   # PKScreener's unsigned channel form
        ("  1001785195297  ", "-1001785195297"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalise_chat_id(raw, expected):
    assert config.normalise_chat_id(raw) == expected


# --- env file parsing ------------------------------------------------------

def test_read_env_file_strips_the_quotes_pkscreener_writes(tmp_path):
    path = tmp_path / ".env.dev"
    path.write_text("TOKEN='123:abc'\nchat_idADMIN=\"456\"\n# comment\n\nCHAT_ID=789\n")
    values = config.read_env_file(str(path))
    assert values["TOKEN"] == "123:abc"
    assert values["chat_idADMIN"] == "456"
    assert values["CHAT_ID"] == "789"


def test_read_env_file_on_a_missing_file_is_empty():
    assert config.read_env_file("/nonexistent/.env.dev") == {}


def test_environment_variables_beat_the_file(tmp_path, monkeypatch):
    path = tmp_path / ".env.dev"
    path.write_text("TOKEN='from-file'\n")
    monkeypatch.setenv("TOKEN", "from-env")
    assert config.load_secrets(str(path))["TOKEN"] == "from-env"


# --- settings --------------------------------------------------------------

def test_settings_read_the_environment(monkeypatch):
    monkeypatch.setenv("PKS_STRATEGY", "my_indicator")
    monkeypatch.setenv("PKS_MIN_PRICE", "50")
    monkeypatch.setenv("PKS_RUN_AT", "09:05, 15:45")
    monkeypatch.setenv("PKS_DRY_RUN", "yes")
    settings = config.Settings.from_env()
    assert settings.strategy == "my_indicator"
    assert settings.min_price == 50.0
    assert settings.run_at == ["09:05", "15:45"]
    assert settings.dry_run is True


def test_settings_survive_garbage_values(monkeypatch):
    monkeypatch.setenv("PKS_MIN_PRICE", "not-a-number")
    monkeypatch.setenv("PKS_LOOKBACK_DAYS", "")
    settings = config.Settings.from_env()
    assert settings.min_price == 20.0
    assert settings.lookback_days == 250


def test_telegram_configured_needs_both_halves():
    settings = config.Settings(secrets={"TOKEN": "123:abc"})
    assert settings.telegram_configured is False
    settings.secrets["chat_idADMIN"] = "456"
    assert settings.telegram_configured is True


def test_describe_masks_the_token():
    settings = config.Settings(secrets={"TOKEN": "8123456789:AAFsecretsecret", "chat_idADMIN": "42"})
    described = settings.describe()
    assert "AAFsecretsecret" not in described
    assert "812345" in described


# --- message splitting -----------------------------------------------------

def test_short_messages_are_not_split():
    assert split_message("hello") == ["hello"]


def test_split_message_respects_the_limit():
    text = "\n".join(f"line {index}" for index in range(2000))
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)


def test_split_message_keeps_every_line():
    text = "\n".join(f"line {index}" for index in range(2000))
    assert "\n".join(chunks := split_message(text)).count("line ") == 2000
    assert len(chunks) >= 2


def test_split_message_hard_splits_one_very_long_line():
    chunks = split_message("x" * 10000)
    assert all(len(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)
    assert "".join(chunks) == "x" * 10000


def test_escape_neutralises_html():
    assert escape("A & B <tag>") == "A &amp; B &lt;tag&gt;"


# --- notifier --------------------------------------------------------------

def test_dry_run_prints_and_never_calls_telegram(capsys):
    notifier = TelegramNotifier(token="", chat_id="", dry_run=True)
    notifier.send_message("hello there")
    assert "hello there" in capsys.readouterr().out


def test_unconfigured_notifier_raises_rather_than_silently_dropping():
    notifier = TelegramNotifier(token="", chat_id="")
    with pytest.raises(TelegramError, match="not configured"):
        notifier.send_message("hello")


def test_check_without_a_token_raises():
    with pytest.raises(TelegramError, match="No bot token"):
        TelegramNotifier(token="", chat_id="1").check()


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload or {"ok": True, "result": {}}
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data=None, files=None, timeout=None):
        self.calls.append((url, data, files))
        return self.responses.pop(0)


def test_send_message_posts_once_per_chunk():
    text = "\n".join(f"line {index}" for index in range(600))
    expected = len(split_message(text))
    assert expected > 1, "test needs a message long enough to split"

    session = _FakeSession([_FakeResponse() for _ in range(expected)])
    notifier = TelegramNotifier(token="t", chat_id="42", session=session)
    notifier.send_message(text)

    assert len(session.calls) == expected
    assert session.calls[0][1]["chat_id"] == "42"
    assert "sendMessage" in session.calls[0][0]


def test_send_message_posts_once_for_a_short_message():
    session = _FakeSession([_FakeResponse()])
    notifier = TelegramNotifier(token="t", chat_id="42", session=session)
    notifier.send_message("short")
    assert len(session.calls) == 1


def test_a_4xx_is_not_retried():
    session = _FakeSession([_FakeResponse(400, text="chat not found")])
    notifier = TelegramNotifier(token="t", chat_id="42", session=session)
    with pytest.raises(TelegramError, match="chat not found"):
        notifier.send_message("hello")
    assert len(session.calls) == 1


def test_check_returns_the_bot_username():
    session = _FakeSession([_FakeResponse(payload={"ok": True, "result": {"username": "my_bot"}})])
    notifier = TelegramNotifier(token="t", chat_id="42", session=session)
    assert notifier.check() == "my_bot"


# --- data directories ------------------------------------------------------

def test_data_dirs_splits_the_search_path():
    settings = config.Settings(data_dir="/a/results/Data:/a/actions-data-download")
    assert settings.data_dirs == ["/a/results/Data", "/a/actions-data-download"]


def test_data_dirs_handles_a_single_directory():
    settings = config.Settings(data_dir="/a/results/Data")
    assert settings.data_dirs == ["/a/results/Data"]


def test_reports_dir_sits_next_to_the_primary_data_dir():
    settings = config.Settings(data_dir="/PKScreener-main/results/Data:/other")
    assert settings.reports_dir == "/PKScreener-main/results/Reports"
