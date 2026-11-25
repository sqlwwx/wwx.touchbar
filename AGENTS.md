# wwx.touchbar

Omarchy `service` 插件：用自绘 renderer 给 t1bridge MacBook 的 Touch Bar 画一棵按钮树，
由 `t1tb` CLI 和 Hermes hook 驱动。`AI` 只是树里普通分支，没有特判。
架构与按键语义见 `README.md`；下面只列容易踩的坑。

```
manifest.json          kind: service → Service.qml（Quickshell Process，退出后 3s 重启）
Service.qml            写死插件路径，卸载时停渲染器
renderer/touchbar.py   渲染器 + 控制 socket（独占 /run/t1bridge/touchbar.sock）
renderer/t1tb.py       控制 CLI（软链 ~/.local/bin/t1tb）
renderer/launch.sh     先停 stock t1-touchbar.service，再 exec 渲染器
view.example.json5     内置默认树（配置缺失时写到用户路径）
hooks/human-input.py   Hermes hook → 跳 AI 层 + 语音播报
```

## 运行环境 / 别找了

- 没有 package.json / pyproject / Makefile / CI / lint / 测试目录；验证靠下面三条命令。
- `touchbar.py` shebang 写死 `/home/wwx/.local/share/t1-touchbar/venv/bin/python3`（3.14，
  只装 json5 + pillow）。系统 `python3` 没有 json5 → 静默降级成严格 JSON（注释/尾逗号全废），
  别拿它调试配置。
- `t1tb.py`、`hooks/human-input.py` 用系统 python，只用标准库，别引入第三方依赖。
- 外部命令：`wpctl`、`omarchy-osd`、`hyprctl`、`wtype`、`wl-copy`、`pw-play`；
  字体 `/usr/share/fonts/noto-cjk/NotoSansCJK-*.ttc`。

## 验证（无测试套件）

```sh
# 离线渲染，不需要硬件/渲染器在跑；改完看 PNG 判断布局，别重启插件试
./renderer/touchbar.py --dump /tmp/tb.png                      # 根层
./renderer/touchbar.py --dump /tmp/tb.png --at AI --text 测试   # 按 label 进层
./renderer/touchbar.py --dump /tmp/tb.png --path 5,0           # 按索引路径进层

/home/wwx/.local/share/t1-touchbar/venv/bin/python3 -m py_compile \
  renderer/touchbar.py renderer/t1tb.py hooks/human-input.py
t1tb status    # 渲染器在跑时经 socket 问一句，返回 JSON
```

- 插件命令：`omarchy-plugin-validate <dir>` / `omarchy-plugin-list | grep touchbar` /
  `omarchy-plugin-enable|disable wwx.touchbar`。
- 配置按 mtime 自动重载；改 Python 要重载插件（或 `pkill -f renderer/touchbar.py`，3s 后拉起）。

## 约定 / 易写错

- 树是唯一抽象：有 `children` 就是分支（其 `action` 被忽略）；叶子只有 `action: "cmd: <shell>"`
  （`sh -c` 独立 session，丢弃输出），没 action 的叶子只聚焦 agent。别往渲染器加 builtin 或 label 特例。
- 节点字段：`label`、`children`、`gap`（左间距）、`text`（向下继承）、`key`（物理键触发）、
  `hidden`、`buttonWidth`（层内可覆盖，Fn 层用它画 F1–F12）、`action`。`windowMatch` 仅兼容保留、不再用于定位。
  顶层只放 `buttonWidth` / `gap` / `levelGap` / `children`。旧文档的 `groupGap` / `aiGap` 已废，
  改用键上的显式 `gap`；JSON5 不原谅缺分隔符逗号。
- 清洗夹取会写日志（不再静默）：`depth > 4` 丢弃该层、`buttonWidth` 80–182、`gap` 4–80。
- 目标窗口三级解析（`TouchBar.agent_window`）：显式 `-w` > hook 锁定的触发者窗口 > 当前聚焦窗口 > 旧关键词兜底。
  hook 用 `getppid()` + `/proc` PPid 链找到拥有 Hyprland 窗口的祖先，随 `goto AI -w <addr>` 锁定；
  `normal`/`root` 清除锁定。`insert` / `close` / `goto` 都带 `-w <addr>`，`status` 回报锁定值。
  这样支持各种 agent，不靠 class/title 猜。gateway/无窗口场景反查返回 None，退回当前聚焦窗口。
- 命令分类：完全本地的是 `volume` / `kbd` / `voice` / `open`（新 session 跑命令，按钮配「打开什么」）；
  `display`、`goto`、`text`、`insert`、`close`、`status` / `normal` / `quit` / `root` 走 socket，要求渲染器在跑，
  否则 `renderer not running`。`finish` 的 arm 判断在本地（TTL 内再击才关），两个动作都发 socket；
  `close` 先 `hl.dsp.window.close`、~1.5s 后仍在就 kill（Electron 只回 ok 却不关窗，缺这步会静默失败）。
- CJK 不能用 `wtype` 打：非 ASCII 的 `insert` 走 `wl-copy` + `Ctrl+Shift+V`（结束后恢复剪贴板）。
- 焦点/注入/关窗依赖 Hyprland ≥ 0.56 的 Lua dispatch `hl.dsp.focus({window="address:0x…"})`；
  旧的 `hyprctl dispatch focuswindow address:…` 在 0.56 是解析错误。
- Python 风格：UPPER_SNAKE 常量集中在模块顶部、函数 snake_case、无类层次、配置键 camelCase；
  注释和 docstring 一律用中文（技术术语、协议字段名、shell 命令保留原文）。
- 两个 CLI 入口同构：`main(argv)` 返回 exit code + 末尾 `raise SystemExit(main(sys.argv[1:]))`；
  `t1tb` 成功 stdout 一行 JSON、失败 stderr + `return 1`。hook 的 `main()` 无参，所有异常都吞掉
  （不能干扰 agent 主循环）。

## 坑

- 独占 DRM 设备 `/run/t1bridge/touchbar.sock`：stock `t1-touchbar.service` 必须保持 disable，
  两个渲染器共存会让触摸条全黑；`launch.sh` 每次启动会再 stop 一次。
- 插件目录是代码（重装整体替换），按钮树是用户数据：`view.json5` 在
  `~/.config/omarchy/wwx.touchbar.view.json5`，`.gitignore` 挡住写回插件目录。
- `Service.qml` / `launch.sh` 里的插件路径写死，改目录名或 id 要同步改（外加 `manifest.json`、`t1tb` 软链）。
- Hook 在会话构建时读取，运行中的 TUI 不会热加载：改 `hooks/human-input.py` 后要重启 TUI。
- 键盘背光写 `/sys/class/leds/spi::kbd_backlight/brightness`，靠 udev 规则：
  `sudo install -m 0644 renderer/90-kbd-backlight-perms.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger --action=change --subsystem-match=leds`
  （trigger 会让 `systemd-backlight@` 重新套用关机时保存的亮度）。
- 运行期文件在 `~/.local/state/t1-touchbar/`：`ai.sock`、`ai-events.jsonl`（每次点击）、
  `ai-renderer.log`、`ai-stdout.log`、`ai-pending.json`（hook 待办计数）、
  `kbd-backlight.json`、`finish-armed.json`。排障先看日志。
- `T1TB_DUMP_DIR=<dir>` 把每帧存 PNG，`T1TB_CONFIG` 覆盖配置路径；语音开关/代理见 README
  （`T1TTS_SPEAK=0` 可临时静音）。

## 提交

`<scope>: <中文一句话>`，scope 用 `config` / `docs` / `touchbar` / `rename`；正文用中文列要点。
