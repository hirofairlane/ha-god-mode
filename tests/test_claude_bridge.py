"""Tests for god-claude-bridge.py's pure logic: config precedence, the
chat-id allow-list gate, and message chunking. Network calls (Telegram API,
`claude` subprocess) are stubbed out - nothing here touches the network.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
BRIDGE_PATH = RAIZ / "god_mode" / "rootfs" / "usr" / "bin" / "god-claude-bridge.py"


def _load_bridge(monkeypatch, env: dict[str, str]):
    """Fresh import with a given environment - the module reads env vars
    (and /data/options.json) at import time into module-level constants."""
    for key in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "CLAUDE_BRIDGE_ENABLED",
        "CLAUDE_BRIDGE_INSTANCE_NAME", "CLAUDE_BRIDGE_MODEL", "CLAUDE_BRIDGE_STATE_DIR",
        "CLAUDE_BRIDGE_WORKDIR", "CLAUDE_BRIDGE_WHISPER_BIN", "CLAUDE_BRIDGE_WHISPER_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    spec = importlib.util.spec_from_file_location("god_claude_bridge_test", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_env_overrides_options_json(monkeypatch, tmp_path):
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"telegram_bot_token": "from-json", "instance_name": "json-name"}))
    mod = _load_bridge(monkeypatch, {"TELEGRAM_BOT_TOKEN": "from-env"})
    mod.OPTS_FILE = opts
    assert mod._opt("TELEGRAM_BOT_TOKEN", "telegram_bot_token") == "from-env"
    assert mod._opt("CLAUDE_BRIDGE_INSTANCE_NAME", "instance_name") == "json-name"


def test_opt_falls_back_to_default_when_unset(monkeypatch, tmp_path):
    mod = _load_bridge(monkeypatch, {})
    mod.OPTS_FILE = tmp_path / "missing.json"
    assert mod._opt("CLAUDE_BRIDGE_MODEL", "claude_model", "fallback") == "fallback"


def test_allowed_chat_ids_parsed_from_env(monkeypatch):
    mod = _load_bridge(monkeypatch, {"TELEGRAM_ALLOWED_CHAT_IDS": "111, 222,333"})
    assert mod.ALLOWED_CHAT_IDS == {"111", "222", "333"}


def test_handle_message_ignores_disallowed_chat(monkeypatch):
    mod = _load_bridge(monkeypatch, {"TELEGRAM_ALLOWED_CHAT_IDS": "111"})
    calls = []
    monkeypatch.setattr(mod, "ask_claude", lambda prompt: calls.append(prompt) or "reply")
    monkeypatch.setattr(mod, "send_message", lambda chat_id, text: calls.append((chat_id, text)))
    mod.handle_message({"chat": {"id": 999}, "text": "hola"})
    assert calls == []


def test_handle_message_allowed_chat_forwards_text_to_claude(monkeypatch):
    mod = _load_bridge(monkeypatch, {"TELEGRAM_ALLOWED_CHAT_IDS": "111"})
    seen = {}

    def _ask_claude(prompt):
        seen["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(mod, "ask_claude", _ask_claude)
    sent = []
    monkeypatch.setattr(mod, "send_message", lambda chat_id, text: sent.append((chat_id, text)))
    mod.handle_message({"chat": {"id": 111}, "text": "status del cluster?"})
    assert seen["prompt"] == "status del cluster?"
    assert sent == [(111, "ok")]


def test_handle_message_photo_without_download_warns(monkeypatch):
    mod = _load_bridge(monkeypatch, {"TELEGRAM_ALLOWED_CHAT_IDS": "111"})
    monkeypatch.setattr(mod, "download_file", lambda file_id, dest: False)
    monkeypatch.setattr(mod, "ask_claude", lambda prompt: pytest.fail("should not be called"))
    sent = []
    monkeypatch.setattr(mod, "send_message", lambda chat_id, text: sent.append((chat_id, text)))
    mod.handle_message({"chat": {"id": 111}, "photo": [{"file_id": "x", "file_size": 10}]})
    assert sent and "photo" in sent[0][1].lower()


def test_handle_message_voice_without_transcriber_warns(monkeypatch):
    mod = _load_bridge(monkeypatch, {"TELEGRAM_ALLOWED_CHAT_IDS": "111"})
    monkeypatch.setattr(mod, "download_file", lambda file_id, dest: True)
    monkeypatch.setattr(mod, "transcribe", lambda path: None)
    monkeypatch.setattr(mod, "ask_claude", lambda prompt: pytest.fail("should not be called"))
    sent = []
    monkeypatch.setattr(mod, "send_message", lambda chat_id, text: sent.append((chat_id, text)))
    mod.handle_message({"chat": {"id": 111}, "voice": {"file_id": "x"}})
    assert sent and "transcription" in sent[0][1].lower()


def test_send_message_chunks_long_text(monkeypatch):
    mod = _load_bridge(monkeypatch, {"CLAUDE_BRIDGE_INSTANCE_NAME": "test-instance"})
    calls = []
    monkeypatch.setattr(mod, "api_call", lambda method, params=None, files=None: calls.append(params) or {"ok": True})
    long_text = "x" * (mod.TELEGRAM_MAX_LEN * 2 + 10)
    mod.send_message(42, long_text)
    assert len(calls) == 3
    assert calls[0]["text"].startswith("[test-instance]")
    assert all(c["chat_id"] == 42 for c in calls)
