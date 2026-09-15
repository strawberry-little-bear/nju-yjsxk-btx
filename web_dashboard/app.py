"""Web Dashboard for NJU Course Selection Assistant.

Serves a single-page control panel that reads state.json and log files
to display real-time status, and controls the main script via subprocess.

Usage:
    python web_dashboard/app.py
    # Then open http://localhost:5000
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = BASE_DIR / "state.json"
COURSES_PATH = BASE_DIR / "courses.json"
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "log"
MAIN_SCRIPT = BASE_DIR / "nju_yjsxk_btx.py"
PROCESS_MARKER = BASE_DIR / ".dashboard-process.json"

# Prefer the project's virtual environment Python if it exists,
# otherwise fall back to sys.executable.
_VENV_PYTHON = (
    BASE_DIR / ".venv" / "Scripts" / "python.exe"
    if os.name == "nt"
    else BASE_DIR / ".venv" / "bin" / "python3"
)
PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.is_file() else sys.executable

# ---- Process management ----

_process: subprocess.Popen | None = None
_process_lock = threading.RLock()
_start_args: list[str] = []
_external_pid: int | None = None
_CONTROL_TOKEN = os.environ.get("NJU_DASHBOARD_TOKEN") or secrets.token_urlsafe(32)


def _pid_is_alive(pid: int) -> bool:
    """Return whether a process id currently exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _write_process_marker(pid: int, args: list[str]) -> None:
    payload = {
        "pid": pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "args": args,
    }
    temporary = PROCESS_MARKER.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(PROCESS_MARKER)


def _clear_process_marker() -> None:
    try:
        PROCESS_MARKER.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _recover_external_process() -> None:
    """Recover a process started by a previous Dashboard instance."""
    global _external_pid
    if _external_pid is not None or _process is not None:
        return
    marker = _read_json(PROCESS_MARKER, {})
    pid = marker.get("pid") if isinstance(marker, dict) else None
    if isinstance(pid, int) and _pid_is_alive(pid):
        _external_pid = pid
    elif marker:
        _clear_process_marker()


def is_running() -> bool:
    global _process, _external_pid, _start_args
    with _process_lock:
        if _process is not None:
            running = _process.poll() is None
            if not running:
                _process = None
                _start_args = []
                _clear_process_marker()
            return running
        _recover_external_process()
        if _external_pid is None:
            return False
        if _pid_is_alive(_external_pid):
            return True
        _external_pid = None
        _clear_process_marker()
        return False


def start_process(args: list[str]) -> dict:
    """Start the main script as a subprocess."""
    global _process, _start_args, _external_pid
    with _process_lock:
        # 使用 RLock，允许状态检查复用同一把进程锁。
        if is_running():
            return {"ok": False, "error": "脚本已在运行中"}
        cmd = [PYTHON, str(MAIN_SCRIPT)] + args
        try:
            _process = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            _external_pid = None
            try:
                _write_process_marker(_process.pid, args)
            except OSError:
                _process.terminate()
                _process.wait(timeout=5)
                _process = None
                return {"ok": False, "error": "无法写入 Dashboard 进程标记文件"}
            _start_args = args
            return {
                "ok": True,
                "pid": _process.pid,
                "cmd": " ".join(cmd),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def _terminate_process_tree(pid: int, force: bool = False) -> None:
    """Best-effort termination of a process and its children.

    A process that has already exited counts as success: callers report a
    friendly result instead of failing with ProcessLookupError.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        # 进程（组）已经结束：脚本自行退出，或刚好在检查之后退出。
        pass


def stop_process() -> dict:
    """Stop the running subprocess."""
    global _process, _external_pid, _start_args
    with _process_lock:
        _recover_external_process()
        if _process is None and _external_pid is None:
            return {"ok": True, "message": "没有运行中的脚本"}
        exited_on_its_own = False
        try:
            if _process is not None:
                # 先确认子进程是否还活着，脚本自行退出时不应报错。
                if _process.poll() is not None:
                    exited_on_its_own = True
                else:
                    _terminate_process_tree(_process.pid)
                    try:
                        _process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        _terminate_process_tree(_process.pid, force=True)
                        try:
                            _process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            # 进程杀不掉时不要无限等待（会一直占着进程锁，
                            # 让整个控制台失去响应），保留状态供用户重试。
                            return {
                                "ok": False,
                                "error": f"无法结束脚本进程（PID {_process.pid}），请手动结束该进程后重试",
                            }
            elif _external_pid is not None:
                _terminate_process_tree(_external_pid)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        _process = None
        _external_pid = None
        _start_args = []
        _clear_process_marker()
        if exited_on_its_own:
            return {"ok": True, "message": "脚本已自行结束"}
        return {"ok": True, "message": "已停止"}


# ---- State helpers ----


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _safe_read_text(path: Path, default="") -> str:
    if not path.exists():
        return default
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return default


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON beside the target and replace it atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _latest_log_path() -> Path | None:
    if not LOG_DIR.exists():
        return None
    logs = sorted(LOG_DIR.glob("run_*.log"), key=os.path.getmtime)
    return logs[-1] if logs else None


def _latest_login_status() -> str:
    """Determine login state from the newest login-related log event."""
    events = (
        ("logged_in", ("登录成功", "已进入课程页面")),
        ("captcha_error", ("验证码不正确", "验证码错误")),
        ("login_failed", ("等待登录成功超时", "登录失败")),
        ("expired", ("检测到登录已失效", "未登录不能选课", "登录过期")),
    )
    latest = None
    if LOG_DIR.exists():
        try:
            log_files = sorted(LOG_DIR.glob("run_*.log"), key=os.path.getmtime)
            for log_file in log_files[-3:]:
                for line in _safe_read_text(log_file, "").splitlines():
                    for status, markers in events:
                        if any(marker in line for marker in markers):
                            latest = status
                            break
        except OSError:
            pass
    return latest or "unknown"


# ---- SSE log streaming ----


class LogStreamer:
    """Background thread that tails the latest log file and pushes to SSE clients."""

    def __init__(self):
        self._clients: list = []
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(target=self._tail_loop, daemon=True)
        self._thread.start()

    def add_client(self):
        q: list[str] = []
        with self._lock:
            self._clients.append(q)
        return q

    def remove_client(self, q):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def _push(self, line: str):
        with self._lock:
            for q in self._clients:
                q.append(line)

    def _tail_loop(self):
        last_path = None
        last_size = 0
        while self._running:
            log_path = _latest_log_path()
            if log_path is None:
                time.sleep(1)
                continue
            if log_path != last_path:
                last_path = log_path
                last_size = 0
            try:
                size = log_path.stat().st_size
                if size > last_size:
                    with log_path.open(encoding="utf-8") as f:
                        f.seek(last_size)
                        for line in f:
                            self._push(line.rstrip("\n"))
                        last_size = f.tell()
                elif size < last_size:
                    # log was rotated / truncated
                    last_size = 0
            except OSError:
                pass
            time.sleep(0.5)

    def stop(self):
        self._running = False


_streamer = LogStreamer()


# ---- Routes ----


@app.route("/")
def index():
    return render_template("dashboard.html", dashboard_token=_CONTROL_TOKEN)


@app.before_request
def require_control_token():
    """Protect state-changing endpoints from cross-site requests."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if request.headers.get("X-Dashboard-Token", "") != _CONTROL_TOKEN:
        return jsonify({"ok": False, "error": "缺少或无效的 Dashboard 控制令牌"}), 403
    return None


@app.route("/api/config")
def api_config():
    courses = _read_json(COURSES_PATH, {"courses": []}).get("courses", [])
    return jsonify({"courses": courses})


@app.route("/api/status")
def api_status():
    running = is_running()
    state = _read_json(STATE_PATH, {
        "successful_courses": [],
        "pending_keywords": [],
        "last_failure_reason": "",
        "refresh_count": 0,
        "last_run_at": "",
    })
    courses_data = _read_json(COURSES_PATH, {"courses": []}).get("courses", [])
    all_keywords = []
    for c in courses_data:
        if c.get("enabled", True):
            all_keywords.extend(c.get("keywords", []))
    pending = state.get("pending_keywords", [])
    successful = state.get("successful_courses", [])
    # Build per-course status
    course_statuses = []
    for c in courses_data:
        if not c.get("enabled", True):
            continue
        kw = c.get("keywords", [])
        matched_pending = [k for k in kw if k in pending]
        matched_success = [k for k in kw if any(k in s for s in successful)]
        if matched_success:
            status = "success"
            detail = "已选成功"
        elif matched_pending:
            status = "pending"
            detail = "待选"
        else:
            status = "unknown"
            detail = "未找到"
        course_statuses.append({
            "name": c.get("name", ""),
            "keywords": kw,
            "status": status,
            "detail": detail,
        })
    login_status = _latest_login_status()

    return jsonify({
        "running": running,
        "refresh_count": state.get("refresh_count", 0),
        "last_run_at": state.get("last_run_at", ""),
        "last_failure_reason": state.get("last_failure_reason", ""),
        "courses": course_statuses,
        "login_status": login_status,
        "pid": (_process.pid if _process is not None else _external_pid) if running else None,
    })


@app.route("/api/logs")
def api_logs():
    tail = request.args.get("tail", 200, type=int)
    tail = max(1, min(tail or 200, 1000))
    log_path = _latest_log_path()
    if log_path is None or not log_path.exists():
        return jsonify({"lines": [], "path": None})
    try:
        text = _safe_read_text(log_path, "")
        lines = text.splitlines()
        return jsonify({"lines": lines[-tail:], "path": str(log_path.name)})
    except OSError:
        return jsonify({"lines": [], "path": None})


@app.route("/api/logs/stream")
def api_logs_stream():
    def event_stream():
        queue = _streamer.add_client()
        try:
            # Send initial heart-beat
            yield f": heartbeat\n\n"
            while True:
                if queue:
                    line = queue.pop(0)
                    yield f"data: {line}\n\n"
                else:
                    yield f": tick\n\n"
                time.sleep(0.3)
        except GeneratorExit:
            pass
        finally:
            _streamer.remove_client(queue)

    return Response(event_stream(), mimetype="text/event-stream",
                   headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "manual")
    dry_run = data.get("dry_run", False)
    courses_file = data.get("courses", "courses.json")
    min_interval = data.get("min_interval", 60)
    max_interval = data.get("max_interval", 300)
    login_attempts = data.get("login_max_attempts", 6)

    if mode not in {"manual", "auto"}:
        return jsonify({"ok": False, "error": "登录模式无效"}), 400
    if not isinstance(dry_run, bool):
        return jsonify({"ok": False, "error": "dry_run 必须是布尔值"}), 400
    if not isinstance(courses_file, str) or not courses_file.strip():
        return jsonify({"ok": False, "error": "课程文件名无效"}), 400
    try:
        min_interval = float(min_interval)
        max_interval = float(max_interval)
        login_attempts = int(login_attempts)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "刷新间隔和登录次数必须是数字"}), 400
    try:
        courses_path = (BASE_DIR / courses_file).resolve()
        courses_path.relative_to(BASE_DIR)
    except (OSError, ValueError):
        return jsonify({"ok": False, "error": "课程文件必须位于项目目录内"}), 400
    if not courses_path.is_file():
        return jsonify({"ok": False, "error": "课程文件不存在"}), 400
    if min_interval <= 0 or max_interval < min_interval or login_attempts <= 0:
        return jsonify({"ok": False, "error": "参数必须满足间隔为正、最大间隔不小于最小间隔、登录次数为正"}), 400

    args = [f"--courses={courses_file}"]
    if dry_run:
        args.append("--dry-run")
    if mode == "auto":
        args.append("--use-login-helper")
    args += [
        f"--min-interval={min_interval}",
        f"--max-interval={max_interval}",
        f"--login-max-attempts={login_attempts}",
    ]
    result = start_process(args)
    return jsonify(result)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    result = stop_process()
    return jsonify(result)


@app.route("/api/process")
def api_process():
    running = is_running()
    pid = None
    cmd = None
    if running:
        pid = _process.pid if _process is not None else _external_pid
        marker = _read_json(PROCESS_MARKER, {})
        args = marker.get("args", _start_args) if isinstance(marker, dict) else _start_args
        cmd = " ".join([PYTHON, str(MAIN_SCRIPT)] + args)
    return jsonify({"running": running, "pid": pid, "cmd": cmd})


# ---- Config management ----

@app.route("/api/config/account")
def api_config_account():
    """Return account info. Never expose the actual password."""
    config = _read_json(CONFIG_PATH, {})
    has_password = bool(config.get("PassWd", "").strip())
    return jsonify({
        "user_id": config.get("UserId", ""),
        "url": config.get("url", ""),
        "has_password": has_password,
    })


@app.route("/api/config/account", methods=["POST"])
def api_config_account_save():
    """Update account config. Password field only updates if non-empty."""
    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "").strip()
    url = (data.get("url") or "").strip()
    password = data.get("password", "") or ""

    if not user_id or not url:
        return jsonify({"ok": False, "error": "学号和 URL 不能为空"})
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        return jsonify({"ok": False, "error": "URL 必须是有效的 http 或 https 地址"})

    # Read existing config to preserve values we don't update
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return jsonify({"ok": False, "error": "config.json 无法读取，请先修复文件"}), 400
    else:
        config = {}
    config["UserId"] = user_id
    config["url"] = url
    if password:
        config["PassWd"] = password
    # If password is empty string, keep existing (user didn't change it)

    try:
        _atomic_write_json(CONFIG_PATH, config)
        return jsonify({"ok": True})
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/config/courses")
def api_config_courses():
    """Return full courses list for editing."""
    data = _read_json(COURSES_PATH, {"courses": []})
    try:
        modified_at = datetime.fromtimestamp(COURSES_PATH.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        modified_at = ""
    return jsonify({**data, "modified_at": modified_at})


@app.route("/api/config/courses", methods=["POST"])
def api_config_courses_save():
    """Save updated courses list."""
    data = request.get_json(silent=True) or {}
    courses = data.get("courses")
    if not isinstance(courses, list):
        return jsonify({"ok": False, "error": "courses 必须是数组"})

    # Basic validation
    for c in courses:
        if not isinstance(c, dict):
            return jsonify({"ok": False, "error": "每个课程必须是对象"})
        if "name" not in c or "keywords" not in c:
            return jsonify({"ok": False, "error": "课程缺少 name 或 keywords 字段"})
        if not isinstance(c["name"], str):
            return jsonify({"ok": False, "error": "课程 name 必须是字符串"})
        if not isinstance(c["keywords"], list):
            return jsonify({"ok": False, "error": "keywords 必须是数组"})
        if any(not isinstance(keyword, str) or not keyword.strip() for keyword in c["keywords"]):
            return jsonify({"ok": False, "error": "keywords 必须是非空字符串数组"})
        if "enabled" in c and not isinstance(c["enabled"], bool):
            return jsonify({"ok": False, "error": "enabled 必须是布尔值"})

    try:
        _atomic_write_json(COURSES_PATH, {"courses": courses})
        return jsonify({"ok": True, "count": len(courses)})
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)})


# ---- Entry point ----


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Web Dashboard for NJU Course Selection")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    args = parser.parse_args()

    try:
        app.run(host=args.host, port=args.port, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        _streamer.stop()
        stop_process()
