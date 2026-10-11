"""Optional local OS reminder. Inbox remains the durable notification receipt."""
import json
import shutil
import subprocess
import sys


def available():
    return sys.platform == "darwin" and bool(shutil.which("osascript"))


def send(title):
    if not available():
        return {"status": "unavailable", "message": "当前后台宿主不支持 macOS 系统提醒。"}
    text = "".join(c for c in str(title) if ord(c) >= 32)[:160]
    # One quoted literal, never a shell command or an arbitrary script from a source.
    script = "display notification " + json.dumps(text, ensure_ascii=False) + ' with title "Morning"'
    try:
        result = subprocess.run([shutil.which("osascript"), "-e", script], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        if result.returncode == 0:
            return {"status": "requested", "message": "已请求系统显示提醒；显示方式由系统通知权限和专注模式决定。"}
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"status": "failed", "message": "系统提醒没有发出，请检查 macOS 通知设置。Inbox 中的消息仍保留。"}
