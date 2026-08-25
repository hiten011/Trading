"""Sending alerts to Telegram.

Deliberately talks to the Bot API over plain HTTP rather than going through
PKScreener's notifier: that one is wired to the project's own channel and
silently no-ops unless a pile of upstream environment variables are set. Here a
misconfiguration should be loud.
"""

from __future__ import annotations

import html
import logging
import os
import time
from dataclasses import dataclass
from typing import List, Optional

import requests

LOGGER = logging.getLogger("custom.notify")

API_ROOT = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 4


class TelegramError(RuntimeError):
    """Raised when Telegram rejects a request we cannot retry out of."""


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Split on line boundaries so an HTML block is never cut mid-tag."""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    current: List[str] = []
    length = 0
    for line in text.split("\n"):
        # A single line longer than the limit has to be hard-split.
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, length = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if length + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


@dataclass
class TelegramNotifier:
    token: str
    chat_id: str
    dry_run: bool = False
    session: Optional[requests.Session] = None

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

    # -- plumbing ----------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self.token}/{method}"

    def _post(self, method: str, data: dict, files: Optional[dict] = None) -> dict:
        last_error: Optional[str] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.session.post(
                    self._url(method), data=data, files=files, timeout=REQUEST_TIMEOUT
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                LOGGER.warning("%s attempt %d/%d failed: %s", method, attempt, MAX_ATTEMPTS, exc)
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 429:
                retry_after = int(response.json().get("parameters", {}).get("retry_after", 5))
                LOGGER.warning("Rate limited by Telegram; sleeping %ss", retry_after)
                time.sleep(retry_after + 1)
                continue

            if response.ok:
                return response.json()

            # 4xx other than 429 will not fix themselves on a retry.
            if 400 <= response.status_code < 500:
                raise TelegramError(f"{method} failed [{response.status_code}]: {response.text}")

            last_error = f"[{response.status_code}] {response.text}"
            LOGGER.warning("%s attempt %d/%d failed: %s", method, attempt, MAX_ATTEMPTS, last_error)
            time.sleep(2 ** attempt)

        raise TelegramError(f"{method} failed after {MAX_ATTEMPTS} attempts: {last_error}")

    # -- public API --------------------------------------------------------
    def check(self) -> str:
        """Verify the token and return the bot's @username."""
        if not self.token:
            raise TelegramError("No bot token configured (set TOKEN in secrets/.env.dev)")
        payload = self._post("getMe", {})
        username = payload.get("result", {}).get("username", "<unknown>")
        LOGGER.info("Telegram token belongs to @%s", username)
        return username

    def send_message(self, text: str, parse_mode: str = "HTML", silent: bool = False) -> None:
        if self.dry_run:
            print("\n----- TELEGRAM (dry run) -----")
            print(text)
            print("----- end -----\n")
            return
        if not self.configured:
            raise TelegramError(
                "Telegram is not configured: set TOKEN and chat_idADMIN in secrets/.env.dev"
            )
        for index, chunk in enumerate(split_message(text)):
            self._post(
                "sendMessage",
                {
                    "chat_id": self.chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                    "disable_notification": silent,
                },
            )
            if index:
                time.sleep(0.5)  # stay under Telegram's per-chat message rate

    def send_document(self, file_path: str, caption: str = "") -> None:
        if self.dry_run:
            print(f"----- TELEGRAM (dry run): would attach {file_path} -----")
            return
        if not self.configured:
            raise TelegramError(
                "Telegram is not configured: set TOKEN and chat_idADMIN in secrets/.env.dev"
            )
        if not os.path.isfile(file_path):
            LOGGER.warning("Attachment %s does not exist; skipping", file_path)
            return
        with open(file_path, "rb") as handle:
            self._post(
                "sendDocument",
                {
                    "chat_id": self.chat_id,
                    "caption": caption[:MAX_CAPTION_LENGTH],
                    "parse_mode": "HTML",
                },
                files={"document": (os.path.basename(file_path), handle)},
            )


def escape(text: object) -> str:
    """Escape a value for Telegram's HTML parse mode."""
    return html.escape(str(text), quote=False)
