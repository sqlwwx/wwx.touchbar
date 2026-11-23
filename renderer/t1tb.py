#!/usr/bin/env python3
"""t1tb — talk to the wwx.touchbar-ai renderer, and switch its voice on/off.

  t1tb ai [-t TEXT]     enter AI mode (optionally set the status text)
  t1tb text -t TEXT     set the status text and show AI mode
  t1tb normal           back to the normal bar
  t1tb toggle           flip modes
  t1tb status           print mode + text
  t1tb quit             stop the renderer (the omarchy service restarts it)
  t1tb voice on|off|    turn the spoken prompt announcement on or off (no arg
       [toggle]|status  prints the current state)
"""

import json
import os
import socket
import sys
from pathlib import Path

SOCK = os.path.expanduser("~/.local/state/t1-touchbar/ai.sock")
TTS_CONFIG = Path.home() / ".local/share/t1-touchbar/tts/tts.json"
TRUTHY = ("1", "true", "yes", "on")


def send(req):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2.0)
    client.connect(SOCK)
    client.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = client.recv(4096)
        if not chunk:
            break
        buf += chunk
    client.close()
    return json.loads(buf.decode("utf-8") or "{}")


def read_tts():
    try:
        data = json.loads(TTS_CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_tts(cfg):
    TTS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    TTS_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def voice_state():
    """Mirror the hook's precedence: $T1TTS_SPEAK wins over tts.json."""
    env = os.environ.get("T1TTS_SPEAK")
    if env is not None:
        return env.strip().lower() in TRUTHY, "env T1TTS_SPEAK"
    cfg = read_tts()
    if "speak" in cfg:
        value = cfg["speak"]
        on = value if isinstance(value, bool) else str(value).strip().lower() in TRUTHY
        return on, str(TTS_CONFIG)
    return True, "default"


def voice(argv):
    cfg = read_tts()
    arg = argv[0].lower() if argv else ""
    current, source = voice_state()
    if arg in ("status", ""):
        print(json.dumps({"speak": current, "source": source}, ensure_ascii=False))
        return 0
    if arg in ("on", "1", "true", "yes"):
        cfg["speak"] = True
    elif arg in ("off", "0", "false", "no"):
        cfg["speak"] = False
    elif arg == "toggle":
        cfg["speak"] = not current
    else:
        print("usage: t1tb voice on|off|toggle|status", file=sys.stderr)
        return 2
    write_tts(cfg)
    now, source = voice_state()
    print(json.dumps({"speak": now, "source": source}, ensure_ascii=False))
    return 0


def usage(code=0):
    print(__doc__.strip())
    raise SystemExit(code)


def main(argv):
    if not argv:
        usage(1)
    cmd = argv[0]
    if cmd == "voice":
        return voice(argv[1:])
    text = None
    if "-t" in argv:
        text = argv[argv.index("-t") + 1]
    try:
        if cmd == "ai":
            req = {"cmd": "ai"}
            if text is not None:
                req["text"] = text
        elif cmd == "text":
            if text is None:
                usage(1)
            req = {"cmd": "text", "text": text}
        elif cmd in ("normal", "toggle", "status", "quit"):
            req = {"cmd": cmd}
        elif cmd in ("-h", "--help", "help"):
            usage()
        else:
            usage(1)
        reply = send(req)
    except FileNotFoundError:
        print(f"renderer not running ({SOCK} missing)", file=sys.stderr)
        return 1
    except ConnectionRefusedError:
        print(f"renderer not listening on {SOCK}", file=sys.stderr)
        return 1
    if not reply.get("ok"):
        print(json.dumps(reply, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(reply, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))