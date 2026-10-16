"""Докосмотр: one Flask process, one Docker sampler, no application database."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import selectors
import socket
import subprocess
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlsplit

import psutil
from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, request, send_from_directory, session
from flask_socketio import SocketIO
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parent
ID_PATTERN = re.compile(r"[a-f0-9]{12,64}\Z")
USER_PATTERN = re.compile(r"[a-zA-Z0-9_.-]{3,40}\Z")
VERSION = "0.3.1"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class DockerError(Exception):
    pass


def docker_command(*args, timeout=20, limit=16 * 1024 * 1024):
    """No shell; bounded duration and output, including stdout + stderr."""
    try:
        process = subprocess.Popen(["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise DockerError("Docker CLI недоступен") from exc
    output = {"out": bytearray(), "err": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "out")
            selector.register(process.stderr, selectors.EVENT_READ, "err")
            total = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DockerError("Docker не ответил вовремя")
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise DockerError("Ответ Docker превысил допустимый размер")
                    output[key.data].extend(chunk)
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if code:
            # CLI errors may contain host paths or configuration; don't send them to browsers.
            raise DockerError("Docker отклонил запрос. Проверь состояние контейнера и доступ к сокету")
        return output["out"].decode("utf-8", "replace"), output["err"].decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        raise DockerError("Docker не ответил вовремя") from exc
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()


def number(value):
    try:
        return float(str(value).rstrip("%"))
    except (ValueError, TypeError):
        return None


def memory_bytes(value):
    match = re.match(r"([\d.]+)\s*([A-Za-z]+)", str(value))
    if not match:
        return 0
    units = {"B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3,
             "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    return float(match[1]) * units.get(match[2], 1)


def read_containers():
    raw, _ = docker_command("ps", "--all", "--no-trunc", "--format", "{{json .}}")
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    details = {}
    # Only request fields used by the UI; never serialize Config.Env or arbitrary labels.
    template = ('{"id":{{json .Id}},"state":{{json .State.Status}},'
                '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}},'
                '"restarts":{{.RestartCount}},"started":{{json .State.StartedAt}},'
                '"project":{{json (index .Config.Labels "com.docker.compose.project")}}}')
    ids = [row["ID"] for row in rows if ID_PATTERN.fullmatch(row["ID"])]
    for offset in range(0, len(ids), 100):
        try:
            raw, _ = docker_command("inspect", "--format", template, *ids[offset:offset + 100])
        except DockerError:
            # A container can disappear between ps and inspect; next sample reconciles it.
            continue
        for line in raw.splitlines():
            detail = json.loads(line)
            details[detail["id"]] = detail
    metrics = {}
    stats_error = None
    if any(row.get("State") == "running" for row in rows):
        try:
            raw, _ = docker_command("stats", "--no-stream", "--no-trunc", "--format", "{{json .}}")
            metrics = {x["ID"]: x for x in (json.loads(line) for line in raw.splitlines() if line.strip())}
        except DockerError as exc:
            stats_error = str(exc)
    containers = []
    for row in rows:
        cid = row["ID"]
        if not ID_PATTERN.fullmatch(cid):
            continue
        detail, metric = details.get(cid, {}), metrics.get(cid, {})
        state = detail.get("state", row.get("State", "unknown"))
        containers.append({
            "id": cid, "name": row["Names"], "image": row["Image"], "state": state,
            "status": row.get("Status", ""), "ports": row.get("Ports", ""),
            "created": row.get("CreatedAt", ""), "started": detail.get("started"),
            "health": detail.get("health", "unknown"), "restarts": detail.get("restarts"),
            "project": detail.get("project") or "Без проекта", "running": state == "running",
            "cpu": number(metric.get("CPUPerc")), "memory_percent": number(metric.get("MemPerc")),
            "memory": metric.get("MemUsage"), "memory_bytes": memory_bytes(metric.get("MemUsage")),
            "network": metric.get("NetIO"), "disk_io": metric.get("BlockIO"),
            "pids": metric.get("PIDs"),
            "protected": cid.startswith(os.environ.get("HOSTNAME", "__none__")),
        })
    return containers, stats_error


def system_stats(config):
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(config["DISK_PATH"])
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "memory_percent": memory.percent, "memory_used": memory.used, "memory_total": memory.total,
        "disk_percent": disk.percent, "disk_used": disk.used, "disk_total": disk.total,
        "uptime": max(0, int(time.time() - psutil.boot_time())),
        "scope": "linux-host" if config["HOST_PROC"] else "runtime",
        "disk_label": "Файловая система данных",
    }


def create_app(overrides=None):
    load_dotenv(ROOT / ".env")
    app = Flask(__name__, static_folder=None)
    data_dir = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    app.config.update(
        DATA_DIR=data_dir, READ_ONLY=os.getenv("READ_ONLY", "true").lower() != "false",
        SESSION_COOKIE_NAME="dokosmotr_csrf", SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        MAX_CONTENT_LENGTH=4096, PUBLIC_ORIGIN=os.getenv("PUBLIC_ORIGIN", "").rstrip("/"),
        SESSION_TTL=int(os.getenv("SESSION_TTL", "28800")), UPDATE_INTERVAL=max(3, int(os.getenv("UPDATE_INTERVAL", "5"))),
        HOST_PROC=os.getenv("HOST_PROC", ""), DISK_PATH=os.getenv("DISK_PATH", str(data_dir)),
        SERVER_LABEL=os.getenv("SERVER_LABEL", "Docker server"), TESTING=False,
    )
    if overrides:
        app.config.update(overrides)
    # An empty origin supports direct LAN access through any IP/hostname and mapped port.
    # A configured origin pins a canonical address for HTTPS reverse proxies.
    origin = urlsplit(app.config["PUBLIC_ORIGIN"])
    app.config["TRUSTED_HOSTS"] = None
    if app.config["PUBLIC_ORIGIN"]:
        if origin.scheme not in {"http", "https"} or not origin.hostname or origin.path not in {"", "/"} or origin.query or origin.fragment or origin.username:
            raise ValueError("PUBLIC_ORIGIN must be an http(s) origin without path or credentials")
        if origin.scheme == "https" and not app.config["SESSION_COOKIE_SECURE"]:
            raise ValueError("HTTPS requires COOKIE_SECURE=true")
        app.config["TRUSTED_HOSTS"] = [origin.hostname, "localhost", "127.0.0.1", "::1"]
    data_dir = Path(app.config["DATA_DIR"])
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Sessions are process-local and revoked on restart; the signing key need not persist.
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    users_file = data_dir / "users.json"
    users = json.loads(users_file.read_text()) if users_file.exists() else {}
    sio = SocketIO(app, async_mode="threading", cors_allowed_origins=[app.config["PUBLIC_ORIGIN"]] if app.config["PUBLIC_ORIGIN"] else None,
                   max_http_buffer_size=4096, logger=False, engineio_logger=False)
    app.extensions["dashboard_socket"] = sio
    audit = logging.getLogger(f"dokosmotr.audit.{id(app)}")
    audit.setLevel(logging.INFO)
    audit.propagate = False
    handler = RotatingFileHandler(data_dir / "audit.jsonl", maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit.addHandler(handler)
    os.chmod(data_dir / "audit.jsonl", 0o600)
    lock = threading.RLock()
    action_lock = threading.Lock()
    log_slots = threading.BoundedSemaphore(4)
    sessions = {}
    clients = {}
    attempts = defaultdict(deque)
    global_attempts = deque()
    history = deque(maxlen=120)
    snapshot = {"containers": [], "stats": None, "history": [], "timestamp": None,
                "docker_available": False, "error": "Подключаемся к Docker…", "stats_error": None,
                "server": app.config["SERVER_LABEL"], "version": VERSION,
                "update_interval": app.config["UPDATE_INTERVAL"]}
    app.extensions["dashboard_state"] = {"sessions": sessions, "snapshot": snapshot, "clients": clients}
    dummy_hash = generate_password_hash(secrets.token_hex(24))

    def record(event, **fields):
        audit.info(json.dumps({"time": utc_now(), "event": event, **fields}, ensure_ascii=False))

    def token_hash():
        return hashlib.sha256(request.cookies.get("dokosmotr_auth", "").encode()).hexdigest()

    def current_user():
        key = token_hash()
        with lock:
            entry = sessions.get(key)
            if entry and entry["expires"] > time.time():
                return entry
            sessions.pop(key, None)
        return None

    def authorized(role=None):
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                g.user = current_user()
                if not g.user:
                    return jsonify(error="Сессия закончилась. Войди снова"), 401
                if role and g.user["role"] != role:
                    return jsonify(error="Недостаточно прав"), 403
                return func(*args, **kwargs)
            return wrapper
        return decorator

    @app.before_request
    def security_gate():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            allowed_origin = app.config["PUBLIC_ORIGIN"] or request.host_url.rstrip("/")
            if request.headers.get("Origin") not in {None, allowed_origin}:
                return jsonify(error="Недопустимый источник запроса"), 403
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                return jsonify(error="Межсайтовый запрос отклонён"), 403
            expected = session.get("csrf", "")
            supplied = request.headers.get("X-CSRF-Token", "")
            if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
                return jsonify(error="Обнови страницу и повтори запрос"), 403

    @app.after_request
    def headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/healthz")
    def health():
        return jsonify(status="ok", version=VERSION)

    @app.get("/")
    def index():
        if not current_user():
            return redirect("/login")
        return send_from_directory(ROOT / "frontend", "index.html")

    @app.get("/login")
    def login_page():
        if current_user():
            return redirect("/")
        return send_from_directory(ROOT / "frontend", "login.html")

    @app.get("/assets/<path:filename>")
    def asset(filename):
        if not filename.startswith(("css/", "js/", "vendor/")):
            return "", 404
        return send_from_directory(ROOT / "frontend", filename)

    @app.get("/api/session")
    def session_info():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        user = current_user()
        return jsonify(csrf=session["csrf"], authenticated=bool(user),
                       username=user["username"] if user else None, role=user["role"] if user else None,
                       read_only=app.config["READ_ONLY"], setup_required=not bool(users), version=VERSION)

    @app.post("/api/login")
    def login():
        now = time.time()
        ip = request.remote_addr or "unknown"
        with lock:
            for key in list(attempts):
                while attempts[key] and attempts[key][0] < now - 300:
                    attempts[key].popleft()
                if not attempts[key]:
                    del attempts[key]
            while global_attempts and global_attempts[0] < now - 60:
                global_attempts.popleft()
            if len(attempts[ip]) >= 5 or len(global_attempts) >= 30:
                return jsonify(error="Слишком много попыток. Подожди 5 минут"), 429, {"Retry-After": "300"}
            attempts[ip].append(now)
            global_attempts.append(now)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Ожидается JSON-объект"), 400
        username, password = body.get("username"), body.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 1024:
            return jsonify(error="Некорректные данные входа"), 400
        user = users.get(username, {})
        valid = check_password_hash(user.get("password_hash", dummy_hash), password)
        if not valid or not user:
            record("login_failed", username=username[:40], ip=ip)
            return jsonify(error="Неверный логин или пароль"), 401
        token = secrets.token_urlsafe(32)
        with lock:
            for key in list(sessions):
                if sessions[key]["expires"] <= now:
                    del sessions[key]
            if len(sessions) >= 100:
                del sessions[min(sessions, key=lambda key: sessions[key]["expires"])]
            sessions[hashlib.sha256(token.encode()).hexdigest()] = {
                "username": username, "role": user["role"], "expires": now + app.config["SESSION_TTL"]}
            attempts.pop(ip, None)
        session.clear()
        session["csrf"] = secrets.token_urlsafe(32)
        response = jsonify(success=True)
        response.set_cookie("dokosmotr_auth", token, max_age=app.config["SESSION_TTL"],
                            httponly=True, secure=app.config["SESSION_COOKIE_SECURE"], samesite="Strict")
        record("login", username=username, ip=ip)
        return response

    @app.post("/api/logout")
    @authorized()
    def logout():
        key = token_hash()
        with lock:
            sessions.pop(key, None)
            disconnected = [sid for sid, value in clients.items() if value == key]
        for sid in disconnected:
            sio.server.disconnect(sid)
        record("logout", username=g.user["username"])
        session.clear()
        response = jsonify(success=True)
        response.delete_cookie("dokosmotr_auth", secure=app.config["SESSION_COOKIE_SECURE"], httponly=True, samesite="Strict")
        return response

    @app.get("/api/snapshot")
    @authorized()
    def get_snapshot():
        with lock:
            return jsonify(snapshot)

    @app.get("/api/containers")
    @authorized()
    def containers():
        with lock:
            return jsonify(success=True, data=snapshot["containers"], timestamp=snapshot["timestamp"])

    @app.get("/api/stats")
    @authorized()
    def stats():
        with lock:
            return jsonify(success=True, data=snapshot["stats"])

    def valid_id(cid):
        return ID_PATTERN.fullmatch(cid) is not None

    @app.get("/api/container/<cid>/logs")
    @authorized()
    def logs(cid):
        if not valid_id(cid):
            return jsonify(error="Некорректный ID контейнера"), 400
        try:
            tail = int(request.args.get("tail", "200"))
        except ValueError:
            return jsonify(error="Число строк должно быть целым"), 400
        if not 1 <= tail <= 2000:
            return jsonify(error="Число строк должно быть от 1 до 2000"), 400
        if not log_slots.acquire(blocking=False):
            return jsonify(error="Слишком много запросов логов. Попробуй через несколько секунд"), 429
        try:
            out, err = docker_command("logs", "--tail", str(tail), "--timestamps", cid, limit=2 * 1024 * 1024)
            # Docker CLI splits stdout/stderr; sort timestamped lines to restore chronology.
            lines = (out + err).splitlines()
            lines.sort(key=lambda line: line.split(" ", 1)[0])
            return jsonify(logs="\n".join(lines), container=cid)
        except DockerError as exc:
            return jsonify(error=str(exc)), 502
        finally:
            log_slots.release()

    @app.post("/api/container/<cid>/<action>")
    @authorized("admin")
    def container_action(cid, action):
        if app.config["READ_ONLY"]:
            return jsonify(error="Включён режим наблюдения"), 403
        if not valid_id(cid) or action not in {"start", "stop", "restart"}:
            return jsonify(error="Недопустимый ID или действие"), 400
        with lock:
            container = next((c for c in snapshot["containers"] if c["id"] == cid), None)
        if not container:
            return jsonify(error="Контейнер не найден. Обнови список"), 404
        if container["protected"]:
            return jsonify(error="Панель защищена от остановки через собственный интерфейс"), 403
        if not action_lock.acquire(blocking=False):
            return jsonify(error="Другая операция ещё выполняется"), 409
        try:
            docker_command(action, cid, timeout=45)
            record("container_action", username=g.user["username"], container=cid, action=action, result="ok")
            return jsonify(success=True)
        except DockerError as exc:
            record("container_action", username=g.user["username"], container=cid, action=action, result="error")
            return jsonify(error=str(exc)), 502
        finally:
            action_lock.release()

    @sio.on("connect")
    def connected(auth=None):
        if not current_user():
            return False
        with lock:
            if len(clients) >= 100:
                return False
            clients[request.sid] = token_hash()
            initial = dict(snapshot)
        sio.emit("update", initial, to=request.sid)

    @sio.on("disconnect")
    def disconnected(reason=None):
        with lock:
            clients.pop(request.sid, None)

    def sample_once():
        try:
            items, stats_error = read_containers()
            try:
                host_stats = system_stats(app.config)
            except (OSError, ValueError, psutil.Error):
                host_stats = None
                stats_error = "Метрики системы недоступны"
            now = utc_now()
            with lock:
                if host_stats:
                    history.append({"time": now, "cpu": host_stats["cpu"], "memory": host_stats["memory_percent"]})
                snapshot.update(containers=items, stats=host_stats, history=list(history), timestamp=now,
                                docker_available=True, error=None, stats_error=stats_error)
        except (DockerError, ValueError, KeyError) as exc:
            app.logger.warning("Docker sampling failed: %s", type(exc).__name__)
            with lock:
                snapshot.update(docker_available=False, error="Нет связи с Docker. Показаны последние полученные данные")
        with lock:
            now = time.time()
            for key in list(sessions):
                if sessions[key]["expires"] <= now:
                    del sessions[key]
            recipients = list(clients.items())
            payload = dict(snapshot)
        for sid, key in recipients:
            with lock:
                valid = key in sessions
            if valid:
                sio.emit("update", payload, to=sid)
            else:
                sio.server.disconnect(sid)

    app.extensions["dashboard_sample"] = sample_once
    if not app.config["TESTING"]:
        if app.config["HOST_PROC"]:
            if not Path(app.config["HOST_PROC"], "stat").is_file():
                raise ValueError("HOST_PROC must point to a mounted host /proc")
            psutil.PROCFS_PATH = app.config["HOST_PROC"]
        psutil.cpu_percent(interval=None)

        def monitor():
            while True:
                try:
                    sample_once()
                except Exception:
                    app.logger.exception("Unexpected sampler error")
                sio.sleep(app.config["UPDATE_INTERVAL"])

        sio.start_background_task(monitor)
    return app


def cli():
    parser = argparse.ArgumentParser(description="Докосмотр: управление пользователями")
    parser.add_argument("command", choices=["user"])
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", choices=["admin", "viewer"], default="viewer")
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()
    if not USER_PATTERN.fullmatch(args.username):
        parser.error("Имя: 3–40 латинских букв, цифр, точек, дефисов или подчёркиваний")
    import getpass
    import sys
    password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Пароль (от 14 символов): ")
    if not 14 <= len(password) <= 1024:
        parser.error("Пароль должен содержать от 14 до 1024 символов")
    if not args.password_stdin and password != getpass.getpass("Повтори пароль: "):
        parser.error("Пароли не совпадают")
    load_dotenv(ROOT / ".env")
    directory = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "users.json"
    # Serialize CLI updates; account storage remains a small JSON file.
    import fcntl
    with (directory / ".users.lock").open("w") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        users = json.loads(path.read_text()) if path.exists() else {}
        users[args.username] = {"password_hash": generate_password_hash(password), "role": args.role}
        temporary = directory / (".users-" + secrets.token_hex(8))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(users, output, indent=2)
        temporary.replace(path)
    print(f"Пользователь {args.username} ({args.role}) сохранён. Перезапусти панель, чтобы применить изменения.")


if __name__ == "__main__":
    cli()
