#!/usr/bin/env python3
"""Telegram <-> Claude Code bridge.

Polls a Telegram bot for messages from an allow-listed set of chat ids,
forwards each one to a local `claude` CLI (print mode, subscription auth
via `claude login` - no API key involved) and relays the reply back.

Supports three message kinds:
  - text: forwarded as-is.
  - photo: downloaded to the working directory's inbox and referenced by
    path in the prompt. Claude Code's own Read tool loads images natively,
    so no separate vision step is needed here.
  - voice/audio: downloaded, converted to wav with ffmpeg and transcribed
    with a local whisper.cpp binary before being forwarded as text.

The add-on container itself has no mount into /mnt/18T or any other
host's filesystem, so "LAN infra awareness" comes from SSH: every
message is prefixed with a short list of the hosts GOD Mode already
manages (from /data/options.json's `hosts`) plus the private key
Claude Code can use to reach them - the same key/inventory the
collector's own Ansible runs already rely on, no new enrollment
needed as long as a host's authorized_keys trusts it.

Config is read from environment variables, falling back to the
add-on's /data/options.json - this also lets the script run standalone
outside the add-on if needed, just by setting env vars instead.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OPTS_FILE = Path("/data/options.json")


def _opt(env_key: str, json_key: str, default=None):
    """env var > /data/options.json > default. Env wins so a standalone
    deployment never needs to fake an add-on options file."""
    env_val = os.environ.get(env_key)
    if env_val is not None and env_val != "":
        return env_val
    if OPTS_FILE.exists():
        try:
            val = json.loads(OPTS_FILE.read_text()).get(json_key)
            if val is not None and val != "":
                return val
        except Exception:
            pass
    return default


def _opt_bool(env_key: str, json_key: str, default: bool) -> bool:
    val = _opt(env_key, json_key, default)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


BOT_TOKEN = _opt("TELEGRAM_BOT_TOKEN", "telegram_bot_token", "")
ALLOWED_CHAT_IDS = {
    c.strip()
    for c in str(_opt("TELEGRAM_ALLOWED_CHAT_IDS", "telegram_allowed_chat_ids", "")).split(",")
    if c.strip()
}
ENABLED = _opt_bool("CLAUDE_BRIDGE_ENABLED", "claude_bridge_enabled", False)
INSTANCE_NAME = _opt("CLAUDE_BRIDGE_INSTANCE_NAME", "instance_name", "god-mode")
CLAUDE_MODEL = _opt("CLAUDE_BRIDGE_MODEL", "claude_model", "")

STATE_DIR = Path(os.environ.get("CLAUDE_BRIDGE_STATE_DIR", "/data/claude-bridge"))
WORKDIR = Path(os.environ.get("CLAUDE_BRIDGE_WORKDIR", str(STATE_DIR / "workspace")))
INBOX_DIR = STATE_DIR / "inbox"
OFFSET_FILE = STATE_DIR / "offset.txt"
CLAUDE_CONFIG_DIR = STATE_DIR / "claude-auth"

WHISPER_BIN = os.environ.get("CLAUDE_BRIDGE_WHISPER_BIN", "")
WHISPER_MODEL = os.environ.get("CLAUDE_BRIDGE_WHISPER_MODEL", "")

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
POLL_TIMEOUT_S = 50
CLAUDE_TIMEOUT_S = 300
TELEGRAM_MAX_LEN = 4000


def log(msg: str) -> None:
    print(f"[god-claude-bridge:{INSTANCE_NAME}] {msg}", flush=True)


def api_call(method: str, params: dict | None = None, files: dict | None = None) -> dict:
    url = f"{API_BASE}/{method}"
    if files:
        # multipart not needed for our calls (we only GET files, never send media)
        raise NotImplementedError
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT_S + 15) as resp:
        return json.loads(resp.read())


def get_updates(offset: int) -> list[dict]:
    try:
        resp = api_call(
            "getUpdates",
            {"offset": offset, "timeout": POLL_TIMEOUT_S, "allowed_updates": json.dumps(["message"])},
        )
    except (urllib.error.URLError, TimeoutError) as e:
        log(f"getUpdates network error: {e}")
        return []
    if not resp.get("ok"):
        log(f"getUpdates failed: {resp}")
        return []
    return resp.get("result", [])


def send_message(chat_id: int, text: str) -> None:
    prefix = f"[{INSTANCE_NAME}]\n"
    text = prefix + text if text else prefix + "(no output)"
    for i in range(0, len(text), TELEGRAM_MAX_LEN):
        chunk = text[i : i + TELEGRAM_MAX_LEN]
        try:
            api_call("sendMessage", {"chat_id": chat_id, "text": chunk})
        except (urllib.error.URLError, TimeoutError) as e:
            log(f"sendMessage failed: {e}")


def download_file(file_id: str, dest: Path) -> bool:
    try:
        info = api_call("getFile", {"file_id": file_id})
        if not info.get("ok"):
            return False
        file_path = info["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        with urllib.request.urlopen(url, timeout=60) as resp:
            dest.write_bytes(resp.read())
        return True
    except (urllib.error.URLError, TimeoutError, KeyError) as e:
        log(f"download_file failed: {e}")
        return False


def transcribe(audio_path: Path) -> str | None:
    if not WHISPER_BIN or not WHISPER_MODEL:
        return None
    wav_path = audio_path.with_suffix(".wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(audio_path), "-ar", "16000", "-ac", "1", str(wav_path)],
            check=True, capture_output=True, timeout=60,
        )
        out = subprocess.run(
            [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", str(wav_path), "-nt", "--language", "auto"],
            check=True, capture_output=True, timeout=120, text=True,
        )
        return out.stdout.strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        log(f"transcription failed: {e}")
        return None
    finally:
        wav_path.unlink(missing_ok=True)


SSH_KEY = Path("/data/.ssh/id_ed25519")


def _lan_context() -> str:
    """Short, cheap-to-build reminder of what's reachable over SSH with
    GOD Mode's own key - the add-on container has no direct filesystem
    access to anything outside itself, so this is how Claude finds out
    what's on the LAN at all."""
    if not OPTS_FILE.exists():
        return ""
    try:
        hosts = json.loads(OPTS_FILE.read_text()).get("hosts", [])
    except Exception:
        return ""
    if not hosts:
        return ""
    lines = [
        f"  - {h.get('name')}: {h.get('addr')} (user {h.get('user', 'root')}, category {h.get('category', '?')})"
        for h in hosts
    ]
    return (
        "You're running inside the GOD Mode Home Assistant add-on. You have no "
        "direct filesystem access outside this container, but you can reach the "
        f"following LAN hosts over SSH using the key at {SSH_KEY} "
        f"(e.g. `ssh -i {SSH_KEY} -o StrictHostKeyChecking=accept-new <user>@<addr>`), "
        "provided that host's authorized_keys already trusts it (every host GOD Mode "
        "monitors does, since the collector's own Ansible runs use this same key):\n"
        + "\n".join(lines)
        + "\n\nIn particular, the user's NAS tree /mnt/18T/MIO/Proyectos is mounted on "
        "the Proxmox host (look for it in the list above, usually named for a pve_node "
        "category) - SSH there to read or edit files in it.\n\n---\n\n"
    )


def ask_claude(prompt: str) -> str:
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(CLAUDE_CONFIG_DIR)
    full_prompt = _lan_context() + prompt
    cmd = ["claude", "-p", full_prompt, "--output-format", "text", "--permission-mode", "bypassPermissions"]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    try:
        out = subprocess.run(
            cmd, cwd=str(WORKDIR), env=env, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return "Timed out waiting for Claude Code to respond."
    if out.returncode != 0:
        log(f"claude exited {out.returncode}: {out.stderr[:500]}")
        if "not logged in" in out.stderr.lower() or "login" in out.stderr.lower():
            return (
                "Claude Code isn't authenticated on this instance yet. "
                "Run `claude login` once in this container/host to fix it."
            )
        return f"Claude Code error: {out.stderr.strip()[:1500] or 'unknown error'}"
    return out.stdout.strip()


def handle_message(msg: dict) -> None:
    chat_id = msg.get("chat", {}).get("id")
    if chat_id is None or str(chat_id) not in ALLOWED_CHAT_IDS:
        log(f"Ignoring message from non-allowed chat {chat_id}")
        return

    prompt = None

    if "text" in msg:
        prompt = msg["text"]

    elif "voice" in msg or "audio" in msg:
        media = msg.get("voice") or msg.get("audio")
        ts = int(time.time() * 1000)
        raw_path = INBOX_DIR / f"voice_{ts}.oga"
        if not download_file(media["file_id"], raw_path):
            send_message(chat_id, "Couldn't download the voice message.")
            return
        transcript = transcribe(raw_path)
        raw_path.unlink(missing_ok=True)
        if transcript is None:
            send_message(
                chat_id,
                "Voice message received, but this instance has no transcription "
                "configured (set CLAUDE_BRIDGE_WHISPER_BIN/_MODEL).",
            )
            return
        prompt = transcript

    elif "photo" in msg:
        sizes = msg["photo"]
        biggest = max(sizes, key=lambda s: s.get("file_size", 0))
        ts = int(time.time() * 1000)
        img_path = INBOX_DIR / f"photo_{ts}.jpg"
        if not download_file(biggest["file_id"], img_path):
            send_message(chat_id, "Couldn't download the photo.")
            return
        caption = msg.get("caption", "").strip()
        prompt = (
            f"{caption}\n\n" if caption else ""
        ) + f"There is an image at {img_path} - read it and respond to the request above (or just describe it if there's no request)."

    if not prompt:
        return

    log(f"chat {chat_id}: {prompt[:120]!r}")
    reply = ask_claude(prompt)
    send_message(chat_id, reply)


def main() -> None:
    if not BOT_TOKEN:
        log("No TELEGRAM_BOT_TOKEN configured, idling.")
        while True:
            time.sleep(300)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    CLAUDE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    offset = 0
    if OFFSET_FILE.exists():
        try:
            offset = int(OFFSET_FILE.read_text().strip())
        except ValueError:
            offset = 0

    log(f"Starting. enabled={ENABLED} allowed_chats={len(ALLOWED_CHAT_IDS)} workdir={WORKDIR}")

    while True:
        if not _opt_bool("CLAUDE_BRIDGE_ENABLED", "claude_bridge_enabled", False):
            time.sleep(60)
            continue
        if not ALLOWED_CHAT_IDS:
            log("claude_bridge_enabled but no allowed chat ids configured, idling.")
            time.sleep(60)
            continue

        updates = get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if msg:
                try:
                    handle_message(msg)
                except Exception as e:  # keep polling even if one message blows up
                    log(f"handle_message crashed: {e}")
            OFFSET_FILE.write_text(str(offset))


if __name__ == "__main__":
    sys.exit(main())
