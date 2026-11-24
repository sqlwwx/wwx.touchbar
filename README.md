# wwx.touchbar-ai

Touch Bar UI for the Omarchy agent on a t1bridge MacBook.

The Touch Bar is a DRM device with exactly one owner
(`/run/t1bridge/touchbar.sock`). This plugin is a `service` plugin: it stops the
stock t1bridge renderer, runs its own renderer, restarts it if it dies, and
stops it when the plugin is unloaded.

```
manifest.json          kind: service
Service.qml            supervisor (Quickshell Process, 3 s restart on exit)
view.json5             the whole button tree (JSON5)
renderer/touchbar_ai.py  the renderer (Hello / buffers / draw / tap)
renderer/t1tb.py       control CLI (symlinked to ~/.local/bin/t1tb)
renderer/launch.sh     stop stock renderer, exec renderer
hooks/human-input.py   Hermes shell hook → touch bar mode + voice
```

## Agent notifications

Two Hermes shell hooks (in `~/.hermes/config.yaml`, allowlisted in
`~/.hermes/shell-hooks-allowlist.json`) drive the bar from the agent side:

| hook | effect |
| --- | --- |
| `on_human_input_request` | `t1tb goto AI -t "等待审批：<prompt>"` (or 需要密码 / 需要回答), spoken aloud |
| `on_human_input_resolved` | `t1tb normal`, or the next still-pending prompt |

A pending counter in `~/.local/state/t1-touchbar/ai-pending.json` keeps
concurrent prompts apart and expires entries after 10 min, so a crashed turn
cannot pin the bar on the agent level. Prompt text is force-redacted by Hermes and truncated to
110 chars. Voice uses `~/.local/share/t1-touchbar/tts/say.py` (edge-tts 7.3.1 +
`pw-play`, detached so the 5 s hook budget is not blown); set Voice is optional and off-switchable, from the bar or from a shell. On the bar
the `声` key (configurable like any other) runs `cmd: t1tb voice toggle` and
shows an OSD confirming 开/关:

```sh
t1tb voice off      # silent from now on; the bar still switches + shows text
t1tb voice on
t1tb voice toggle
t1tb voice status   # {"speak": true, "source": "...tts.json"}
```

`t1tb voice` writes `"speak"` into `~/.local/share/t1-touchbar/tts/tts.json`
(same file as `proxy` and `voice`). `$T1TTS_SPEAK=0` overrides the file for one
process. Nothing else changes when voice is off — mode switching and the status
text are independent of it.

Proxy/voice live in the same `tts.json`
(`{"speak": true, "proxy": "socks5://100.80.181.5:10808", "voice": "zh-CN-YunxiNeural"}`);
`$T1TTS_PROXY` overrides it (`none` forces direct), `$T1TTS_VOICE` the voice.
A configured proxy that fails falls back to a direct attempt once.
`T1TTS_SPEAK=0`
to silence it.

Hooks are read when a session is built — a running TUI keeps the old set until
you restart it.

## Modes

```
normal   esc | ............ | 音+ 音− │ 静音 │ 亮+ 亮− │ 键+ 键− 键灯 ‖ AI
ai       esc | agent text .. | 生 死 ‖ 善 恶 │ 引 易 │ 习 册 ‖ AI

│ = groupGap (22), ‖ = aiGap (44)
```

Keyboard-backlight keys drive applespi's `spi::kbd_backlight` LED directly:
`键 +` / `键 −` step 51 of 255 (20 %), `键灯` toggles off ⇄ last non-zero level
(remembered in `~/.local/state/t1-touchbar/kbd-backlight.json`). Each tap shows
an OSD with the percentage. Writing the LED node needs the udev rule
`renderer/90-kbd-backlight-perms.rules`, installed (and refreshed) with:

```sh
sudo install -m 0644 renderer/90-kbd-backlight-perms.rules /etc/udev/rules.d/ \
  && sudo udevadm control --reload \
  && sudo udevadm trigger --action=change --subsystem-match=leds
```

It hands the node to group `wwx` with mode 0660 at every boot. Note: `udevadm
trigger ... --action=change` on the leds subsystem also makes
`systemd-backlight@` re-apply the brightness saved at last shutdown, and that
saved value is what the keyboard comes up with — turn the light back on before
shutting down if you want it lit at the next boot. Without the udev rule the
renderer falls back to `sudo -n tee` on the node (passwordless sudo only).

At the root the `AI` branch sits rightmost; once inside a level the rightmost
key is the level key `‹层名`, so the same slot both enters and leaves.

| key | does |
| --- | --- |
| esc | real `KEY_ESC` to the focused window |
| text area | focus the agent window, so you can type there |
| branch (`children`) | descend a level; the rightmost key becomes `‹level name` |
| level key | pop back up one level |
| any leaf | run its `action: "cmd: <shell>"`; a leaf with no action just focuses the agent |

`死` is a leaf too — `cmd: t1tb insert 收尾三件事：… 完成后运行 t1tb close 关掉本窗口`
— so the *agent* closes the window when it is actually done. There is no timer:
killing on a schedule would cut a wrap-up short. `t1tb close` resolves the window
through the same `windowMatch` list as `t1tb insert`, then sends a normal close
(`hl.dsp.window.close`).

There are no special keys: `生` (`cmd: omarchy-launch-terminal hermes chat`) and
`死` (`cmd: t1tb insert 复盘…`) are ordinary leaves whose command happens to start
or wrap up a session. Rename, move or drop them like any other key.

CJK text cannot be typed with `wtype`, so non-ASCII inserts go through
`wl-copy` + `Ctrl+Shift+V` (the clipboard is restored afterwards). ASCII inserts
are typed directly.

## view.json5

JSON5, reloaded on save — no restart. Comments, trailing commas and bare keys
all work. (JSON5 does **not** forgive a *missing separator* comma — `{a:1 b:2}`
is still an error; it forgives `{a:1, b:2,}`.) The older `buttons.json5` and
`buttons.json` are still read if no `view.json5` exists.

```json5
{
  buttonWidth: 85,
  gap: 4,          // default spacing in front of every key
  levelGap: 44,    // gap left of the level key (‹level name)

  children: [      // the file itself is the root node
    { label: "音 +", action: "cmd: t1tb volume up" },
    { label: "音 −", action: "cmd: t1tb volume down" },
    { label: "静", action: "cmd: t1tb volume mute", gap: 22 },
    { label: "AI", gap: 44, text: "AI 交互模式 · 右侧 ‹AI 回根层",
      windowMatch: ["omarchy.agent", "hermes"],
      children: [
        { label: "生", action: "cmd: omarchy-launch-terminal hermes chat" },
        { label: "死", action: "cmd: t1tb insert 复盘这次会话…" },
        { label: "善", action: "cmd: t1tb insert 善", gap: 22 } ] },
  ],

}
```

- `buttonWidth` 80–182, default 96. Lower = more room for the text area.
- The file itself is one node: its `children` are the root row. Any node with
  `children` is a **branch**: the tap descends a level and the bar shows that
  subtree. Branches draw a `›` hint and their own `action` is ignored. Nesting is
  capped at 4 levels.
- No entry is special-cased, the `AI` button included — it is a plain branch in
  `children`. At the root there is no level key; one level down the rightmost key
  is the **level key**: it carries the name of the level you are in (with a `‹`
  hint) and pops back up.
- `gap` — spacing in front of a key. Top-level `gap` (8–80, default 8) is the
  default; a key's own `gap` overrides it, so a new block starts with `gap: 22`
  on its first key. There is no `group`/`groupGap`.
- `text` on a branch — the agent text area appears on that level and every level
  below it; the string is the idle hint (`t1tb text` overrides it while set).
- `action` — **one form only**: `cmd: <shell line>`, run through `sh -c` in its
  own session with output discarded. Anything you can type in a terminal works
  (`cmd: wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%+`, `cmd: wtype -k F5`, …). The
  renderer has no builtins; the shipped config just calls `t1tb`.
- The top level holds only layout: `buttonWidth`, `gap`, `levelGap`, `children`.
  Nothing in the file names a mode, a session, a prompt or a window.
- `windowMatch` — optional list of keywords on a node (class / title / process
  cmdline of a terminal's children) telling `t1tb insert` and the text-area tap
  which window to focus. The innermost node on the current path wins, so the
  agent branch owns its own list; with no declaration anywhere the renderer falls
  back to `omarchy.agent` / `hermes`. Point it at whatever CLI you actually run
  (`windowMatch: ["codex", "opencode"]`).
- Buttons render left-to-right in array order: the first entry sits leftmost,
  the last one next to the level key.
- A file that will not parse falls back to the built-in layout **and** shows an
  `omarchy-osd` notice (`配置有误(view.json5)：…`), so a typo cannot silently pass
  as "the bar looks fine".

## Agent side

```sh
t1tb goto AI -t "等待审批：允许执行 rm -rf build/ 吗？"   # jump to a node + set the text
t1tb text -t "正在跑测试"                                # update the text, stay here
t1tb normal                                          # back to the root row
t1tb root | status | quit
```

Socket: `~/.local/state/t1-touchbar/ai.sock` (one JSON line in, one out).
Every tap is appended to `~/.local/state/t1-touchbar/ai-events.jsonl`.
Logs: `ai-renderer.log`, `ai-stdout.log`.

Besides those, the socket takes `{"cmd":"insert","text":"…","submit":true}` (focus
the agent window + paste), `{"cmd":"close"}` (close that window) and
`{"cmd":"display","up":true|false}` (one panel-backlight step through the T1,
same path as the `亮 +/−` keys).

## Screen brightness

`亮 +` / `亮 −` send the t1bridge `STEP_DISPLAY` opcode, i.e. they step the
**panel** backlight (observed in `/sys/class/backlight/acpi_video0`,
`max_brightness` 90, one step = 6 units). The T1 applies it asynchronously, so
`step_display()` polls `actual_brightness` for up to ~300 ms and then shows
`omarchy-osd -i brightness -p <pct>` — the keys are silent otherwise.

The Touch Bar strip itself has **no** host brightness channel on this machine:
`/sys/class/backlight` holds only `acpi_video0`, `/sys/class/leds` only
`spi::kbd_backlight`, and the `card0-USB-1` connector exposes EDID/DPMS/
link-status/TILE/orientation but no brightness property. Its pixels are drawn at
fixed colours, so a dimmer-looking strip means either the T1 firmware scaling
the OLED with the panel backlight or an ambient-light illusion.

## Caveats

- The stock user unit `t1-touchbar.service` is **disabled on purpose** — two
  renderers cannot coexist. Re-enable it only after disabling this plugin,
  otherwise the Touch Bar stays black.
- `launch.sh` also stops the stock unit on every start, so a manual
  `systemctl --user start t1-touchbar` gets overridden.
- Focus/typing needs Hyprland ≥ 0.56 Lua dispatch:
  `hyprctl dispatch 'hl.dsp.focus({window="address:0x…"})'`. The classic
  `hyprctl dispatch focuswindow address:0x…` form is a parse error there.
- Injection targets the agent window by class/title match
  (`omarchy.agent` / `hermes`). If nothing matches you get an OSD saying so.
