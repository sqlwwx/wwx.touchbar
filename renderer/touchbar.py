#!/home/wwx/.local/share/t1-touchbar/venv/bin/python3
"""
wwx.touchbar — Omarchy 的 Touch Bar UI（t1bridge 渲染器）。

独占 /run/t1bridge/touchbar.sock，每次绘制同一棵树的一层：

  root    esc | <根层按键>                                      [| AI›]
  deeper  esc | [agent 文字] | <本层按键> | <层级键 ‹名称>

有 `children` 的节点就是分支：点击会进入下一层。进入后最右侧的键是层级键——
显示当前层名称，点它返回上一级。根层没有层级键：包括 `AI` 在内的每个按键都
只由自身字段绘制（`gap` 控左侧间距、`text` 控 agent 文字区、`children` 控子树）。
没有任何 label 被特判。节点的 `windowMatch` 决定 `t1tb insert` 聚焦哪个窗口
（沿路径向下继承，缺省回退到 DEFAULT_AGENT_MATCH）。

点击：
  每个叶子键执行 `action: "cmd: <shell>"` —— 只有这一种形式，没有内置行为。
  命令行由配置决定（`cmd: t1tb volume up`、`cmd: wtype -k F5`、…）。
  没有 action 的叶子只聚焦 agent 窗口。

控制 socket（给 agent 用）：~/.local/state/t1-touchbar/ai.sock，一行 JSON 请求 /
一行 JSON 回复：
  {"cmd":"goto","label":"…","text":"…"}   按 label 跳到任意节点
  {"cmd":"text","text":"…"}               设置 agent 文字，留在当前层
  {"cmd":"normal"}                        回到根层
  {"cmd":"status"} {"cmd":"quit"}
  {"cmd":"insert","text":"…","submit":true}   聚焦 agent 窗口并粘贴
  {"cmd":"close"}                         关闭 agent 窗口（先 close，再 kill）
  {"cmd":"display","up":true|false}       面板背光调一档（T1）
配置里没有任何特殊概念：没有"AI 模式"，只有同一棵树的不同层。
用 `t1tb` CLI 操作，不要直接跟 socket 说话。

配置：~/.config/omarchy/wwx.touchbar.view.json5（JSON5：注释、尾逗号、裸键），
保存即重载。一个文件、一棵树，没有旧格式。
它特意放在插件目录外：插件目录是会被重装替换的代码，按钮树是用户数据。
用 $T1TB_CONFIG 可以覆盖路径。
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

try:  # JSON5 = JSON + 尾逗号、注释、裸键
    import json5 as _JSON5
except ImportError:  # 回退到严格 JSON，并在日志里说明
    _JSON5 = None

HOME = Path.home()
PLUGIN = HOME / ".config/omarchy/plugins/wwx.touchbar"
# 按钮树是用户数据，放在 omarchy 配置根（shell.json 旁边），而不是插件目录里——
# 插件目录是代码，重装会被替换。文件名带插件 id，因为这个根目录和 omarchy 其它配置共用。
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
FEATURES = 0x2F  # 能力位：INITIAL_SERVICE(0x07) | TOUCH_ID_CANCELLATION(0x20) | DISPLAY_BRIGHTNESS(0x08)
KEY_ESC = 1
# 能驱动触摸条的物理键。节点用 `key: "fn"` 声明；按一下键盘上的键进入该节点那一层
# （再按一次退出）。KEY_FN 是 Apple SPI 键盘上报的键码；设备以只读方式打开。
KEY_CODES = {"fn": 464}          # 键码：KEY_FN
KEY_DEVICES = ("Apple SPI Keyboard",)
# 触摸条能合成按键的 Linux 输入键码（TapKeys opcode）。
# F1..F12 是 59..68，然后是 87,88（F11/F12）——不连续，所以用这张表。
KEY_TAP = {**{f"f{n}": 58 + n for n in range(1, 11)}, "f11": 87, "f12": 88,
           "esc": KEY_ESC}

BACKGROUND = (0, 0, 0)
PLATE = (53, 53, 53)
PLATE_PRESSED = (76, 76, 76)
LABEL = (242, 242, 242)
LABEL_DIM = (200, 200, 200)

BUTTON_W = 182
GAP = 4
# 层级键（进入某层后出现的 "‹层名" 键）左侧的间距，让它看起来是"出口"而不是又一个控制键。
LEVEL_GAP = 44
# 相关按键块之间的中等间距（音量一组、亮度一组，…）。
GROUP_GAP = 22
RADIUS = 8
ESC_W = 166

# 节点没有声明 `windowMatch` 时 focus_agent() 使用的默认窗口匹配。
# 节点自己的列表优先，所以任何 agent CLI 都能在配置里指定。
DEFAULT_AGENT_MATCH = ["omarchy.agent", "hermes"]
TERMINAL_CLASSES = ("foot", "alacritty", "kitty", "ghostty", "org.wezfurlong.wezterm",
                    "com.mitchellh.ghostty", "wezterm", "xterm", "alacritty-")

DEFAULT_CONFIG = {
    "buttonWidth": 96,
    "gap": 4,          # 按键间距默认值；节点自己的 `gap` 优先
    "levelGap": 44,    # 层级键左侧的间距
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
    """清洗按钮树的一层，只保留合法的 `children` 子树。

    限制深度，避免手改的配置让条形界面不可达。
    """
    out = []
    if not isinstance(items, list):
        return out
    if depth > 4:
        if depth == 5:
            log("配置嵌套超过 4 层，更深的节点已忽略")
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
    """有 json5 时用 JSON5（注释、尾逗号、裸键），否则退回严格 JSON。"""
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
        # 以前解析失败是静默的：触摸条回退到内置布局，看起来"差不多对"。
        # 现在要明确说出来。
        osd(None, None, f"配置有误({CONFIG_PATH.name})：{str(exc)[:44]}")
        return json.loads(json.dumps(DEFAULT_CONFIG))

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    children = raw.get("children")
    if isinstance(children, list):
        cfg["children"] = clean_entries(children)
    width = raw.get("buttonWidth")
    if isinstance(width, int) and 80 <= width <= BUTTON_W:
        cfg["buttonWidth"] = width
    elif width is not None:
        log(f"忽略 buttonWidth={width!r}：需为 80–{BUTTON_W} 的整数")
    for key in ("gap", "levelGap"):
        gap = raw.get(key)
        if isinstance(gap, int) and GAP <= gap <= 80:
            cfg[key] = gap
        elif gap is not None:
            log(f"忽略 {key}={gap!r}：需为 {GAP}–80 的整数")
    return cfg


# --------------------------------------------------------------- 布局计算


def level_entries(config, path):
    """返回给定树路径上的节点；路径已失效时返回 None。"""
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
    """拥有当前层的节点（路径最后一步）；在根层返回 None。"""
    if not path:
        return None
    parent = level_entries(config, path[:-1]) or []
    index = path[-1]
    return parent[index] if 0 <= index < len(parent) else None


def find_level(config, label):
    """树中第一个匹配该 label 的节点的路径（深度优先）。"""
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
    """第一个声明 `key: "<name>"` 的节点路径（如 fn）。"""
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
    """路径上最内层声明 `text` 的节点的提示文字；没有则为 None。

    故意向下继承：agent 文字区在声明它的分支内保持可见，
    而布局代码不需要知道是哪个分支。
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
    """沿路径继承的行布局：{buttonWidth?, gap?}。"""
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
    """返回 (esc 矩形, 文字矩形|None, [(矩形, 节点)])。

    同一棵树，每层一行。每个节点自己的 `gap` 决定它左侧的间距，
    因此配置里给相关按键更大的左间距，就能让它们读起来是一组。
    有 `children` 的节点是分支：点击会进入下一层而不是执行动作。
    进入后最右侧的键是层级键：显示当前层名称和 `‹` 提示，点它返回上一级。
    根层没有层级键，包括 `AI` 在内的每个节点都只由自身字段绘制。
    """
    # 某一层可以用自己的 buttonWidth/gap 覆盖行布局，这样短键组成的行（F1..F12）
    # 可以比文字较多的根层更宽。
    row = level_row(config, path)
    button_w = int(row.get("buttonWidth") or config.get("buttonWidth") or BUTTON_W)
    level_gap = int(config.get("levelGap", LEVEL_GAP))
    default_gap = int(row.get("gap", config.get("gap", GAP)))
    esc = (4, 0, ESC_W, h)
    entries = level_entries(config, path)
    if entries is None:  # 在深层时改了配置
        path = ()
        entries = config.get("children") or []
    keys = []
    # 数组顺序就是从左到右：第一个节点在最左，最后一个在最右
    # （进入某层后则紧挨层级键）。
    for index, entry in enumerate(entries):
        branch = bool(entry.get("children"))
        gap = entry.get("gap")
        keys.append({
            "type": "action",
            "label": str(entry["label"]),
            "action": "" if branch else str(entry.get("action", "")),
            # 每个键一个间距参数：该键左侧的 gap。没有 group id。
            "gap": gap if isinstance(gap, int) and GAP <= gap <= 80 else default_gap,
            "index": index,
            "branch": branch,
            # `hidden` 让节点不出现在父层的一行里（仍可通过物理键、
            # `t1tb goto` 或作为子层进入）。
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
        # 这个键左侧的间距，即 keys[index-1] 和它之间的间距。
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
            # 分支键：箭头表示"这里面还有一层"。
            draw.text((rect[2] - 22, 14), "›", font=f_small, fill=LABEL_DIM)
        elif item["type"] == "up":
            # 层级键：镜像提示，点击会返回上一级。
            draw.text((rect[0] + 10, 14), "‹", font=f_small, fill=LABEL_DIM)
    return img


# --------------------------------------------------------------- 渲染器


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
        # AI 交互的目标窗口 address。hook 触发时随 goto 传入并锁定；
        # 为空时 insert/close 作用于当前聚焦窗口。切回根层时清空。
        self.target_window = None
        self.config = load_config()
        self.cfg_mtime = self._config_mtime()
        self.dirty = True
        self.ctl = None
        self.suspend_offset = self._suspend_offset()
        # 物理键（Fn）把我们从当前层切走之前所在的层，这样再按一次同一个键
        # 就能准确回到那里。
        self.key_return_path = None

        size = width * height * 4
        for i in range(min(2, max_buffers)):
            self._register(i + 1, width, height, size)
        self.idle = list(self.buffers)
        self._open_control()
        self._install_signals()
        self._open_key_device()

    # ---------- 配置 ----------
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
                self.path = ()   # 编辑后的树丢掉了我们所在的层
                self._sync_mode()
            log("config reloaded")
            # 节点可能增删了 `key:`；重新解析设备，让快捷键跟随配置变化而无需重启。
            if self.key_fd is not None:
                os.close(self.key_fd)
                self.key_fd = None
            self._open_key_device()
            self.dirty = True

    # ---------- 物理键 ----------
    def _open_key_device(self):
        """以只读方式打开键盘的 evdev 节点，供声明了 `key:` 的节点使用。

        只打开名称在 KEY_DEVICES 里的设备，并且仅当有节点真的声明了 key——
        否则渲染器完全不碰键盘。设备不存在不算错误：条形界面照常工作，
        只是物理键快捷键不可用。
        """
        self.key_fd = None
        wanted = self.key_bindings()
        if not wanted:
            return
        for event in sorted(Path("/dev/input").glob("event*")):
            # 设备名在 sysfs 里——/dev/input/eventN 是字符设备，
            # 所以 "/dev/input/eventN/device/name" 并不存在。
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
        """每个声明 `key: "<name>"` 的节点的 {键码: 路径}。"""
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
        """读取待处理的按键事件；点击已绑定的键会切换对应层。"""
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
                # value 1 = 按下，2 = 自动重复，0 = 松开。只响应按下，
                # 这样长按不会让层来回闪。
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

    # ---------- 控制 socket ----------
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
                    "path": list(self.path), "level": str((node_at(self.config, self.path) or {}).get("label", "")),
                    "window": self.target_window}
        if cmd in ("normal", "root"):
            self.go_root()
            self.target_window = None
            return {"ok": True, "mode": self.mode, "path": []}
        if cmd == "goto":
            label = str(req.get("label") or "")
            target = find_level(self.config, label)
            if not target:
                raise ValueError(f"no entry labelled {label!r}")
            if "text" in req:
                self.text = str(req.get("text") or "")
            # AI 触发时 hook 带上触发者的窗口 address，锁定为本次交互目标。
            # 不带则沿用上一次的目标（普通按钮点击不改它）。
            if "window" in req:
                self.target_window = str(req.get("window") or "") or None
            self.goto(target)
            return {"ok": True, "mode": self.mode, "text": self.text,
                    "path": list(self.path), "window": self.target_window}
        if cmd == "text":
            self.text = str(req.get("text") or "")
            self.dirty = True
            self.maybe_render()
            return {"ok": True, "mode": self.mode, "text": self.text,
                    "path": list(self.path)}
        if cmd == "insert":
            text = str(req.get("text") or "")
            window = str(req.get("window") or "") or None
            threading.Thread(target=self.inject,
                             args=(text, bool(req.get("submit", True)), window),
                             daemon=True).start()
            return {"ok": True, "insert": text, "window": window or self.target_window}
        if cmd == "close":
            window = str(req.get("window") or "") or None
            threading.Thread(target=self.close_agent, args=(window,),
                             daemon=True).start()
            return {"ok": True, "closing": True, "window": window or self.target_window}
        if cmd == "key":
            # t1tb key f5 → 向聚焦窗口发送真实的 F5。
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
        # 在工作线程里抛 SystemExit 只会结束那个线程；
        # 渲染循环必须由主线程来收尾。
        time.sleep(0.2)
        log("shutting down")
        os._exit(0)

    # ---------- 硬件缓冲区 ----------
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

    # ---------- 绘制 ----------
    def go_root(self):
        self.goto(())

    def _sync_mode(self):
        """`mode` 只是提示信息：在根层为 "normal"，否则是该层的 label。"""
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

    # ---------- 输入 ----------
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
        # 树导航改变的是条形界面自身的状态，所以留在当前
        # （输入）线程；叶子节点的动作仍在独立线程执行。
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
        """跳到某个树路径（根层是 ()），供 socket 命令使用。"""
        if level_entries(self.config, path) is None:
            return
        self.path = tuple(path)
        self._sync_mode()
        self.pressed_key = None
        self.dirty = True
        self.maybe_render()

    # ---------- 树导航 ----------
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

    # ---------- 动作 ----------
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
        except Exception as exc:  # 绝不能让渲染循环挂掉
            log(f"action {kind} failed: {exc}")
            record_event({"label": item.get("label", "?"), "action": "error", "error": str(exc)})

    def send_keys(self, codes):
        """通过 T1（TapKeys）向聚焦窗口发送真实按键。

        Fn 层就是这样产生 F1–F12 的：条形界面在这里是键盘的对等设备，
        按键事件走硬件通道，而不是 wtype（后者需要 Wayland input seat，
        在 Touch Bar 获得焦点时会被拒绝）。
        """
        codes = [int(c) for c in codes][:4]
        if not codes:
            return
        count = len(codes)  # 真实按键数；未用槽位填 0，服务会拒绝
        # 让那些 0 看起来像重复键的 count
        # （TapKeys v1 规定 "unused slots are zero and duplicates are invalid"）。
        codes += [0] * (4 - count)
        self.sock.send(packet(TAP_KEYS, struct.pack("<B3x4H", count, *codes), 300))

    def send_esc(self):
        self.send_keys([KEY_ESC])

    def run_action(self, action):
        """只有一种形式：`cmd:<shell>`。按键做的一切都是命令。"""
        if not action.startswith("cmd:"):
            raise ValueError(f"bad action {action!r} (only cmd:<shell> is supported)")
        subprocess.Popen(["sh", "-c", action[4:]], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def step_display(self, up):
        """通过 T1 调节面板背光一档，并在 OSD 上显示。

        只有独占 /run/t1bridge/touchbar.sock 的进程才能发这个 opcode
        ——没有 sysfs 通道——所以逻辑留在渲染器里，`t1tb display up|down`
        通过控制 socket 请求它。
        """
        before, _top = display_brightness()
        self.sock.send(packet(STEP_DISPLAY, b"\x01" if up else b"\x00", 301))
        cur, top = before, None
        for _ in range(12):  # T1 是异步应用这一档的
            time.sleep(0.025)
            cur, top = display_brightness()
            if cur != before:
                break
        if cur is None:
            osd(None, None, "屏幕亮度不可读")
            return
        osd("brightness", int(round(cur * 100 / (top or 1))))

    # ---------- agent 窗口处理 ----------
    @staticmethod
    def _cmdlines(root_pid, depth=4):
        """root_pid 及其后代的命令行（agent 可能是终端的子进程：
        `foot -e hermes chat`，或者 shell 的子进程）。"""
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
        """focus_agent() 用的窗口关键词，来自配置树。

        当前路径上最内层声明 `windowMatch` 的节点胜出
        （所以 agent 分支可以有自己的匹配列表）；在根层则用树中第一个声明。
        完全没声明时回退到 DEFAULT_AGENT_MATCH。没有任何 agent 被硬编码。
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

    def agent_window(self, address=None):
        """解析本次交互的目标窗口，按优先级：显式 address > 锁定的触发者 > 当前聚焦 > 旧关键词。"""
        clients = self._clients()
        if not clients:
            return None
        # 1) 调用方显式指定（t1tb insert/close -w）
        if address:
            return next((c for c in clients
                         if str(c.get("address", "")).lower() == address.lower()), None)
        # 2) hook 在 AI 触发时锁定的窗口
        if self.target_window:
            return next((c for c in clients
                         if str(c.get("address", "")).lower() == self.target_window.lower()), None)
        # 3) 当前聚焦窗口（普通按钮操作默认落在这里）
        active = self._active_client(clients)
        if active:
            return active
        # 4) 兜底：沿用旧的 windowMatch 关键词，兼容没有焦点的场景
        words = [str(w).lower() for w in self.window_match()]
        hits = []
        for client in clients:
            haystack = " ".join(str(client.get(k, "")) for k in ("class", "initialClass", "title")).lower()
            if any(w in haystack for w in words):
                hits.append(client)
                continue
            if str(client.get("class", "")).lower() not in TERMINAL_CLASSES:
                continue
            try:
                pid = int(client.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            if pid > 0 and any(any(w in line for w in words) for line in self._cmdlines(pid)):
                hits.append(client)
        return min(hits, key=lambda c: c.get("focusHistoryID", 1 << 30)) if hits else None

    def _clients(self):
        got = run_cmd(["hyprctl", "clients", "-j"])
        if not got or not got.stdout:
            return []
        try:
            return json.loads(got.stdout)
        except Exception:
            return []

    @staticmethod
    def _active_client(clients):
        got = run_cmd(["hyprctl", "activewindow", "-j"])
        if not got or not got.stdout:
            return None
        try:
            active = json.loads(got.stdout)
        except Exception:
            return None
        address = str(active.get("address", "")).lower()
        if not address or address == "0x0":
            return None
        return next((c for c in clients
                     if str(c.get("address", "")).lower() == address), None)

    def focus_agent(self, address=None):
        client = self.agent_window(address)
        if not client:
            osd(None, None, message="没找到窗口")
            return False
        # Hyprland 0.56 把 `hyprctl dispatch` 路由到它的 Lua dispatcher，
        # 所以老式的 "focuswindow address:0x..." 写法是解析错误。
        run_cmd(["hyprctl", "dispatch",
                 f'hl.dsp.focus({{window="address:{client["address"]}"}})'])
        time.sleep(0.12)
        return True

    def window_alive(self, address):
        got = run_cmd(["hyprctl", "clients", "-j"])
        if not got or not got.stdout:
            return True  # 无法判断；假定还活着，让调用方继续升级处理
        try:
            clients = json.loads(got.stdout)
        except Exception:
            return True
        return any(str(c.get("address", "")).lower() == str(address).lower()
                   for c in clients)

    def close_agent(self, address=None):
        """关闭目标窗口；应用不响应关闭请求时升级为 kill。

        Electron/Chromium（Hermes 桌面应用）收到 `xdg_toplevel.close` 会回 "ok"
        然后保持窗口不关——礼貌请求对它无效。终端会正常关闭。
        所以：先礼貌请求，等待，检查，再 kill。
        """
        client = self.agent_window(address)
        if not client:
            osd(None, None, message="没找到窗口")
            return False
        address = client["address"]
        run_cmd(["hyprctl", "dispatch",
                 f'hl.dsp.window.close({{window="address:{address}"}})'])
        for _ in range(6):  # 最多约 1.5 秒；多数窗口第一下就关了
            time.sleep(0.25)
            if not self.window_alive(address):
                osd(None, None, message="会话已结束")
                return True
        run_cmd(["hyprctl", "dispatch",
                 f'hl.dsp.window.kill({{window="address:{address}"}})'])
        osd(None, None, message="会话已结束（强制关闭）")
        return True

    def inject(self, text, submit=True, address=None):
        if not text:
            self.focus_agent(address)
            return
        if not self.focus_agent(address):
            return
        if text.isascii() and "\n" not in text:
            run_cmd(["wtype", "-s", "15", text])
        else:
            previous = None
            got = run_cmd(["wl-paste", "--no-newline"], timeout=0.6)
            if got and got.returncode == 0:
                previous = got.stdout
            run_cmd(["wl-copy", text])
            run_cmd(["wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"])
            time.sleep(0.1)
            if previous is not None:
                run_cmd(["wl-copy", previous])
        if submit:
            time.sleep(0.05)
            run_cmd(["wtype", "-k", "Return"])

    # ---------- 循环 ----------
    def _suspend_offset(self):
        """累计休眠时间：CLOCK_BOOTTIME 会计入，CLOCK_MONOTONIC 不会。
        两者差值只在机器睡过之后才增长，这是无需依赖就能发现唤醒的方式
        （不需要 dbus/logind）。"""
        try:
            return (time.clock_gettime(time.CLOCK_BOOTTIME)
                    - time.clock_gettime(time.CLOCK_MONOTONIC))
        except OSError:
            return None

    def _check_resume(self):
        """appletbdrm 在休眠后会丢失 scanout：socket 仍是 ESTABLISHED，进程也还活着，
        但没有任何重绘，所以条形界面一直是黑的，直到有人重新跑 HELLO +
        register + submit。这里选择重新执行自身而不是打补丁，
        这样能回到一个确定的状态。"""
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


# ---------- 显示背光 ----------
# 面板背光通过 T1（STEP_DISPLAY）调节，而不是 sysfs；
# acpi_video0 只上报调节后的结果值。
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
