#!/usr/bin/env python3
"""
Hermes shell hook → Touch Bar 的 agent 层。

在 ~/.hermes/config.yaml 里订阅两个事件：

  on_human_input_request   agent 即将等待人工介入
                           （kind = sudo | clarify | approval）
  on_human_input_resolved  该等待结束（已回答 / 超时 / 撤回 …）

收到 request 时，把条形界面跳到树里的 "AI" 节点，并把提示词写进该层的文字区；
最后一个 pending 提示解决后回到根层。节点名是这个 hook 关于配置的全部知识。
状态目录里的一个小计数器让并发的提示不会互相清掉对方的文字。

payload 以 JSON 从 stdin 进来；这些观察者事件忽略 stdout。
任何失败都吞掉：hook 绝不能干扰 agent 主循环。
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
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

# 语音播报：把提示词读出来，这样即使没看条形界面也能发现 agent 被卡住。
# 合成 + 播放以分离进程运行——hook 只有 5 秒预算，而 edge-tts 本身就要 ~1 秒。
#
# 完全可选。优先级：$T1TTS_SPEAK > tts 配置里的 "speak" > 默认开。
# 任一项设为 false/none/0 都让条形界面保持静音。
SAY = HOME / ".local/share/t1-touchbar/tts/say.py"
TTS_CONFIG = HOME / ".local/share/t1-touchbar/tts/tts.json"
TRUTHY = ("1", "true", "yes", "on")

# Touch Bar 能显示两行、每行约 26 个汉字；保留提示词开头。
MAX_CHARS = 110
# 永远等不到 resolved 的提示（回合崩溃、TUI 被杀）不能让界面永远停在 agent 层。
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
    """给条形界面用的一行、限长、无控制字符的文字。"""
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


@contextmanager
def pending_lock():
    """串行化 hook 的读-改-写；加锁失败时退化为无锁。"""
    handle = None
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        handle = PENDING.with_suffix(".lock").open("a+")
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError as exc:
        log(f"pending lock unavailable: {exc}")
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
            except OSError:
                pass


def save_pending(data):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = PENDING.with_name(f"{PENDING.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, PENDING)
    except OSError as exc:
        log(f"pending write failed: {exc}")


def on_request(payload, extra):
    kind = str(extra.get("kind") or "approval")
    request_id = str(extra.get("request_id") or "")
    prompt = clean(extra.get("prompt"))
    label = KIND_LABEL.get(kind, "等待确认")

    with pending_lock():
        pending = load_pending()
        # `hermes hooks test` 触发时没有 request_id；按 kind 作为键，
        # 这样重复的合成运行不会堆积虚假的 pending 提示。
        key = request_id or f"anon:{kind}"
        pending[key] = {
            "kind": kind,
            "prompt": prompt,
            "since": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "epoch": time.time(),
        }
        save_pending(pending)

    text = f"{label}：{prompt}" if prompt else label
    t1tb("goto", "AI", "-t", text)
    spoke = speak_enabled()
    speak(text)
    log(f"request kind={kind} id={request_id} pending={len(pending)} speak={spoke}")


def on_resolved(extra):
    request_id = str(extra.get("request_id") or "")
    outcome = str(extra.get("outcome") or "")
    with pending_lock():
        pending = load_pending()
        if request_id:
            pending.pop(request_id, None)
        else:
            # 没有 id 可匹配：清掉所有合成条目，因为对应的 request 也无法识别。
            for key in [k for k in pending if k.startswith("anon:")]:
                pending.pop(key, None)
        save_pending(pending)

    if pending:
        # 还有提示在等待：显示最新的那个，而不是退回普通条形界面。
        newest = sorted(pending.values(), key=lambda item: item.get("since", ""))[-1]
        label = KIND_LABEL.get(str(newest.get("kind")), "等待确认")
        prompt = newest.get("prompt") or ""
        t1tb("goto", "AI", "-t", f"{label}：{prompt}" if prompt else label)
    else:
        t1tb("normal")
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
