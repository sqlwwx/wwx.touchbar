#!/usr/bin/env python3
"""
t1tb — 从 shell 里完成触摸条按键能做的所有事。按键执行 `cmd: t1tb …`。

目标窗口：默认作用于当前聚焦窗口；agent 触发时由 hook 锁定触发者窗口。
所有交互命令都可带 -w <address> 显式指定窗口（ai.sock status 会回报当前锁定值）。

渲染器命令（需要条形界面，通过 ~/.local/state/t1-touchbar/ai.sock 通信）：
  t1tb goto LABEL [-t TEXT] [-w ADDR]  按 label 跳到任意树节点（t1tb goto AI）
  t1tb text -t TEXT          设置 agent 文字，留在当前层
  t1tb root                  回到根层并清除锁定的窗口（别名：normal）
  t1tb status | quit
  t1tb insert TEXT [-n] [-w ADDR]   聚焦目标窗口并粘贴 TEXT；-n 不按回车
  t1tb close [-w ADDR]       关闭目标窗口（先礼貌请求，不行再 kill）
  t1tb finish                收尾：第一下贴入收尾提示词并 arm，
                             10 分钟内第二下关闭 agent 窗口
  t1tb key NAME              向聚焦窗口发送真实按键（f1..f12、esc）
  t1tb display up|down       面板背光调一档（T1 STEP_DISPLAY）

本地命令（不需要 socket）：
  t1tb open CMD [ARGS...]    在新 session 里启动命令（按钮上配「打开什么」）
  t1tb volume up|down|mute [N] [--quiet]   默认步长 6%，除非 --quiet 否则显示 OSD
  t1tb volume status
  t1tb kbd up|down|toggle|off [--quiet]    键盘背光，除非 --quiet 否则显示 OSD
  t1tb kbd status
  t1tb voice on|off|[toggle]|status        语音播报提示词
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

SOCK = os.path.expanduser("~/.local/state/t1-touchbar/ai.sock")
STATE = Path.home() / ".local/state/t1-touchbar"
TTS_CONFIG = Path.home() / ".local/share/t1-touchbar/tts/tts.json"
KBD_LED = Path("/sys/class/leds/spi::kbd_backlight/brightness")
KBD_MAX = Path("/sys/class/leds/spi::kbd_backlight/max_brightness")
KBD_STATE = STATE / "kbd-backlight.json"
KBD_STEP = 51  # 255 的 20%
TRUTHY = ("1", "true", "yes", "on")
# `t1tb finish` 是两次点击的动作：第一下贴入收尾提示词并置上这个标记，
# 第二下才关窗。标记会过期，避免十分钟前的旧标记关掉你刚开的窗口。
FINISH_STATE = STATE / "finish-armed.json"
FINISH_TTL = 600

FINISH_PROMPT = (
    "收尾三件事：①有值得沉淀的做法就整理成技能；②有值得留档的结论就写进 ~/wiki；"
    "③做完就停，不要继续展开。只回一行结果（做了/没做），不要复述过程、不要列清单、"
    "不要总结对话。"
)


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


def osd(icon=None, percent=None, message=None):
    cmd = ["omarchy-osd", "-d", "1200"]
    if icon:
        cmd += ["-i", icon]
    if percent is None:
        cmd += ["-m", message or "已调整"]
    else:
        cmd += ["-p", str(percent)]
    subprocess.run(cmd, capture_output=True)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def open_app(argv):
    """在新 session 里启动命令，供按钮配置「打开什么」。"""
    if not argv:
        print("open needs a command", file=sys.stderr)
        return 1
    try:
        subprocess.Popen(argv, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        print(f"open failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"open": " ".join(argv)}, ensure_ascii=False))
    return 0


# ---------- 音量 ----------

def volume_level():
    try:
        out = run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]).stdout
        level = float(out.split()[1])
        return level, "MUTED" in out
    except (IndexError, ValueError):
        return None, False


def volume(argv):
    quiet = "--quiet" in argv
    args = [a for a in argv if not a.startswith("--")]
    action = args[0] if args else ""
    step = 6
    if action in ("up", "down") and len(args) > 1:
        try:
            step = abs(int(args[1]))
        except ValueError:
            step = 6
    if action == "status":
        level, muted = volume_level()
        print(json.dumps({"level": level, "muted": muted}))
        return 0
    if action in ("up", "down"):
        delta = f"{step}%+" if action == "up" else f"{step}%-"
        run(["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", delta])
        level, muted = volume_level()
        if not quiet:
            pct = int(round((level or 0) * 100))
            osd("audio-volume-muted" if muted else "audio-volume-high", pct)
        return 0
    if action == "mute":
        run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
        if not quiet:
            level, muted = volume_level()
            if muted:
                osd("audio-volume-muted", None)
            else:
                osd("audio-volume-high", int(round((level or 0) * 100)))
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 1


# ---------- 键盘背光 ----------

def kbd_levels():
    try:
        cur = int(KBD_LED.read_text().strip())
        top = int(KBD_MAX.read_text().strip())
    except (OSError, ValueError):
        return None, None
    return cur, max(top, 1)


def kbd_set(level):
    try:
        KBD_LED.write_text(f"{level}\n")
        return level
    except PermissionError:
        res = subprocess.run(["sudo", "-n", "tee", str(KBD_LED)],
                             input=f"{level}\n", capture_output=True, text=True)
        if res.returncode != 0:
            return None
        return level
    except OSError:
        return None


def kbd_last():
    try:
        return int(json.loads(KBD_STATE.read_text()).get("last") or 0)
    except (OSError, ValueError):
        return 0


def kbd_save_last(level):
    try:
        KBD_STATE.parent.mkdir(parents=True, exist_ok=True)
        KBD_STATE.write_text(json.dumps({"last": int(level)}))
    except OSError:
        pass


def kbd_osd(level, quiet):
    if quiet:
        return
    if level is None:
        osd(None, None, "键盘背光不可用")
        return
    _cur, top = kbd_levels()
    if level == 0:
        osd("input-keyboard", None, "键盘背光 关")
    else:
        osd("input-keyboard", int(round(level * 100 / (top or 255))))


def kbd(argv):
    quiet = "--quiet" in argv
    action = next((a for a in argv if not a.startswith("--")), "")
    cur, top = kbd_levels()
    if action == "status":
        print(json.dumps({"level": cur, "max": top}))
        return 0
    if cur is None:
        kbd_osd(None, quiet)
        return 1
    if action == "up":
        level = kbd_set(min(top, cur + KBD_STEP))
    elif action == "down":
        level = kbd_set(max(0, cur - KBD_STEP))
    elif action == "off":
        level = kbd_set(0)
    elif action == "toggle":
        if cur > 0:
            kbd_save_last(cur)
            level = kbd_set(0)
        else:
            level = kbd_set(kbd_last() or top // 2)
    else:
        print(__doc__.strip(), file=sys.stderr)
        return 1
    kbd_osd(level, quiet)
    return 0


# ---------- 语音 ----------

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
    """沿用 hook 的优先级：$T1TTS_SPEAK 优先于 tts.json。"""
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
        return 1
    write_tts(cfg)
    now, _ = voice_state()
    osd("audio-volume-high" if now else "audio-volume-muted", None,
        "语音播报 开" if now else "语音播报 关")
    print(json.dumps({"speak": now}, ensure_ascii=False))
    return 0


def finish_armed():
    try:
        data = json.loads(FINISH_STATE.read_text())
        return (time.time() - float(data.get("at") or 0)) < FINISH_TTL
    except (OSError, ValueError, TypeError):
        return False


def finish_arm():
    STATE.mkdir(parents=True, exist_ok=True)
    FINISH_STATE.write_text(json.dumps({"at": time.time()}))


def finish_disarm():
    try:
        FINISH_STATE.unlink()
    except OSError:
        pass


def finish():
    """两次点击：先收尾，再关闭。第一下绝不关窗。"""
    if finish_armed():
        finish_disarm()
        reply = send({"cmd": "close"})
        print(json.dumps({"finish": "closed", **reply}, ensure_ascii=False))
        return 0 if reply.get("ok") else 1
    finish_arm()
    reply = send({"cmd": "insert", "text": FINISH_PROMPT, "submit": True})
    print(json.dumps({"finish": "wrapping-up", **reply}, ensure_ascii=False))
    return 0 if reply.get("ok") else 1


def usage(code=0):
    print(__doc__.strip())
    raise SystemExit(code)


def main(argv):
    if not argv:
        usage(1)
    cmd = argv[0]
    if cmd in ("volume", "kbd", "voice"):
        return {"volume": volume, "kbd": kbd, "voice": voice}[cmd](argv[1:])
    if cmd == "finish":
        return finish()
    if cmd == "open":
        return open_app(argv[1:])

    def flag(name):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None

    text = flag("-t")
    window = flag("-w")
    try:
        if cmd == "text":
            if text is None:
                usage(1)
            req = {"cmd": "text", "text": text}
        elif cmd in ("normal", "status", "quit", "root", "close"):
            req = {"cmd": cmd}
            if window and cmd == "close":
                req["window"] = window
        elif cmd == "goto":
            label = next((a for a in argv[1:]
                          if not a.startswith("-") and a not in (text, window)), "")
            if not label:
                usage(1)
            req = {"cmd": "goto", "label": label}
            if text is not None:
                req["text"] = text
            if window:
                req["window"] = window
        elif cmd == "insert":
            skip = {"-n", "-t", "-w"}
            rest = []
            args = argv[1:]
            i = 0
            while i < len(args):
                if args[i] in skip:
                    i += 2 if args[i] in ("-t", "-w") else 1
                else:
                    rest.append(args[i])
                    i += 1
            if not rest:
                usage(1)
            req = {"cmd": "insert", "text": " ".join(rest), "submit": "-n" not in argv}
            if window:
                req["window"] = window
        elif cmd == "key":
            if len(argv) < 2:
                usage(1)
            req = {"cmd": "key", "name": argv[1]}
        elif cmd == "display":
            action = argv[1] if len(argv) > 1 else ""
            if action not in ("up", "down"):
                usage(1)
            req = {"cmd": "display", "up": action == "up"}
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
