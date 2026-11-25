#!/home/wwx/.local/share/t1-touchbar/venv/bin/python3
"""wwx.touchbar — Touch Bar UI for Omarchy (t1bridge renderer).

Owns /run/t1bridge/touchbar.sock and draws ONE tree, one level at a time:

  root    esc | <root entries>                                      [| AI›]
  deeper  esc | [agent text] | <level entries> | <level key ‹name>

An entry with `children` is a branch: tapping it descends. The rightmost key
while descended is the level key — it carries the name of the level you are in
and pops back up. At the root there is no level key: every entry, the `AI`
branch included, is drawn purely from its own fields (`gap` for the left
spacing, `text` for the agent text area, `children` for the subtree). No label
is special-cased anywhere. `windowMatch` on a node decides which window
`t1tb insert` focuses (inherited down the path, fallback DEFAULT_AGENT_MATCH).

Taps:
  every leaf key runs `action: "cmd: <shell>"` — one form, no builtins. The
  shell line is whatever the config says (`cmd: t1tb volume up`, `cmd: wtype -k
  F5`, …). A leaf with no action just focuses the agent window.

Control socket (for the agent): ~/.local/state/t1-touchbar/ai.sock, one JSON
line request / one JSON line reply:
  {"cmd":"goto","label":"…","text":"…"}   jump to any node by its label
  {"cmd":"text","text":"…"}               set the agent text, stay where we are
  {"cmd":"normal"}                        back to the root row
  {"cmd":"status"} {"cmd":"quit"}
  {"cmd":"insert","text":"…","submit":true}   focus the agent window + paste
  {"cmd":"close"}                         close the agent window (close, then kill)
  {"cmd":"display","up":true|false}       one panel-backlight step (T1)
Nothing in the config is special: there is no "AI mode", only levels of the one
tree. Use the `t1tb` CLI instead of talking to it directly.

Config: ~/.config/omarchy/wwx.touchbar.view.json5 (JSON5: comments, trailing
commas, bare keys), reloaded on save. One file, one tree, no legacy shapes.
It lives outside the plugin directory on purpose: the plugin dir is code that
gets reinstalled, the button tree is user data. Override with $T1TB_CONFIG.
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
PLUGIN = HOME / ".config/omarchy/plugins/wwx.touchbar"
# The button tree is user data, so it lives beside shell.json in the omarchy
# config root rather than inside the plugin directory, which is code and gets
# replaced on reinstall. The filename carries the plugin id because that root is
# shared with the rest of omarchy's config.
CONFIG_PATH = Path(os.environ.get("T1TB_CONFIG")
                   or HOME / ".config/omarchy/wwx.touchbar.view.json5")
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
# Physical keys that can drive the bar. A node opts in with `key: "fn"`; tapping
# the key on the keyboard toggles into that node's level (tap again to leave).
# KEY_FN is what the Apple SPI Keyboard reports; the device is opened read-only.
KEY_CODES = {"fn": 464}          # KEY_FN
KEY_DEVICES = ("Apple SPI Keyboard",)
# Linux input keycodes for the keys the bar can synthesize (TapKeys opcode).
# F1..F12 are 59..68 then 87,88 (F11/F12) — not contiguous, hence the table.
KEY_TAP = {**{f"f{n}": 58 + n for n in range(1, 11)}, "f11": 87, "f12": 88,
           "esc": KEY_ESC}

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

# Fallback window match for focus_agent() when no node declares `windowMatch`.
# A node's own list wins, so any agent CLI can be targeted from the config.
DEFAULT_AGENT_MATCH = ["omarchy.agent", "hermes"]
TERMINAL_CLASSES = ("foot", "alacritty", "kitty", "ghostty", "org.wezfurlong.wezterm",
                    "com.mitchellh.ghostty", "wezterm", "xterm", "alacritty-")

DEFAULT_CONFIG = {
    "buttonWidth": 96,
    "gap": 8,          # default spacing between keys; an entry's own `gap` wins
    "levelGap": 44,    # gap left of the level key
    "children": [
        {"label": "音 +", "action": "cmd: t1tb volume up"},
        {"label": "音 −", "action": "cmd: t1tb volume down"},
        {"label": "静", "action": "cmd: t1tb volume mute", "gap": 22},
        {"label": "亮 +", "action": "cmd: t1tb display up", "gap": 22},
        {"label": "亮 −", "action": "cmd: t1tb display down"},
        {"label": "键 +", "action": "cmd: t1tb kbd up", "gap": 22},
        {"label": "键 −", "action": "cmd: t1tb kbd down"},
        {"label": "键灯", "action": "cmd: t1tb kbd toggle"},
        {"label": "AI", "gap": 44, "text": "AI 交互模式 · 右侧 ‹AI 回根层",
         "windowMatch": ["omarchy.agent", "hermes"],
         "children": [
            {"label": "生", "action": "cmd: omarchy-launch-terminal hermes chat"},
            {"label": "死", "action": "cmd: t1tb finish"},
            {"label": "善", "action": "cmd: t1tb insert 善", "gap": 22},
            {"label": "恶", "action": "cmd: t1tb insert no"},
            {"label": "声", "action": "cmd: t1tb voice toggle", "gap": 22},
        ]},
        {"label": "Fn", "key": "fn", "hidden": True, "buttonWidth": 120, "gap": 16,
         "text": "再按 Fn 返回",
         "children": [{"label": f"F{n}", "action": f"cmd: t1tb key f{n}"}
                      for n in range(1, 13)]},
    ],
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
        raw = parse_config_text(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("顶层必须是对象")
    except FileNotFoundError:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
            log(f"wrote default config {CONFIG_PATH}")
        except OSError as exc:
            log(f"could not write default config: {exc}")
        return cfg
    except Exception as exc:
        log(f"config invalid, using defaults: {exc}")
        # A parse failure used to be silent: the bar fell back to the built-in
        # layout and looked "almost right". Say it out loud.
        osd(None, None, f"配置有误({CONFIG_PATH.name})：{str(exc)[:44]}")
        return json.loads(json.dumps(DEFAULT_CONFIG))

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    children = raw.get("children")
    if isinstance(children, list):
        cfg["children"] = clean_entries(children)
    width = raw.get("buttonWidth")
    if isinstance(width, int) and 80 <= width <= BUTTON_W:
        cfg["buttonWidth"] = width
    for key in ("gap", "levelGap"):
        gap = raw.get(key)
        if isinstance(gap, int) and GAP <= gap <= 80:
            cfg[key] = gap
    return cfg


# --------------------------------------------------------------- geometry


def level_entries(config, path):
    """Entries at the given tree path, or None when the path went stale."""
    entries = config.get("children") or []
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

    return walk(config.get("children") or [], ()) or ()


def find_key_node(config, key):
    """Path of the first entry declaring `key: "<name>"` (e.g. fn)."""
    name = str(key or "").lower()
    if not name:
        return ()

    def walk(entries, prefix):
        for index, entry in enumerate(entries):
            here = prefix + (index,)
            if str(entry.get("key") or "").lower() == name:
                return here
            found = walk(entry.get("children") or [], here)
            if found is not None:
                return found
        return None

    return walk(config.get("children") or [], ()) or ()


def level_text(config, path):
    """Hint text of the innermost entry with `text` along the path, else None.

    Inherited downwards on purpose: the agent text area stays visible inside the
    branch that declared it, without the layout knowing which branch that is.
    """
    entries = config.get("children") or []
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


def level_row(config, path):
    """Row geometry inherited down the path: {buttonWidth?, gap?}."""
    row = {}
    entries = config.get("children") or []
    for index in path:
        if not isinstance(index, int) or not 0 <= index < len(entries):
            break
        node = entries[index]
        for key in ("buttonWidth", "gap"):
            value = node.get(key)
            if isinstance(value, int) and value > 0:
                row[key] = value
        entries = node.get("children") or []
    return row


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
    # A level can override the row geometry with its own buttonWidth/gap, so a
    # row of short keys (F1..F12) can be wider than the text-heavy root row.
    row = level_row(config, path)
    button_w = int(row.get("buttonWidth") or config.get("buttonWidth") or BUTTON_W)
    level_gap = int(config.get("levelGap", LEVEL_GAP))
    default_gap = int(row.get("gap", config.get("gap", GAP)))
    esc = (4, 0, ESC_W, h)
    entries = level_entries(config, path)
    if entries is None:  # config edited while drilled in
        path = ()
        entries = config.get("children") or []
    keys = []
    # Array order is left-to-right: the first entry sits leftmost, the last one
    # ends up at the right edge (or next to the level key once descended).
    for index, entry in enumerate(entries):
        branch = bool(entry.get("children"))
        gap = entry.get("gap")
        keys.append({
            "type": "action",
            "label": str(entry["label"]),
            "action": "" if branch else str(entry.get("action", "")),
            # One spacing knob per key: this key's left gap. No group ids.
            "gap": gap if isinstance(gap, int) and GAP <= gap <= 80 else default_gap,
            "index": index,
            "branch": branch,
            # `hidden` keeps an entry out of its parent's row (still reachable
            # by a physical key, `t1tb goto`, or as a child level).
            "hidden": bool(entry.get("hidden")),
        })
    if path:
        node = node_at(config, path) or {}
        keys.append({"type": "up", "label": str(node.get("label", "‹")),
                     "gap": level_gap})

    right = w - 4
    items = []
    for item in reversed([k for k in keys if not k.get("hidden")]):
        rect = (right - button_w, 0, right, h)
        items.append((rect, item))
        # Gap to the LEFT of this key, i.e. between keys[index-1] and this one.
        right -= button_w + int(item["gap"])

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
        # Level we were on before a physical key (Fn) switched us away, so the
        # same key press can put us back exactly there.
        self.key_return_path = None

        size = width * height * 4
        for i in range(min(2, max_buffers)):
            self._register(i + 1, width, height, size)
        self.idle = list(self.buffers)
        self._open_control()
        self._install_signals()
        self._open_key_device()

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
            # A node may have gained/lost `key:`; re-resolve the device so the
            # shortcut follows the config without a restart.
            if self.key_fd is not None:
                os.close(self.key_fd)
                self.key_fd = None
            self._open_key_device()
            self.dirty = True

    # ---------- physical keys ----------
    def _open_key_device(self):
        """Open the keyboard's evdev node read-only, for nodes declaring `key:`.

        Only devices whose name is in KEY_DEVICES are opened, and only if some
        node actually declares a key — otherwise the renderer leaves the keyboard
        completely alone. A missing device is not an error: the bar keeps working
        and only the physical-key shortcut is unavailable.
        """
        self.key_fd = None
        wanted = self.key_bindings()
        if not wanted:
            return
        for event in sorted(Path("/dev/input").glob("event*")):
            # The device name lives in sysfs — /dev/input/eventN is a character
            # device, so "/dev/input/eventN/device/name" does not exist.
            try:
                name = Path(f"/sys/class/input/{event.name}/device/name").read_text(
                    encoding="utf-8").strip()
            except OSError:
                continue
            if name not in KEY_DEVICES:
                continue
            try:
                self.key_fd = os.open(event, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                log(f"key device {event} ({name}) unavailable: {exc}")
                continue
            log(f"key device {event} ({name}) open for {sorted(wanted)}")
            return
        log("no key device found; physical-key levels disabled")

    def key_bindings(self):
        """{keycode: path} for every node declaring `key: "<name>"`."""
        found = {}

        def walk(entries, prefix):
            for index, entry in enumerate(entries):
                here = prefix + (index,)
                code = KEY_CODES.get(str(entry.get("key") or "").lower())
                if code is not None:
                    found[code] = here
                walk(entry.get("children") or [], here)

        walk(self.config.get("children") or [], ())
        return found

    def poll_keys(self):
        """Read pending key events; a tap of a bound key toggles its level."""
        if self.key_fd is None:
            return
        while True:
            try:
                data = os.read(self.key_fd, 24 * 64)
            except BlockingIOError:
                return
            except OSError as exc:
                log(f"key device read failed: {exc}")
                self.key_fd = None
                return
            if not data:
                self.key_fd = None
                return
            for off in range(0, len(data) - 23, 24):
                _sec, _usec, etype, code, value = struct.unpack("<qqHHi", data[off:off + 24])
                # value 1 = press, 2 = autorepeat, 0 = release. Only act on press
                # so holding the key cannot flicker between levels.
                if etype != 1 or value != 1:
                    continue
                path = self.key_bindings().get(code)
                if path is None:
                    continue
                self.toggle_key_level(code, path)

    def toggle_key_level(self, code, path):
        if self.path[:len(path)] == path:
            target = self.key_return_path if self.key_return_path is not None else ()
            log(f"key {code} off -> {'/'.join(map(str, target)) or 'root'}")
            self.key_return_path = None
            self.goto(target)
        else:
            self.key_return_path = tuple(self.path)
            log(f"key {code} on -> {'/'.join(map(str, path))}")
            self.goto(path)

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
            conn.settimeout(1.0)
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
            self.go_root()
            return {"ok": True, "mode": self.mode, "path": []}
        if cmd == "goto":
            label = str(req.get("label") or "")
            target = find_level(self.config, label)
            if not target:
                raise ValueError(f"no entry labelled {label!r}")
            if "text" in req:
                self.text = str(req.get("text") or "")
            self.goto(target)
            return {"ok": True, "mode": self.mode, "text": self.text,
                    "path": list(self.path)}
        if cmd == "text":
            self.text = str(req.get("text") or "")
            self.dirty = True
            self.maybe_render()
            return {"ok": True, "mode": self.mode, "text": self.text,
                    "path": list(self.path)}
        if cmd == "insert":
            text = str(req.get("text") or "")
            threading.Thread(target=self.inject,
                             args=(text, bool(req.get("submit", True))), daemon=True).start()
            return {"ok": True, "insert": text}
        if cmd == "close":
            threading.Thread(target=self.close_agent, daemon=True).start()
            return {"ok": True, "closing": True}
        if cmd == "key":
            # t1tb key f5 → a real F5 to the focused window.
            name = str(req.get("name") or "").lower()
            code = KEY_TAP.get(name)
            if code is None:
                raise ValueError(f"unknown key {name!r}")
            self.send_keys([code])
            return {"ok": True, "key": name}
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
    def go_root(self):
        self.goto(())

    def _sync_mode(self):
        """`mode` is informational: "normal" at the root, else the level's label."""
        self.mode = "normal" if not self.path else str(
            (node_at(self.config, self.path) or {}).get("label", "level"))

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
                if action:
                    self.run_action(action)
                    record_event({"label": item["label"], "action": action})
                else:
                    self.focus_agent()
                    record_event({"label": item["label"], "action": "focus"})
        except Exception as exc:  # never kill the render loop
            log(f"action {kind} failed: {exc}")
            record_event({"label": item.get("label", "?"), "action": "error", "error": str(exc)})

    def send_keys(self, codes):
        """Send real key taps to the focused window through the T1 (TapKeys).

        This is how the Fn layer produces F1–F12: the bar is the keyboard's
        peer here, so the key event comes from the hardware path, not from
        wtype (which needs a Wayland input seat and would be refused while the
        Touch Bar has focus).
        """
        codes = [int(c) for c in codes][:4]
        if not codes:
            return
        count = len(codes)  # the real key count; unused slots are zero, and the
        # service rejects a count that would make those zeros look like repeats
        # (TapKeys v1: "unused slots are zero and duplicates are invalid").
        codes += [0] * (4 - count)
        self.sock.send(packet(TAP_KEYS, struct.pack("<B3x4H", count, *codes), 300))

    def send_esc(self):
        self.send_keys([KEY_ESC])

    def run_action(self, action):
        """One form only: `cmd:<shell>`. Everything a key does is a command."""
        if not action.startswith("cmd:"):
            raise ValueError(f"bad action {action!r} (only cmd:<shell> is supported)")
        subprocess.Popen(["sh", "-c", action[4:]], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def step_display(self, up):
        """Step the panel backlight through the T1, then show it on the OSD.

        Only the process owning /run/t1bridge/touchbar.sock can send this opcode
        — there is no sysfs channel — so this stays in the renderer and `t1tb
        display up|down` asks for it over the control socket.
        """
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

    def window_match(self):
        """Window keywords for focus_agent(), from the tree.

        The innermost node on the current path that declares `windowMatch` wins
        (so the agent branch owns its own match list); at the root the first
        declaration anywhere in the tree is used. With no declaration at all it
        falls back to DEFAULT_AGENT_MATCH. Nothing is hard-coded to one agent.
        """
        entries = self.config.get("children") or []
        found = None
        for index in self.path:
            if not isinstance(index, int) or not 0 <= index < len(entries):
                break
            node = entries[index]
            if isinstance(node.get("windowMatch"), list) and node["windowMatch"]:
                found = node["windowMatch"]
            entries = node.get("children") or []
        if found:
            return found

        def walk(items):
            for item in items:
                match = item.get("windowMatch")
                if isinstance(match, list) and match:
                    return match
                deeper = walk(item.get("children") or [])
                if deeper:
                    return deeper
            return None

        return walk(self.config.get("children") or []) or DEFAULT_AGENT_MATCH

    def agent_window(self):
        got = run_cmd(["hyprctl", "clients", "-j"])
        if not got or not got.stdout:
            return None
        try:
            clients = json.loads(got.stdout)
        except Exception:
            return None
        words = [str(w).lower() for w in self.window_match()]
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

    def window_alive(self, address):
        got = run_cmd(["hyprctl", "clients", "-j"])
        if not got or not got.stdout:
            return True  # cannot tell; assume alive so the caller escalates
        try:
            clients = json.loads(got.stdout)
        except Exception:
            return True
        return any(str(c.get("address", "")).lower() == str(address).lower()
                   for c in clients)

    def close_agent(self):
        """Close the agent window; escalate to kill if the app ignores close.

        Electron/Chromium (the Hermes desktop app) answers `xdg_toplevel.close`
        with "ok" and then keeps the window — the polite request is a no-op
        there. Terminals honour it. So: ask politely, wait, check, then kill.
        """
        client = self.agent_window()
        if not client:
            osd(None, None, message="没找到 AI 窗口")
            return False
        address = client["address"]
        run_cmd(["hyprctl", "dispatch",
                 f'hl.dsp.window.close({{window="address:{address}"}})'])
        for _ in range(6):  # up to ~1.5 s; most windows close on the first tick
            time.sleep(0.25)
            if not self.window_alive(address):
                osd(None, None, message="会话已结束")
                return True
        run_cmd(["hyprctl", "dispatch",
                 f'hl.dsp.window.kill({{window="address:{address}"}})'])
        osd(None, None, message="会话已结束（强制关闭）")
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
            if self.key_fd is not None:
                watch.append(self.key_fd)
            ready, _, _ = select.select(watch, [], [], 0.5)
            for source in ready:
                if source is self.ctl:
                    self.handle_control()
                    continue
                if source is self.key_fd:
                    self.poll_keys()
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


def osd(icon=None, percent=None, message=None):
    cmd = ["omarchy-osd", "-d", "1200"]
    if icon:
        cmd += ["-i", icon]
    if percent is None:
        cmd += ["-m", message or "已调整"]
    else:
        cmd += ["-p", str(percent)]
    run_cmd(cmd)


# ---------- display backlight ----------
# The panel backlight is stepped through the T1 (STEP_DISPLAY), not through
# sysfs; acpi_video0 only reports the resulting value.
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
        elif "--at" in argv:
            tree = find_level(load_config(), argv[argv.index("--at") + 1])
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
