#!/usr/bin/env python3
"""Hermes shell hook → Touch Bar AI mode.

Subscribed to two events in ~/.hermes/config.yaml:

  on_human_input_request   the agent is about to block on a person
                           (kind = sudo | clarify | approval)
  on_human_input_resolved  that wait ended (answered / timeout / withdrawn …)

On a request it flips the Touch Bar into AI mode and writes the prompt into the
status area; when the last pending prompt resolves it returns to the normal bar.
A small counter in the state dir keeps concurrent prompts from clearing each
other's text.

Payload arrives as JSON on stdin; stdout is ignored for these observer events.
Any failure is swallowed: a hook must never disturb the agent loop.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
STATE = Path(os.environ.get("XDG_STATE_HOME", str(HOME / ".local/state"))) / "t1-touchbar"
PENDING = STATE / "ai-pending.json"
T1TB = HOME / ".local/bin/t1tb"
LOG = STATE / "ai-hook.log"

KIND_LABEL = {
    "sudo": "需要密码",
    "clarify": "需要回答",
    "approval": "等待审批",
}

# Voice announcement: the prompt is read out so a blocked agent is noticed even
# when you are not looking at the bar. Synth + playback run detached — the hook
# has a 5 s budget and edge-tts alone takes ~1 s.
#
# It is strictly optional. Priority: $T1TTS_SPEAK > "speak" in the tts config >
# default on. Set either to false/none/0 to keep the bar silent.
SAY = HOME / ".local/share/t1-touchbar/tts/say.py"
TTS_CONFIG = HOME / ".local/share/t1-touchbar/tts/tts.json"
TRUTHY = ("1", "true", "yes", "on")

# The Touch Bar shows two lines of ~26 CJK chars; keep the head of the prompt.
MAX_CHARS = 110
# A prompt whose resolution never arrives (crashed turn, killed TUI) must not pin
# AI mode forever.
STALE_SECONDS = 600


def log(msg):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def t1tb(*args):
    try:
        subprocess.run([str(T1TB), *args], capture_output=True, timeout=3)
    except Exception as exc:
        log(f"t1tb {args} failed: {exc}")


def speak_enabled():
    env = os.environ.get("T1TTS_SPEAK")
    if env is not None:
        return env.strip().lower() in TRUTHY
    try:
        cfg = json.loads(TTS_CONFIG.read_text(encoding="utf-8"))
        if isinstance(cfg, dict) and "speak" in cfg:
            value = cfg["speak"]
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in TRUTHY
    except (OSError, ValueError):
        pass
    return True


def speak(text):
    if not text or not speak_enabled():
        return
    try:
        subprocess.Popen([str(SAY), text], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        log(f"say failed: {exc}")


def clean(text):
    """One-line, length-bounded, control-character-free text for the bar."""
    flat = " ".join(str(text or "").split())
    if len(flat) > MAX_CHARS:
        flat = flat[: MAX_CHARS - 1] + "…"
    return flat


def load_pending():
    try:
        data = json.loads(PENDING.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
    except (OSError, ValueError):
        return {}
    now = time.time()
    fresh = {}
    for key, entry in data.items():
        since = entry.get("epoch") if isinstance(entry, dict) else None
        if isinstance(since, (int, float)) and now - since > STALE_SECONDS:
            continue
        fresh[key] = entry
    return fresh


def save_pending(data):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        PENDING.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log(f"pending write failed: {exc}")


def on_request(payload, extra):
    kind = str(extra.get("kind") or "approval")
    request_id = str(extra.get("request_id") or "")
    prompt = clean(extra.get("prompt"))
    label = KIND_LABEL.get(kind, "等待确认")

    pending = load_pending()
    # `hermes hooks test` fires with no request_id; key those by kind so a
    # repeated synthetic run does not stack up phantom pending prompts.
    key = request_id or f"anon:{kind}"
    pending[key] = {
        "kind": kind,
        "prompt": prompt,
        "since": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "epoch": time.time(),
    }
    save_pending(pending)

    text = f"{label}：{prompt}" if prompt else label
    t1tb("ai", "-t", text)
    spoke = speak_enabled()
    speak(text)
    log(f"request kind={kind} id={request_id} pending={len(pending)} speak={spoke}")


def on_resolved(extra):
    request_id = str(extra.get("request_id") or "")
    outcome = str(extra.get("outcome") or "")
    pending = load_pending()
    if request_id:
        pending.pop(request_id, None)
    else:
        # No id to match on: clear every synthetic entry, since the paired
        # request could not be identified either.
        for key in [k for k in pending if k.startswith("anon:")]:
            pending.pop(key, None)
    if pending:
        # Another prompt is still waiting: show the newest one instead of
        # dropping back to the normal bar.
        newest = sorted(pending.values(), key=lambda item: item.get("since", ""))[-1]
        label = KIND_LABEL.get(str(newest.get("kind")), "等待确认")
        prompt = newest.get("prompt") or ""
        t1tb("ai", "-t", f"{label}：{prompt}" if prompt else label)
    else:
        t1tb("normal")
    save_pending(pending)
    log(f"resolved id={request_id} outcome={outcome} pending={len(pending)}")


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        log(f"bad payload: {exc}")
        return 0
    event = str(payload.get("hook_event_name") or "")
    extra = payload.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    try:
        if event == "on_human_input_request":
            on_request(payload, extra)
        elif event == "on_human_input_resolved":
            on_resolved(extra)
    except Exception as exc:
        log(f"{event} failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
