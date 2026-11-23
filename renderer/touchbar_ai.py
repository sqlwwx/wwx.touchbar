#!/home/wwx/.local/share/t1-touchbar/venv/bin/python3
"""wwx.touchbar-ai — Touch Bar UI for the Omarchy agent (t1bridge renderer).

Owns /run/t1bridge/touchbar.sock and draws ONE tree, one level at a time:

  root    esc | <root entries>                                      [| AI›]
  deeper  esc | [agent text] | <level entries> | <level key ‹name>

An entry with `children` is a branch: tapping it descends. The rightmost key
while descended is the level key — it carries the name of the level you are in
and pops back up. At the root there is no level key: every entry, the `AI`
branch included, is drawn purely from its own fields (`gap` for the left
spacing, `text` for the agent text area, `children` for the subtree). No label
is special-cased anywhere.

Taps:
  生  session:start : open a fresh agent session in a new terminal
  死  session:end   : paste the wrap-up prompt into the running session
  leaves             : `action` first, then `insert` (empty = just focus)

Control socket (for the agent): ~/.local/state/t1-touchbar/ai.sock, one JSON
line request / one JSON line reply:
  {"cmd":"ai","text":"..."} {"cmd":"normal"} {"cmd":"toggle"}
  {"cmd":"text","text":"..."} {"cmd":"status"} {"cmd":"quit"}
  {"cmd":"display","up":true|false}   one panel-backlight step (T1 STEP_DISPLAY)
`ai` / `normal` are shorthands: `ai` = "go to the entry named by the `aiNode`
config key", `normal` = "go to the root". `{"cmd":"goto","label":"…"}` reaches
any node by label, `{"cmd":"root"}` is the alias of `normal`. The reported mode
is derived from the path, never stored separately.
Use the `t1tb` CLI instead of talking to it directly.

Config: buttons.json5 (JSON5: comments, trailing commas, bare keys) in the
plugin directory, reloaded on save. A legacy buttons.json is still read, and
the old normal[]/ai[] shape is folded into one tree on load.
"""

import fcntl
import json
import mmap
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:  # JSON5 = JSON + trailing commas, comments, bare keys
    import json5 as _JSON5
except ImportError:  # fall back to strict JSON, and say so in the log
    _JSON5 = None

HOME = Path.home()
PLUGIN = HOME / ".config/omarchy/plugins/wwx.touchbar-ai"
# JSON5 (comments, trailing commas, bare keys). `buttons.json5` wins; the old
# `buttons.json` keeps working as a fallback.
CONFIG_CANDIDATES = (PLUGIN / "buttons.json5", PLUGIN / "buttons.json")
CONFIG_PATH = next((p for p in CONFIG_CANDIDATES if p.exists()), CONFIG_CANDIDATES[0])
STATE = Path(os.environ.get("XDG_STATE_HOME", str(HOME / ".local/state"))) / "t1-touchbar"
EVENTS = STATE / "ai-events.jsonl"
CTL_PATH = STATE / "ai.sock"
LOG_PATH = STATE / "ai-renderer.log"
DUMP_DIR = os.environ.get("T1TB_DUMP_DIR", "")
HW_SOCK = "/run/t1bridge/touchbar.sock"

FONT_PATH = "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"
FONT_BOLD = "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"

HELLO, REGISTER, SUBMIT, TAP_KEYS = 0x0001, 0x0002, 0x0003, 0x0004
STEP_DISPLAY = 0x0008
HELLO_ACK, ACK, ERROR, FRAME_RELEASED, INPUT_FRAME = 0x8001, 0x8002, 0x8003, 0x9001, 0x9002
FEATURES = 0x2F  # INITIAL_SERVICE(0x07) | TOUCH_ID_CANCELLATION(0x20) | DISPLAY_BRIGHTNESS(0x08)
KEY_ESC = 1

BACKGROUND = (0, 0, 0)
PLATE = (53, 53, 53)
PLATE_PRESSED = (76, 76, 76)
LABEL = (242, 242, 242)
LABEL_DIM = (200, 200, 200)

BUTTON_W = 182
GAP = 8
# Gap to the left of the level key (the "‹level name" key you get once you have
# descended), so it reads as a way out rather than one more control.
LEVEL_GAP = 44
# Medium gap between blocks of related keys (volume pair, brightness pair, …).
GROUP_GAP = 22
RADIUS = 8
ESC_W = 166

# Window identification for focus_agent(): matched against class/title, and
# against the process cmdline tree of terminal windows.
DEFAULT_AGENT_MATCH = ["omarchy.agent", "hermes"]
TERMINAL_CLASSES = ("foot", "alacritty", "kitty", "ghostty", "org.wezfurlong.wezterm",
                    "com.mitchellh.ghostty", "wezterm", "xterm", "alacritty-")

DEFAULT_CONFIG = {
    "agentWindowMatch": list(DEFAULT_AGENT_MATCH),
    "buttonWidth": 96,
    "levelGap": 44,
    "groupGap": 22,
    # Label of the root entry the agent hooks drive (t1tb ai / normal). It is
    # only a lookup key for the socket commands and for the reported mode — the
    # layout, the text area and the gaps all come from the entry's own fields.
    "aiNode": "AI",
    "tree": [
        {"label": "音 +", "action": "builtin:volume-up", "group": 0},
        {"label": "音 −", "action": "builtin:volume-down", "group": 0},
        {"label": "静", "action": "builtin:mute", "group": 1},
        {"label": "亮 +", "action": "builtin:brightness-up", "group": 2},
        {"label": "亮 −", "action": "builtin:brightness-down", "group": 2},
        {"label": "键 +", "action": "builtin:kbd-up", "group": 3},
        {"label": "键 −", "action": "builtin:kbd-down", "group": 3},
        {"label": "键灯", "action": "builtin:kbd-toggle", "group": 3},
        {"label": "AI", "group": 4, "gap": 44, "text": "AI 交互模式 · 右侧 ‹AI 回根层",
         "children": [
            {"label": "生", "action": "session:start", "group": 0},
            {"label": "死", "action": "session:end", "group": 0},
            {"label": "善", "insert": "y", "group": 1},
            {"label": "恶", "insert": "n", "group": 1},
            {"label": "引", "insert": "", "group": 2},
            {"label": "易", "insert": "换个方向，先别沿这条走", "group": 3},
            {"label": "习", "insert": "把这次会话里可复用的做法沉淀成技能", "group": 4},
            {"label": "册", "insert": "把这次工作的结论整理进 ~/wiki", "group": 5},
        ]},
    ],
    "birthCommand": "omarchy-launch-terminal hermes chat",
    "deathPrompt": "复盘这次会话：评估有没有值得沉淀为技能、或整理进 wiki 的内容，有就整理完，然后结束这次会话。",
}


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def now_ts():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def record_event(payload):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with EVENTS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now_ts(), **payload}, ensure_ascii=False) + "\n")
    except OSError as exc:
        log(f"event log failed: {exc}")


def packet(msg_type, payload, req_id):
    return b"T1HW" + struct.pack("<HHII", 1, msg_type, len(payload), req_id) + payload


def run_cmd(cmd, timeout=2.0):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def clean_entries(items, depth=0):
    """Sanitise one level of the button tree; keeps valid `children` subtrees.

    Depth is capped so a hand-edited file cannot make the bar unreachable.
    """
    out = []
    if not isinstance(items, list) or depth > 4:
        return out
    for item in items:
        if not isinstance(item, dict) or not item.get("label"):
            continue
        entry = dict(item)
        children = clean_entries(item.get("children"), depth + 1)
        if children:
            entry["children"] = children
        else:
            entry.pop("children", None)
        out.append(entry)
    return out


def parse_config_text(text):
    """JSON5 when available (comments, trailing commas, bare keys), else JSON."""
    if _JSON5 is not None:
        return _JSON5.loads(text)
    return json.loads(text)


def load_config():
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        raw = parse_config_text(text)
        if not isinstance(raw, dict):
            raise ValueError("顶层必须是对象")
    except FileNotFoundError:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            log(f"wrote default config {CONFIG_PATH}")
        except OSError as exc:
            log(f"could not write default config: {exc}")
        return cfg
    except Exception as exc:
        log(f"config invalid, using defaults: {exc}")
        # A typo in buttons.json used to fail silently: the bar just fell back
        # to the built-in layout and looked "almost right". Say it out loud.
        osd(None, None, f"配置有误({CONFIG_PATH.name})：{str(exc)[:44]}")
        return json.loads(json.dumps(DEFAULT_CONFIG))

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    tree = raw.get("tree")
    if isinstance(tree, list):
        cfg["tree"] = clean_entries(tree)
    else:
        # Legacy two-array config (normal[] + ai[]): fold it into one tree with
        # an "AI" branch at the end, so the bar has a single root.
        legacy = clean_entries(raw.get("normal"))
        ai = clean_entries(raw.get("ai"))
        if ai:
            groups = [e["group"] for e in legacy if isinstance(e.get("group"), int)]
            old_gap = raw.get("aiGap")
            legacy.append({"label": str(raw.get("aiNode") or "AI"),
                           "group": (max(groups) + 1) if groups else len(legacy),
                           "gap": old_gap if isinstance(old_gap, int) and GAP <= old_gap <= 80 else 44,
                           "text": str(raw.get("aiHint") or "") or "AI 交互模式",
                           "children": ai})
        if legacy:
            cfg["tree"] = legacy
    for key in ("birthCommand", "deathPrompt", "aiNode"):
        if isinstance(raw.get(key), str) and raw[key]:
            cfg[key] = raw[key]
    width = raw.get("buttonWidth")
    if isinstance(width, int) and 80 <= width <= BUTTON_W:
        cfg["buttonWidth"] = width
    for key in ("levelGap", "aiGap"):  # aiGap = the old name for levelGap
        gap = raw.get(key)
        if isinstance(gap, int) and GAP <= gap <= 80:
            cfg["levelGap"] = gap
    block = raw.get("groupGap")
    if isinstance(block, int) and GAP <= block <= 60:
        cfg["groupGap"] = block
    return cfg


# --------------------------------------------------------------- geometry


def level_entries(config, path):
    """Entries at the given tree path, or None when the path went stale."""
    entries = config.get("tree") or []
    for index in path:
        if not isinstance(index, int) or not 0 <= index < len(entries):
            return None
        children = entries[index].get("children")
        if not children:
            return None
        entries = children
    return entries


def node_at(config, path):
    """The entry that owns this level (last path step), or None at the root."""
    if not path:
        return None
    parent = level_entries(config, path[:-1]) or []
    index = path[-1]
    return parent[index] if 0 <= index < len(parent) else None


def find_level(config, label):
    """Path of the first entry with this label anywhere in the tree (DFS)."""
    if not label:
        return ()

    def walk(entries, prefix):
        for index, entry in enumerate(entries):
            here = prefix + (index,)
            if str(entry.get("label")) == label:
                return here
            found = walk(entry.get("children") or [], here)
            if found is not None:
                return found
        return None

    return walk(config.get("tree") or [], ()) or ()


def ai_path(config):
    """Where `t1tb ai` goes: the entry named by the `aiNode` config key."""
    return find_level(config, str(config.get("aiNode") or "AI"))


def level_text(config, path):
    """Hint text of the innermost entry with `text` along the path, else None.

    Inherited downwards on purpose: the agent text area stays visible inside the
    branch that declared it, without the layout knowing which branch that is.
    """
    entries = config.get("tree") or []
    hint = None
    for index in path:
        if not isinstance(index, int) or not 0 <= index < len(entries):
            break
        node = entries[index]
        text = node.get("text")
        if isinstance(text, str) and text:
            hint = text
        entries = node.get("children") or []
    return hint


def build_layout(w, h, config, path=()):
    """Return (esc_rect, text_rect|None, [(rect, item)]).

    One tree, one row per level. Buttons with a different `group` value get a
    wider gap, so related pairs read as one block; an entry's own `gap` overrides
    that for its left side. An entry with `children` is a branch: the tap
    descends a level instead of acting. While descended the rightmost key is the
    level key: it carries the name of the level you are in with a `‹` hint and
    pops back up. At the root there is no level key, and every entry — the `AI`
    branch included — is drawn purely from its own fields.
    """
    button_w = int(config.get("buttonWidth") or BUTTON_W)
    level_gap = int(config.get("levelGap", LEVEL_GAP))
    group_gap = int(config.get("groupGap", GROUP_GAP))
    esc = (4, 0, ESC_W, h)
    entries = level_entries(config, path)
    if entries is None:  # config edited while drilled in
        path = ()
        entries = config.get("tree") or []
    group = []
    # Array order is left-to-right: the first entry sits leftmost, the last one
    # ends up at the right edge (or next to the level key once descended).
    for index, entry in enumerate(entries):
        branch = bool(entry.get("children"))
        gap = entry.get("gap")
        group.append({
            "type": "action",
            "label": str(entry["label"]),
            "action": "" if branch else str(entry.get("action", "")),
            "insert": str(entry.get("insert", "")),
            "submit": bool(entry.get("submit", True)),
            "gap": gap if isinstance(gap, int) and GAP <= gap <= 80 else None,
            "group": entry["group"] if isinstance(entry.get("group"), int) else 0,
            "index": index,
            "branch": branch,
        })
    if path:
        node = node_at(config, path) or {}
        group.append({"type": "up", "label": str(node.get("label", "‹")),
                      "group": 10 ** 6})

    right = w - 4
    items = []
    for index in range(len(group) - 1, -1, -1):
        item = group[index]
        rect = (right - button_w, 0, right, h)
        items.append((rect, item))
        # Gap to the LEFT of this key, i.e. between group[index-1] and this one.
        if item.get("gap"):
            gap = int(item["gap"])
        elif item["type"] == "up":
            gap = level_gap
        elif index > 0 and group[index - 1]["group"] != item["group"]:
            gap = group_gap
        else:
            gap = GAP
        right -= button_w + gap

    text_rect = None
    if level_text(config, path):
        text_rect = (ESC_W + 8, 0, right - 4, h)
        if text_rect[2] - text_rect[0] < 40:
            text_rect = None
    return esc, text_rect, items


def wrap_text(draw, text, font, max_w, max_lines):
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            if len(lines) >= max_lines:
                break
            continue
        if draw.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
            if len(lines) >= max_lines:
                break
    if len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and sum(len(line) for line in lines) < len(text):
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return [line for line in lines if line]


def render_frame(w, h, config, text, pressed_key=None, path=()):
    img = Image.new("RGB", (w, h), BACKGROUND)
    draw = ImageDraw.Draw(img)
    f_wide = ImageFont.truetype(FONT_PATH, 28)
    f_small = ImageFont.truetype(FONT_PATH, 26)
    f_word = ImageFont.truetype(FONT_BOLD, 36)
    f_text = ImageFont.truetype(FONT_PATH, 22)

    def plate(rect, label, font, active=False):
        x0, _y0, x1, _y1 = rect
        draw.rounded_rectangle((x0 + 4, 0, x1 - 4, h), radius=RADIUS,
                               fill=PLATE_PRESSED if active else PLATE)
        if label:
            bbox = draw.textbbox((0, 0), label, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(((x0 + x1 - tw) / 2 - bbox[0], (h - th) / 2 - bbox[1]),
                      label, font=font, fill=LABEL)

    esc, text_rect, items = build_layout(w, h, config, path)
    plate(esc, "esc", f_small)

    if text_rect:
        shown = text or level_text(config, path) or ""
        color = LABEL_DIM if text else (140, 140, 140)
        lines = wrap_text(draw, shown, f_text, text_rect[2] - text_rect[0] - 16, 2)
        line_h = 25
        cy = (h - line_h * len(lines)) / 2
        for line in lines:
            draw.text((text_rect[0] + 8, cy), line, font=f_text, fill=color)
            cy += line_h

    for rect, item in items:
        label = item["label"]
        font = f_word if len(label) <= 1 else f_wide
        plate(rect, label, font, pressed_key == label)
        if item.get("branch"):
            # Branch key: a chevron marks "there is another level in here".
            draw.text((rect[2] - 22, 14), "›", font=f_small, fill=LABEL_DIM)
        elif item["type"] == "up":
            # Level key: mirror hint that a tap goes back up.
            draw.text((rect[0] + 10, 14), "‹", font=f_small, fill=LABEL_DIM)
    return img


# --------------------------------------------------------------- renderer


class TouchBar:
    def __init__(self, sock, width, height, max_buffers):
        self.sock = sock
        self.w, self.h = width, height
        self.buffers = {}
        self.idle = []
        self.inflight = {}
        self.next_frame = 1
        self.pressed = {}
        self.pressed_key = None
        self.mode = "normal"
        self.path = ()
        self.text = ""
        self.config = load_config()
        self.cfg_mtime = self._config_mtime()
        self.dirty = True
        self.ctl = None
        self.suspend_offset = self._suspend_offset()

        size = width * height * 4
        for i in range(min(2, max_buffers)):
            self._register(i + 1, width, height, size)
        self.idle = list(self.buffers)
        self._open_control()
        self._install_signals()

    # ---------- config ----------
    def _config_mtime(self):
        try:
            return CONFIG_PATH.stat().st_mtime
        except OSError:
            return 0.0

    def _reload_config_if_changed(self):
        mtime = self._config_mtime()
        if mtime and mtime != self.cfg_mtime:
            self.cfg_mtime = mtime
            self.config = load_config()
            if level_entries(self.config, self.path) is None:
                self.path = ()   # the edited tree dropped the level we were in
                self._sync_mode()
            log("config reloaded")
            self.dirty = True

    # ---------- control socket ----------
    def _open_control(self):
        STATE.mkdir(parents=True, exist_ok=True)
        try:
            CTL_PATH.unlink()
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(CTL_PATH))
        os.chmod(CTL_PATH, 0o600)
        srv.listen(8)
        self.ctl = srv
        log(f"control socket {CTL_PATH}")

    def _install_signals(self):
        def stop(_signum, _frame):
            raise SystemExit(0)

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, stop)

    def handle_control(self):
        conn, _ = self.ctl.accept()
        try:
            conn.settimeout(2.0)
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                return
            req = json.loads(buf.decode("utf-8").strip())
            reply = self.apply_command(req)
        except Exception as exc:
            reply = {"ok": False, "error": str(exc)}
        try:
            conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            conn.close()

    def apply_command(self, req):
        cmd = str(req.get("cmd", ""))
        if cmd == "quit":
            log("quit requested")
            threading.Thread(target=self._delayed_exit, daemon=True).start()
            return {"ok": True, "quitting": True}
        if cmd == "status":
            return {"ok": True, "mode": self.mode, "text": self.text,
                    "path": list(self.path), "level": str((node_at(self.config, self.path) or {}).get("label", ""))}
        if cmd in ("normal", "root"):
            self.set_mode("normal")
            return {"ok": True, "mode": self.mode}
        if cmd == "toggle":
            self.set_mode("normal" if self.mode == "ai" else "ai")
            return {"ok": True, "mode": self.mode}
        if cmd == "goto":
            # Generic: any label in the tree, not just the agent node. The
            # renderer has no idea which one "AI" is — the tree decides.
            target = find_level(self.config, str(req.get("label") or ""))
            if not target:
                raise ValueError(f"no entry labelled {req.get('label')!r}")
            self.goto(target)
            return {"ok": True, "mode": self.mode, "path": list(self.path)}
        if cmd == "ai":
            if "text" in req:
                self.text = str(req.get("text") or "")
            self.set_mode("ai")
            return {"ok": True, "mode": self.mode, "text": self.text}
        if cmd == "text":
            self.text = str(req.get("text") or "")
            self.set_mode("ai")
            return {"ok": True, "mode": self.mode, "text": self.text}
        if cmd == "display":
            up = bool(req.get("up", True))
            threading.Thread(target=self.step_display, args=(up,), daemon=True).start()
            return {"ok": True, "stepping": "up" if up else "down"}
        raise ValueError(f"unknown cmd {cmd!r}")

    def _delayed_exit(self):
        # A SystemExit raised inside a worker thread only ends that thread; the
        # render loop must be torn down by the main thread instead.
        time.sleep(0.2)
        log("shutting down")
        os._exit(0)

    # ---------- hardware buffers ----------
    def _register(self, buf_id, width, height, size):
        fd = os.memfd_create(f"tb-{buf_id}", os.MFD_ALLOW_SEALING)
        os.ftruncate(fd, size)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS,
                    fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SEAL)
        mm = mmap.mmap(fd, size)
        self.buffers[buf_id] = (mm, fd)
        payload = struct.pack("<IIQ", buf_id, width * 4, size)
        self.sock.sendmsg([packet(REGISTER, payload, 100 + buf_id)],
                          [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("<i", fd))])
        log(f"register buffer={buf_id} stride={width * 4} size={size}")
        self._read_until_ack(timeout=1.0)

    def _read_until_ack(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready, _, _ = select.select([self.sock], [], [], 0.2)
            if not ready:
                continue
            data = self.sock.recv(65536)
            if len(data) < 16:
                continue
            _magic, _major, mtype, plen, _req = struct.unpack("<4sHHII", data[:16])
            payload = data[16:16 + plen]
            if mtype == ERROR:
                log(f"error code={struct.unpack('<I', payload[:4])[0]}")
            elif mtype == ACK:
                return
            elif mtype == FRAME_RELEASED:
                buf_id = struct.unpack("<I", payload[:4])[0]
                self.inflight.pop(buf_id, None)
                if buf_id not in self.idle:
                    self.idle.append(buf_id)
            elif mtype == INPUT_FRAME:
                self.on_input(payload)

    # ---------- drawing ----------
    def set_mode(self, mode):
        # `ai` means "the level of the entry named by the `aiNode` config key";
        # nothing about that entry's layout is special, it is a plain branch.
        self.path = ai_path(self.config) if mode == "ai" else ()
        self._sync_mode()
        self.pressed_key = None
        self.dirty = True
        self.maybe_render()

    def _sync_mode(self):
        """Reported mode = "ai" while the path is inside the configured ai node."""
        agent = ai_path(self.config)
        inside = bool(agent) and self.path[:len(agent)] == agent
        self.mode = "ai" if inside else "normal"

    def maybe_render(self):
        if not self.dirty or not self.idle:
            return
        buf_id = self.idle.pop(0)
        mm = self.buffers[buf_id][0]
        img = render_frame(self.w, self.h, self.config, self.text, self.pressed_key, self.path)
        mm[:] = img.tobytes("raw", "BGRX")
        frame_id = self.next_frame
        self.next_frame += 1
        self.sock.send(packet(SUBMIT, struct.pack("<IQI", buf_id, frame_id, 0), 200 + frame_id % 1000))
        self.inflight[buf_id] = frame_id
        self.dirty = False
        if DUMP_DIR:
            try:
                Path(DUMP_DIR).mkdir(parents=True, exist_ok=True)
                img.save(Path(DUMP_DIR) / f"{int(time.time() * 1000)}-{self.mode}.png")
            except OSError:
                pass

    # ---------- input ----------
    def on_input(self, payload):
        if len(payload) < 12:
            return
        _ns, _fn, count, _resv = struct.unpack("<QBBH", payload[:12])
        contacts = {}
        off = 12
        for _ in range(count):
            if off + 12 > len(payload):
                break
            cid, tip, _in_range, _r, x, y = struct.unpack("<4BII", payload[off:off + 12])
            off += 12
            if tip:
                contacts[cid] = (x, y)
        for cid, pos in contacts.items():
            if cid not in self.pressed:
                self.pressed[cid] = pos
                self.pressed_key = self._key_at(pos[0])
                self.dirty = True
                self.maybe_render()
        for cid, start in list(self.pressed.items()):
            if cid in contacts:
                continue
            del self.pressed[cid]
            self.pressed_key = None
            self.dirty = True
            self.maybe_render()
            self.tap(*start)

    def _key_at(self, x):
        esc, text_rect, items = build_layout(self.w, self.h, self.config, self.path)
        if text_rect and text_rect[0] <= x < text_rect[2]:
            return "text"
        for rect, item in items:
            if rect[0] <= x < rect[2]:
                return item["label"]
        if esc[0] <= x < esc[2]:
            return "esc"
        return None

    def _hit(self, x):
        esc, text_rect, items = build_layout(self.w, self.h, self.config, self.path)
        if esc[0] <= x < esc[2]:
            return {"type": "esc", "label": "esc"}
        if text_rect and text_rect[0] <= x < text_rect[2]:
            return {"type": "text_area", "label": "text"}
        for rect, item in items:
            if rect[0] <= x < rect[2]:
                return item
        return None

    def tap(self, x, y):
        item = self._hit(x)
        if item is None:
            return
        log(f"tap {item['type']}:{item['label']} at {x},{y}")
        # Tree navigation changes the bar's own state, so it stays on this
        # (input) thread; leaf actions still run off-thread.
        if item["type"] == "up":
            self.pop_level()
            return
        if item.get("branch"):
            self.push_level(item["index"])
            return
        try:
            threading.Thread(target=self.perform, args=(item,), daemon=True).start()
        except RuntimeError as exc:
            log(f"dispatch failed: {exc}")

    def goto(self, path):
        """Jump to a tree path (root is ()), used by the socket commands."""
        if level_entries(self.config, path) is None:
            return
        self.path = tuple(path)
        self._sync_mode()
        self.pressed_key = None
        self.dirty = True
        self.maybe_render()

    # ---------- tree navigation ----------
    def push_level(self, index):
        entries = level_entries(self.config, self.path)
        if not entries or not 0 <= index < len(entries):
            return
        if not entries[index].get("children"):
            return
        self.path = self.path + (index,)
        self._sync_mode()
        log(f"open {entries[index]['label']} path={'/'.join(map(str, self.path))}")
        record_event({"label": entries[index]["label"], "action": "open",
                      "mode": self.mode, "path": list(self.path)})
        self.pressed_key = None
        self.dirty = True
        self.maybe_render()

    def pop_level(self):
        if not self.path:
            return
        self.path = self.path[:-1]
        self._sync_mode()
        log(f"back path={'/'.join(map(str, self.path)) or '-'}")
        record_event({"label": "back", "action": "close",
                      "mode": self.mode, "path": list(self.path)})
        self.pressed_key = None
        self.dirty = True
        self.maybe_render()

    # ---------- actions ----------
    def perform(self, item):
        kind = item["type"]
        try:
            if kind == "esc":
                self.send_esc()
                record_event({"label": "esc", "action": "esc"})
            elif kind == "text_area":
                record_event({"label": "text", "action": "focus"})
                self.focus_agent()
            elif kind == "action":
                action = item.get("action", "")
                insert = item.get("insert", "")
                if action:
                    self.run_action(action)
                    record_event({"label": item["label"], "action": action})
                elif insert:
                    self.inject(insert, submit=item.get("submit", True))
                    record_event({"label": item["label"], "action": "insert", "insert": insert})
                else:
                    self.focus_agent()
                    record_event({"label": item["label"], "action": "focus"})
        except Exception as exc:  # never kill the render loop
            log(f"action {kind} failed: {exc}")
            record_event({"label": item.get("label", "?"), "action": "error", "error": str(exc)})

    def send_esc(self):
        self.sock.send(packet(TAP_KEYS, struct.pack("<B3x4H", 1, KEY_ESC, 0, 0, 0), 300))

    def run_action(self, action):
        if action.startswith("session:"):
            name = action[len("session:"):]
            if name == "start":
                self.spawn_birth()
            elif name == "end":
                self.inject(self.config["deathPrompt"])
            else:
                raise ValueError(f"unknown session action {name}")
        elif action.startswith("builtin:"):
            name = action[len("builtin:"):]
            if name == "volume-up":
                self.adjust_volume(6)
            elif name == "volume-down":
                self.adjust_volume(-6)
            elif name == "mute":
                run_cmd(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
                osd("audio-volume-muted", None)
            elif name == "brightness-up":
                self.step_display(True)
            elif name == "brightness-down":
                self.step_display(False)
            elif name == "kbd-up":
                kbd_adjust(KBD_STEP)
            elif name == "kbd-down":
                kbd_adjust(-KBD_STEP)
            elif name == "kbd-toggle":
                kbd_toggle()
            elif name == "kbd-off":
                kbd_osd(kbd_set(0))
            elif name in ("voice-toggle", "voice-on", "voice-off"):
                self.toggle_voice(name)
            else:
                raise ValueError(f"unknown builtin {name}")
        elif action.startswith("cmd:"):
            subprocess.Popen(["sh", "-c", action[4:]], start_new_session=True)
        elif action.startswith("keys:"):
            run_cmd(["wtype", "-k", action[5:]])
        elif action.startswith("paste:"):
            self.inject(action[6:])
        else:
            raise ValueError(f"bad action {action!r}")

    def toggle_voice(self, name):
        """Flip the spoken-announcement switch that the hook reads."""
        path = HOME / ".local/share/t1-touchbar/tts/tts.json"
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
        except (OSError, ValueError):
            cfg = {}
        current = cfg.get("speak")
        if not isinstance(current, bool):
            current = True
        if name == "voice-on":
            enabled = True
        elif name == "voice-off":
            enabled = False
        else:
            enabled = not current
        cfg["speak"] = enabled
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        except OSError as exc:
            log(f"voice config write failed: {exc}")
            osd(None, None, message="语音开关写入失败")
            return
        osd(None, None, message="语音播报 开" if enabled else "语音播报 关")

    def step_display(self, up):
        """Step the panel backlight through the T1, then show it on the OSD."""
        before, _top = display_brightness()
        self.sock.send(packet(STEP_DISPLAY, b"\x01" if up else b"\x00", 301))
        cur, top = before, None
        for _ in range(12):  # the T1 applies the step asynchronously
            time.sleep(0.025)
            cur, top = display_brightness()
            if cur != before:
                break
        if cur is None:
            osd(None, None, "屏幕亮度不可读")
            return
        osd("brightness", int(round(cur * 100 / (top or 1))))

    def adjust_volume(self, delta):
        current = 50
        got = run_cmd(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
        if got and got.stdout:
            try:
                current = int(float(got.stdout.split()[1]) * 100)
            except Exception:
                pass
        value = max(0, min(100, current + delta))
        run_cmd(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{value}%"])
        osd("audio-volume-medium", value)

    def spawn_birth(self):
        subprocess.Popen(["sh", "-c", self.config["birthCommand"]], start_new_session=True)
        osd(None, None, message="新会话")

    # ---------- agent window plumbing ----------
    @staticmethod
    def _cmdlines(root_pid, depth=4):
        """cmdlines of root_pid and its descendants (the agent may be the
        terminal's child: `foot -e hermes chat`, or a shell's child)."""
        kids = {}
        try:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    stat = (entry / "stat").read_text()
                except OSError:
                    continue
                tail = stat[stat.rfind(")") + 2:].split()
                if len(tail) < 2:
                    continue
                kids.setdefault(int(tail[1]), []).append(int(entry.name))
        except OSError:
            return []
        out, frontier = [], [root_pid]
        for _ in range(depth):
            nxt = []
            for pid in frontier:
                try:
                    out.append((Path(f"/proc/{pid}/cmdline").read_bytes()
                                .replace(b"\0", b" ").decode("utf-8", "replace")).lower())
                except OSError:
                    pass
                nxt.extend(kids.get(pid, []))
            frontier = nxt
        return out

    def agent_window(self):
        got = run_cmd(["hyprctl", "clients", "-j"])
        if not got or not got.stdout:
            return None
        try:
            clients = json.loads(got.stdout)
        except Exception:
            return None
        words = [str(w).lower() for w in (self.config.get("agentWindowMatch")
                                         or DEFAULT_AGENT_MATCH)]
        hits = []
        for client in clients:
            haystack = " ".join(str(client.get(k, "")) for k in ("class", "initialClass", "title")).lower()
            if any(w in haystack for w in words):
                hits.append(client)
                continue
            # A plain terminal running the agent identifies nothing in its
            # class/title (`foot` / `foot`), so look at its process tree.
            if str(client.get("class", "")).lower() not in TERMINAL_CLASSES:
                continue
            try:
                pid = int(client.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            if pid > 0 and any(any(w in line for w in words) for line in self._cmdlines(pid)):
                hits.append(client)
        if not hits:
            return None
        # Tapping the key must land in the window the user is looking at, not
        # whichever client Hyprland listed first.
        return min(hits, key=lambda c: c.get("focusHistoryID", 1 << 30))

    def focus_agent(self):
        client = self.agent_window()
        if not client:
            osd(None, None, message="没找到 AI 窗口")
            return False
        # Hyprland 0.56 routes `hyprctl dispatch` through its Lua dispatchers, so
        # the classic "focuswindow address:0x..." form is a parse error.
        run_cmd(["hyprctl", "dispatch",
                 f'hl.dsp.focus({{window="address:{client["address"]}"}})'])
        time.sleep(0.18)
        return True

    def inject(self, text, submit=True):
        if not text:
            self.focus_agent()
            return
        if not self.focus_agent():
            return
        if text.isascii() and "\n" not in text:
            run_cmd(["wtype", "-s", "60", text])
        else:
            previous = None
            got = run_cmd(["wl-paste", "--no-newline"], timeout=0.6)
            if got and got.returncode == 0:
                previous = got.stdout
            run_cmd(["wl-copy", text])
            run_cmd(["wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"])
            time.sleep(0.2)
            if previous is not None:
                run_cmd(["wl-copy", previous])
        if submit:
            time.sleep(0.12)
            run_cmd(["wtype", "-k", "Return"])

    # ---------- loop ----------
    def _suspend_offset(self):
        """Accumulated suspend time: CLOCK_BOOTTIME counts it, CLOCK_MONOTONIC
        does not. The difference only grows when the machine has slept, which
        is the dependency-free way to notice a resume (no dbus/logind needed)."""
        try:
            return (time.clock_gettime(time.CLOCK_BOOTTIME)
                    - time.clock_gettime(time.CLOCK_MONOTONIC))
        except OSError:
            return None

    def _check_resume(self):
        """appletbdrm loses its scanout across suspend: the socket stays
        ESTABLISHED and this process stays alive, but nothing repaints, so the
        bar stays dark until something re-runs HELLO + register + submit.
        Re-exec instead of patching it up, so we come back in a known state."""
        offset = self._suspend_offset()
        if offset is None:
            return False
        if self.suspend_offset is None:
            self.suspend_offset = offset
            return False
        if offset - self.suspend_offset > 1.0:
            log(f"resume detected (suspend offset +{offset - self.suspend_offset:.1f}s), restarting")
            try:
                CTL_PATH.unlink()
            except OSError:
                pass
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])
        self.suspend_offset = offset
        return False

    def run(self):
        self.maybe_render()
        while True:
            watch = [self.sock, self.ctl]
            ready, _, _ = select.select(watch, [], [], 0.5)
            for source in ready:
                if source is self.ctl:
                    self.handle_control()
                    continue
                data = self.sock.recv(65536)
                if not data:
                    log("hardware socket closed")
                    return
                if len(data) < 16:
                    continue
                _magic, _major, mtype, plen, _req = struct.unpack("<4sHHII", data[:16])
                payload = data[16:16 + plen]
                if mtype == FRAME_RELEASED:
                    buf_id = struct.unpack("<I", payload[:4])[0]
                    self.inflight.pop(buf_id, None)
                    if buf_id not in self.idle:
                        self.idle.append(buf_id)
                    self.maybe_render()
                elif mtype == INPUT_FRAME:
                    self.on_input(payload)
                elif mtype == ERROR:
                    log(f"error code={struct.unpack('<I', payload[:4])[0]}")
            self._check_resume()
            self._reload_config_if_changed()
            self.maybe_render()


def osd(icon, percent, message=None):
    cmd = ["omarchy-osd", "-d", "1200"]
    if icon:
        cmd += ["-i", icon]
    if percent is None:
        cmd += ["-m", message or "已调整"]
    else:
        cmd += ["-p", str(percent)]
    run_cmd(cmd)


# ---------- keyboard backlight ----------
# applespi exposes the keyboard backlight as an LED; udev hands the node to wwx
# (/etc/udev/rules.d/90-kbd-backlight-perms.rules) so taps can write it directly.
KBD_LED = Path("/sys/class/leds/spi::kbd_backlight/brightness")
KBD_MAX = Path("/sys/class/leds/spi::kbd_backlight/max_brightness")
KBD_STATE = STATE / "kbd-backlight.json"
KBD_STEP = 51  # 20 % of 255


def kbd_levels():
    try:
        cur = int(KBD_LED.read_text().strip())
        top = int(KBD_MAX.read_text().strip())
    except (OSError, ValueError):
        return None, None
    return cur, max(top, 1)


def kbd_set(level):
    cur, top = kbd_levels()
    if cur is None:
        log("kbd: backlight device missing")
        return None
    level = max(0, min(top, int(level)))
    try:
        KBD_LED.write_text(f"{level}\n")
    except PermissionError:
        # Fallback if the udev permission rule did not run for this boot.
        res = subprocess.run(["sudo", "-n", "tee", str(KBD_LED)],
                             input=f"{level}\n", text=True, capture_output=True)
        if res.returncode != 0:
            log(f"kbd: write denied: {res.stderr.strip()}")
            return None
    return level


def kbd_last():
    try:
        return int(json.loads(KBD_STATE.read_text()).get("last") or 0)
    except (OSError, ValueError, AttributeError):
        return 0


def kbd_save_last(level):
    try:
        KBD_STATE.parent.mkdir(parents=True, exist_ok=True)
        KBD_STATE.write_text(json.dumps({"last": int(level)}))
    except OSError as exc:
        log(f"kbd: state save failed: {exc}")


def kbd_osd(level):
    if level is None:
        osd(None, None, "键盘背光不可用")
        return
    _cur, top = kbd_levels()
    if level == 0:
        osd("input-keyboard", None, "键盘背光 关")
    else:
        osd("input-keyboard", int(round(level * 100 / (top or 255))))


def kbd_adjust(delta):
    cur, _top = kbd_levels()
    if cur is None:
        osd(None, None, "键盘背光不可用")
        return
    level = kbd_set(cur + delta)
    if level:
        kbd_save_last(level)
    kbd_osd(level)


def kbd_toggle():
    cur, top = kbd_levels()
    if cur is None:
        osd(None, None, "键盘背光不可用")
        return
    if cur > 0:
        kbd_save_last(cur)
        level = kbd_set(0)
    else:
        level = kbd_set(kbd_last() or top // 2)
    kbd_osd(level)


# ---------- display backlight ----------
# The Touch Bar keys step the panel backlight through the T1 (STEP_DISPLAY), not
# through sysfs; acpi_video0 only reports the resulting value.
DISPLAY_BL = Path("/sys/class/backlight/acpi_video0")


def display_brightness():
    try:
        cur = int((DISPLAY_BL / "actual_brightness").read_text().strip())
        top = int((DISPLAY_BL / "max_brightness").read_text().strip())
    except (OSError, ValueError):
        return None, None
    return cur, max(top, 1)


def dump_frame(out, text, tree=()):
    config = load_config()
    img = render_frame(2170, 60, config, text, None, tree)
    img.save(out)
    print(out)


def main(argv):
    if "--dump" in argv:
        path = argv[argv.index("--dump") + 1]
        text = argv[argv.index("--text") + 1] if "--text" in argv else ""
        tree = ()
        if "--path" in argv:
            raw = argv[argv.index("--path") + 1].strip(",")
            tree = tuple(int(part) for part in raw.split(",") if part.strip())
        elif "--mode" in argv and argv[argv.index("--mode") + 1] == "ai":
            tree = ai_path(load_config())
        dump_frame(path, text, tree)
        return 0

    STATE.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock.connect(HW_SOCK)
    sock.send(packet(HELLO, struct.pack("<HHQ", 1, 0, FEATURES), 1))
    data = sock.recv(65536)
    _magic, _major, mtype, plen, _req = struct.unpack("<4sHHII", data[:16])
    if mtype != HELLO_ACK:
        log(f"hello failed type=0x{mtype:x}")
        return 1
    minor, _r, width, height, pixfmt, maxbuf = struct.unpack("<HHIIII", data[16:36])
    log(f"hello ok {width}x{height} minor={minor} pixfmt={pixfmt} maxbuf={maxbuf}")
    try:
        TouchBar(sock, width, height, maxbuf).run()
    except (SystemExit, KeyboardInterrupt):
        pass
    finally:
        try:
            CTL_PATH.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))