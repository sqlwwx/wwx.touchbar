# wwx.touchbar-ai

Touch Bar UI for the Omarchy agent on a t1bridge MacBook.

The Touch Bar is a DRM device with exactly one owner
(`/run/t1bridge/touchbar.sock`). This plugin is a `service` plugin: it stops the
stock t1bridge renderer, runs its own renderer, restarts it if it dies, and
stops it when the plugin is unloaded.

```
manifest.json          kind: service
Service.qml            supervisor (Quickshell Process, 3 s restart on exit)
buttons.json5          the whole button tree (JSON5)
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
| `on_human_input_request` | AI mode + `等待审批：<prompt>` (or 需要密码 / 需要回答), spoken aloud |
| `on_human_input_resolved` | back to the normal bar, or to the next still-pending prompt |

A pending counter in `~/.local/state/t1-touchbar/ai-pending.json` keeps
concurrent prompts apart and expires entries after 10 min, so a crashed turn
cannot pin AI mode. Prompt text is force-redacted by Hermes and truncated to
110 chars. Voice uses `~/.local/share/t1-touchbar/tts/say.py` (edge-tts 7.3.1 +
`pw-play`, detached so the 5 s hook budget is not blown); set Voice is optional and off-switchable, from the bar or from a shell. On the bar
the `声` key (configurable like any other) carries `builtin:voice-toggle` and
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

`AI` is always the rightmost key and toggles the mode, so the same key both
opens and closes AI mode.

| key | does |
| --- | --- |
| esc | real `KEY_ESC` to the focused window |
| text area (AI mode) | focus the agent window, so you can type there |
| AI | toggle normal / ai |
| any `ai[]` key | `action` if set, else its `insert` text + Return |

`生` and `死` are ordinary `ai[]` entries with special actions:
`session:start` (run `birthCommand`, a fresh session in a new terminal) and
`session:end` (inject `deathPrompt` so the agent assesses skills/wiki, tidies up
and ends). Rename, move or drop them like any other key.

CJK text cannot be typed with `wtype`, so non-ASCII inserts go through
`wl-copy` + `Ctrl+Shift+V` (the clipboard is restored afterwards). ASCII inserts
are typed directly.

## buttons.json5

JSON5, reloaded on save — no restart. Comments, trailing commas and bare keys
all work. (JSON5 does **not** forgive a *missing separator* comma — `{a:1 b:2}`
is still an error; it forgives `{a:1, b:2,}`.) A legacy `buttons.json` is still
read if no `.json5` exists.

```json5
{
  buttonWidth: 96,
  groupGap: 22,     // 同 group 相邻 8px，不同 group 之间这个宽度
  levelGap: 44,     // 层级键(‹当前层名)左侧的宽间距
  aiNode: "AI",     // t1tb ai 定位的节点名；只用于定位，不参与布局

  tree: [
    { label: "音 +", action: "builtin:volume-up", group: 0 },
    { label: "音 −", action: "builtin:volume-down", group: 0 },
    { label: "静", action: "builtin:mute", group: 1 },
    { label: "键盘", group: 2, children: [
        { label: "键 +", action: "builtin:kbd-up", group: 0 },
        { label: "键 −", action: "builtin:kbd-down", group: 0 } ] },
    { label: "AI", group: 3, gap: 44, text: "AI 交互模式 · 右侧 ‹AI 回根层",
      children: [
        { label: "生", action: "session:start", group: 0 },
        { label: "死", action: "session:end", group: 0 },
        { label: "善", insert: "善", group: 1 },
        { label: "恶", insert: "no", group: 1 } ] },
  ],

  birthCommand: "omarchy-launch-terminal hermes chat",
  deathPrompt: "…",
}
```

- `buttonWidth` 80–182, default 96. Lower = more room for the text area.
- `tree` — the whole bar, one nested array. `children` makes an entry a
  **branch** instead of a key with an action: the tap descends one level and the
  bar shows that subtree. Branches draw a `›` hint and their own
  `action`/`insert` is ignored. Nesting is capped at 4 levels.
- No entry is special-cased, the `AI` button included — it is a plain branch
  configured in `tree`. At the root there is no level key; one level down the
  rightmost key is the **level key**: it carries the name of the level you are
  in (with a `‹` hint) and pops back up.
- `gap` (8–80) on an entry — left spacing for that key; overrides the
  `group`/`groupGap` rule.
- `text` on a branch — the agent text area appears on that level and every level
  below it; the string is the idle hint (`t1tb text` overrides it while set).
  That is how "AI mode" exists: no `aiHint`, no role, just a field on the node.
- `aiNode` — the label `t1tb ai` jumps to, and the level whose descendants make
  `t1tb status` report `mode: "ai"`. Pure lookup, no layout effect. `t1tb goto
  <label>` reaches any other node.
- `groupGap` (8–60, default 22) — gap between two buttons whose `group` differs.
  Same `group` = ordinary 8 px gap, so pairs (音+/音−, 善/恶) stay tight and
  blocks stay distinguishable.
- `action` prefixes — `builtin:volume-up|volume-down|mute|brightness-up|brightness-down|kbd-up|kbd-down|kbd-toggle|kbd-off|voice-toggle|voice-on|voice-off`,
  `cmd:<shell>`, `keys:<wtype key>`, `paste:<text>`,
  `session:start`, `session:end`.
- `insert` — paste text into the agent window; empty string means "just focus".
  `action` wins over `insert`; `"submit": false` inserts without pressing Return.
  Tap priority per key: `action` → `insert` → focus the agent window.
- `session:start` / `session:end` are the only special actions: they read
  `birthCommand` / `deathPrompt` from this file. Everything else about 生/死 is
  ordinary configuration.
- Buttons render left-to-right in array order: the first entry sits leftmost,
  the last one next to the level key.
- A file that will not parse falls back to the built-in layout **and** shows an
  `omarchy-osd` notice (`buttons.json5 有误：…`), so a typo cannot silently pass
  as "the bar looks fine".

## Agent side

```sh
t1tb ai -t "等待审批：允许执行 rm -rf build/ 吗？"   # switch to AI mode + status text
t1tb text -t "正在跑测试"                            # update the text, stay in AI mode
t1tb normal                                          # back to the normal (root) row
t1tb goto 键盘                                       # jump to any tree node by label
t1tb root                                            # alias of normal
t1tb toggle | status | quit
```

Socket: `~/.local/state/t1-touchbar/ai.sock` (one JSON line in, one out).
Every tap is appended to `~/.local/state/t1-touchbar/ai-events.jsonl`.
Logs: `ai-renderer.log`, `ai-stdout.log`.

Besides the `t1tb` verbs the socket takes `{"cmd":"display","up":true|false}` —
one panel-backlight step through the T1 (same path as the `亮 +/−` keys).

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
