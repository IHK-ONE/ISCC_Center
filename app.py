import copy
import csv
import hashlib
import io
import json
import os
import random
import re
import secrets
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
import urllib3
from flask import Flask, jsonify, render_template, request, send_file, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DOWNLOAD_DIR = APP_DIR / "downloads"
CONFIG_FILE = DATA_DIR / "config.json"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
CHALLENGES_FILE = DATA_DIR / "challenges.json"
SOLVES_FILE = DATA_DIR / "solves.json"
FILES_FILE = DATA_DIR / "files.json"
FLAGS_FILE = DATA_DIR / "flags.json"
ISCC_BASE_URL = "https://iscc.isclab.org.cn"
ISCC_RETRY_ATTEMPTS = 3
OPERATION_DELAY_DEFAULT_SECONDS = 0
OPERATION_DELAY_MAX_SECONDS = 60
CHALLENGE_SOURCE_DEFAULT = "challenge"
CHALLENGE_SOURCE_ARENA = "arena"
CHALLENGE_SOURCE_MEASURE = "measure"
CHALLENGE_SOURCES = (CHALLENGE_SOURCE_DEFAULT, CHALLENGE_SOURCE_ARENA, CHALLENGE_SOURCE_MEASURE)
CHALLENGE_SOURCE_PATHS = {
    CHALLENGE_SOURCE_DEFAULT: {
        "list": "/chals",
        "detail": "/chals/{id}",
        "submit": "/chal/{id}",
        "solves": "/solves",
        "referer": "/challenges",
    },
    CHALLENGE_SOURCE_ARENA: {
        "list": "/arenas",
        "detail": "/arenas/{id}",
        "submit": "/are/{id}",
        "solves": "/arenasolves",
        "referer": "/arenas",
    },
    CHALLENGE_SOURCE_MEASURE: {
        "list": "/measures",
        "detail": "/measure/{id}",
        "submit": "/measure/submit",
        "solves": "/solves_measure",
        "referer": "/challenges",
    },
}


class ISCCError(Exception):
    def __init__(self, message, auth_error=False, transient=False):
        super().__init__(message)
        self.auth_error = auth_error
        self.transient = transient


class OperationCancelled(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_runtime_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def default_config():
    return {
        "secret_key": secrets.token_urlsafe(32),
        "local_auth": {
            "enabled": True,
            "username": None,
            "password_hash": None,
        },
        "server": {
            "host": "127.0.0.1",
            "port": 5000,
            "debug": False,
        },
        "iscc": {
            "base_url": ISCC_BASE_URL,
            "verify_tls": False,
            "timeout": 15,
            "operation_delay_seconds": OPERATION_DELAY_DEFAULT_SECONDS,
            "skip_file_categories": [],
            "submit_payload_mode": "data",
            "proxy": {
                "enabled": False,
                "list": [],
                "mode": "round_robin",
            },
        },
    }


def merge_defaults(data, defaults):
    merged = copy.deepcopy(defaults)
    if not isinstance(data, dict):
        return merged
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_defaults(value, merged[key])
        else:
            merged[key] = value
    return merged


def normalize_config(cfg):
    cfg.setdefault("iscc", {})
    cfg["iscc"].pop("proxy_pool", None)
    proxy = cfg["iscc"].setdefault("proxy", {"enabled": False, "list": [], "mode": "round_robin"})
    if not isinstance(proxy, dict):
        cfg["iscc"]["proxy"] = {"enabled": False, "list": [], "mode": "round_robin"}
        proxy = cfg["iscc"]["proxy"]
    proxy.setdefault("enabled", False)
    if proxy.get("mode") not in {"round_robin", "random"}:
        proxy["mode"] = "round_robin"
    legacy_delay = cfg["iscc"].pop("submit_delay_seconds", None)
    if "operation_delay_seconds" not in cfg["iscc"] and legacy_delay not in (None, ""):
        cfg["iscc"]["operation_delay_seconds"] = legacy_delay
    try:
        cfg["iscc"]["operation_delay_seconds"] = parse_delay_seconds(
            cfg["iscc"].get("operation_delay_seconds"),
            default=OPERATION_DELAY_DEFAULT_SECONDS,
            max_seconds=OPERATION_DELAY_MAX_SECONDS,
        )
    except ISCCError:
        cfg["iscc"]["operation_delay_seconds"] = float(OPERATION_DELAY_DEFAULT_SECONDS)
    cfg["iscc"].pop("submit_proxy_failover", None)
    proxy["list"] = parse_proxy_list(proxy.get("list", []))
    cfg.pop("safety", None)
    return cfg


def load_json(path, default):
    if not path.exists():
        return copy.deepcopy(default)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else copy.deepcopy(default)
    except Exception:
        return copy.deepcopy(default)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    try:
        os.replace(tmp_path, path)
    except PermissionError:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def init_config():
    cfg = normalize_config(merge_defaults(load_json(CONFIG_FILE, {}), default_config()))
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_urlsafe(32)
    save_json(CONFIG_FILE, cfg)
    return cfg


def load_config():
    cfg = normalize_config(merge_defaults(load_json(CONFIG_FILE, {}), default_config()))
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_urlsafe(32)
        save_json(CONFIG_FILE, cfg)
    return cfg


def save_config(cfg):
    cfg = normalize_config(merge_defaults(cfg, default_config()))
    save_json(CONFIG_FILE, cfg)
    return cfg


def default_accounts():
    return {"accounts": []}


def default_challenges():
    return {"updated_at": None, "items": {}}


def default_solves():
    return {"updated_at": None, "accounts": {}}


def default_files():
    return {"updated_at": None, "accounts": {}}


def default_flags():
    return {"updated_at": None, "items": []}


def load_accounts():
    data = load_json(ACCOUNTS_FILE, default_accounts())
    data.setdefault("accounts", [])
    return data


def save_accounts(data):
    data.setdefault("accounts", [])
    save_json(ACCOUNTS_FILE, data)


def load_challenges():
    data = load_json(CHALLENGES_FILE, default_challenges())
    data.setdefault("items", {})
    return data


def save_challenges(data):
    data.setdefault("items", {})
    save_json(CHALLENGES_FILE, data)


def load_solves():
    data = load_json(SOLVES_FILE, default_solves())
    data.setdefault("accounts", {})
    return data


def save_solves(data):
    data.setdefault("accounts", {})
    save_json(SOLVES_FILE, data)


def load_files():
    data = load_json(FILES_FILE, default_files())
    data.setdefault("accounts", {})
    return data


def save_files(data):
    data.setdefault("accounts", {})
    save_json(FILES_FILE, data)


def load_flags():
    data = load_json(FLAGS_FILE, default_flags())
    if not isinstance(data.get("items"), list):
        data["items"] = []
    return data


def save_flags(data):
    if not isinstance(data.get("items"), list):
        data["items"] = []
    save_json(FLAGS_FILE, data)


def is_setup_complete(cfg=None):
    cfg = cfg or load_config()
    auth = cfg.get("local_auth", {})
    return bool(auth.get("username") and auth.get("password_hash"))


PROCESS_SESSION_TOKEN = secrets.token_urlsafe(32)
PROCESS_RUN_ID = uuid.uuid4().hex
RUNTIME_LOGS = []
RUNTIME_LOG_LIMIT = 500
OPERATION_PROGRESS = {}
OPERATION_TASKS = {}
CHALLENGE_DETAIL_VISITS = set()
ACCOUNT_CHALLENGE_DETAILS = {}
PROGRESS_TTL_SECONDS = 3600
PROGRESS_DONE_TTL_SECONDS = 600
CLIENT_CACHE = {}
ACCOUNT_PROXY_BINDINGS = {}
CLIENT_CACHE_LOCK = threading.Lock()
PROXY_CONFIG_LOCK = threading.Lock()


def log_event(level, event, **fields):
    row = {"time": utc_now(), "level": level, "event": event}
    for key, value in fields.items():
        if value is not None:
            row[key] = value
    with CLIENT_CACHE_LOCK:
        RUNTIME_LOGS.append(row)
        if len(RUNTIME_LOGS) > RUNTIME_LOG_LIMIT:
            del RUNTIME_LOGS[: len(RUNTIME_LOGS) - RUNTIME_LOG_LIMIT]
    return row


def public_logs(limit=50, offset=0):
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    with CLIENT_CACHE_LOCK:
        logs = list(reversed(RUNTIME_LOGS))
        total = len(logs)
        return {"items": logs[offset: offset + limit], "total": total, "limit": limit, "offset": offset}


def progress_id_from_body(body):
    value = str((body or {}).get("progress_id") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value):
        return value
    return None


def cleanup_progress_locked():
    now = time.time()
    for progress_id, item in list(OPERATION_PROGRESS.items()):
        updated_at = item.get("updated_at_epoch", now)
        age = now - float(updated_at or now)
        if (item.get("done") and age > PROGRESS_DONE_TTL_SECONDS) or age > PROGRESS_TTL_SECONDS:
            OPERATION_PROGRESS.pop(progress_id, None)


def init_progress(progress_id, title, total):
    if not progress_id:
        return
    with CLIENT_CACHE_LOCK:
        cleanup_progress_locked()
        OPERATION_PROGRESS[progress_id] = {
            "title": title,
            "current": 0,
            "total": int(total or 0),
            "username": None,
            "message": "准备开始",
            "done": False,
            "ok": None,
            "events": [],
            "cancel_requested": False,
            "skip_requested": False,
            "paused": False,
            "control_message": None,
            "updated_at": utc_now(),
            "updated_at_epoch": time.time(),
        }


def update_progress(progress_id, current=None, total=None, username=None, message=None, done=None, ok=None):
    if not progress_id:
        return
    with CLIENT_CACHE_LOCK:
        item = OPERATION_PROGRESS.setdefault(progress_id, {"title": "处理中", "current": 0, "total": 0})
        if current is not None:
            item["current"] = int(current)
        if total is not None:
            item["total"] = int(total)
        if username is not None:
            item["username"] = username
        if message is not None:
            item["message"] = message
        if done is not None:
            item["done"] = bool(done)
        if ok is not None:
            item["ok"] = bool(ok)
        item["updated_at"] = utc_now()
        item["updated_at_epoch"] = time.time()


def add_progress_event(progress_id, username, ok, message, **fields):
    if not progress_id:
        return
    with CLIENT_CACHE_LOCK:
        item = OPERATION_PROGRESS.setdefault(progress_id, {"title": "处理中", "current": 0, "total": 0, "events": []})
        events = item.setdefault("events", [])
        event = {"id": len(events) + 1, "username": username, "ok": bool(ok), "message": str(message or "")}
        event.update({key: value for key, value in fields.items() if value is not None})
        events.append(event)
        item["updated_at"] = utc_now()
        item["updated_at_epoch"] = time.time()


def get_progress(progress_id):
    with CLIENT_CACHE_LOCK:
        cleanup_progress_locked()
        item = OPERATION_PROGRESS.get(progress_id)
        if not item:
            return None
        progress = dict(item)
        progress.pop("updated_at_epoch", None)
        return progress


def list_progress_jobs():
    with CLIENT_CACHE_LOCK:
        cleanup_progress_locked()
        jobs = []
        for progress_id, item in OPERATION_PROGRESS.items():
            progress = dict(item)
            progress["id"] = progress_id
            progress.pop("updated_at_epoch", None)
            jobs.append(progress)
        jobs.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return jobs


def start_background_operation(progress_id, title, total, target, *args, **kwargs):
    progress_id = progress_id or uuid.uuid4().hex
    init_progress(progress_id, title, total)

    def runner():
        try:
            result = target(progress_id, *args, **kwargs)
            with CLIENT_CACHE_LOCK:
                task = OPERATION_TASKS.setdefault(progress_id, {})
                task["result"] = result
                task["finished_at"] = utc_now()
            progress = get_progress(progress_id) or {}
            if not progress.get("done"):
                update_progress(progress_id, total, total, message="任务完成", done=True, ok=True)
        except OperationCancelled as exc:
            update_progress(progress_id, message=str(exc) or "已停止", done=True, ok=False)
        except Exception as exc:
            log_event("error", "background_task_failed", progress_id=progress_id, error=str(exc))
            update_progress(progress_id, message=f"任务失败：{exc}", done=True, ok=False)
        finally:
            with CLIENT_CACHE_LOCK:
                task = OPERATION_TASKS.setdefault(progress_id, {})
                task["done"] = True
                task["finished_at"] = task.get("finished_at") or utc_now()

    thread = threading.Thread(target=runner, name=f"operation-{progress_id}", daemon=True)
    with CLIENT_CACHE_LOCK:
        OPERATION_TASKS[progress_id] = {"title": title, "started_at": utc_now(), "done": False}
    thread.start()
    return progress_id


def set_progress_paused(progress_id, paused):
    if not progress_id:
        return None
    with CLIENT_CACHE_LOCK:
        item = OPERATION_PROGRESS.get(progress_id)
        if not item or item.get("done"):
            return item
        item["paused"] = bool(paused)
        item["control_message"] = "已暂停" if paused else "已继续"
        item["message"] = "已暂停，等待继续" if paused else "已继续，正在恢复"
        item["updated_at"] = utc_now()
        item["updated_at_epoch"] = time.time()
    log_event("warn" if paused else "info", "progress_paused" if paused else "progress_resumed", progress_id=progress_id)
    return item


def request_progress_cancel(progress_id):
    if not progress_id:
        return None
    with CLIENT_CACHE_LOCK:
        item = OPERATION_PROGRESS.get(progress_id)
        if not item or item.get("done"):
            return item
        item["cancel_requested"] = True
        item["paused"] = False
        item["done"] = True
        item["ok"] = False
        item["control_message"] = "已停止"
        item["message"] = "已停止，当前网络请求返回后会结束后台线程"
        item["updated_at"] = utc_now()
        item["updated_at_epoch"] = time.time()
    log_event("warn", "progress_cancel_requested", progress_id=progress_id)
    return item


def request_progress_skip(progress_id):
    if not progress_id:
        return None
    with CLIENT_CACHE_LOCK:
        item = OPERATION_PROGRESS.get(progress_id)
        if not item or item.get("done"):
            return item
        item["skip_requested"] = True
        item["paused"] = False
        item["control_message"] = "已请求跳过"
        item["message"] = "已请求跳过当前账号/题目"
        item["updated_at"] = utc_now()
        item["updated_at_epoch"] = time.time()
    log_event("warn", "progress_skip_requested", progress_id=progress_id)
    return item


def consume_progress_skip(progress_id):
    if not progress_id:
        return False
    with CLIENT_CACHE_LOCK:
        item = OPERATION_PROGRESS.get(progress_id)
        if not item or not item.get("skip_requested"):
            return False
        item["skip_requested"] = False
        item["control_message"] = None
        item["message"] = "已跳过当前步骤"
        item["updated_at"] = utc_now()
        item["updated_at_epoch"] = time.time()
        return True


def sleep_with_progress_control(progress_id, seconds):
    end_at = time.time() + max(0, float(seconds or 0))
    while time.time() < end_at:
        check_progress_control(progress_id)
        time.sleep(min(0.5, max(0, end_at - time.time())))


def check_progress_control(progress_id):
    if not progress_id:
        return
    while True:
        with CLIENT_CACHE_LOCK:
            item = OPERATION_PROGRESS.get(progress_id)
            if not item:
                return
            if item.get("cancel_requested"):
                item["done"] = True
                item["ok"] = False
                item["paused"] = False
                item["message"] = "已停止"
                item["updated_at"] = utc_now()
                item["updated_at_epoch"] = time.time()
                raise OperationCancelled("已停止")
            paused = bool(item.get("paused"))
            if paused:
                item["message"] = "已暂停，等待继续"
                item["updated_at"] = utc_now()
                item["updated_at_epoch"] = time.time()
        if not paused:
            return
        time.sleep(0.5)


def flag_md5(flag):
    return hashlib.md5(str(flag or "").encode("utf-8")).hexdigest()


def is_local_session_authenticated():
    return bool(session.get("logged_in") and session.get("session_token") == PROCESS_SESSION_TOKEN)


def mark_local_session(username):
    session.clear()
    session["logged_in"] = True
    session["username"] = username
    session["session_token"] = PROCESS_SESSION_TOKEN


def api_ok(data=None, status=200):
    return jsonify({"ok": True, "data": data if data is not None else {}}), status


def api_error(message, status=400, details=None):
    payload = {"ok": False, "error": str(message)}
    if details is not None:
        payload["details"] = details
    return jsonify(payload), status


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_setup_complete():
            return api_error("请先完成本站管理员初始化", 401)
        if not is_local_session_authenticated():
            return api_error("请先登录本站", 401)
        return fn(*args, **kwargs)

    return wrapper


def json_body():
    return request.get_json(silent=True) or {}


def parse_delay_seconds(value, default=0, max_seconds=60):
    if value in (None, ""):
        return float(default)
    try:
        delay = float(value)
    except (TypeError, ValueError) as exc:
        raise ISCCError("延时时长必须是数字") from exc
    if delay < 0:
        raise ISCCError("延时时长不能为负数")
    return min(delay, float(max_seconds))


def configured_operation_delay(cfg):
    return float(cfg.get("iscc", {}).get("operation_delay_seconds") or OPERATION_DELAY_DEFAULT_SECONDS)


def sleep_between_progress_items(progress_id, delay_seconds, index, total):
    if index < total and delay_seconds > 0:
        sleep_with_progress_control(progress_id, delay_seconds)


def sanitize_name(value, fallback="item"):
    value = unquote(str(value or "")).strip()
    value = Path(value).name
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    value = value.strip("._")
    return value[:120] or fallback


def normalize_category(value):
    return str(value or "").strip().upper()


def normalize_challenge_source(value):
    value = str(value or CHALLENGE_SOURCE_DEFAULT).strip().lower()
    if value in {"arena", "arenas", "擂台"}:
        return CHALLENGE_SOURCE_ARENA
    if value in {"measure", "measures", "实战"}:
        return CHALLENGE_SOURCE_MEASURE
    return CHALLENGE_SOURCE_DEFAULT


def challenge_endpoint(source, key):
    return CHALLENGE_SOURCE_PATHS[normalize_challenge_source(source)][key]


def split_challenge_ref(value, fallback_source=CHALLENGE_SOURCE_DEFAULT):
    raw = str(value or "").strip()
    if ":" in raw:
        prefix, remote_id = raw.split(":", 1)
        source = normalize_challenge_source(prefix)
        if source != CHALLENGE_SOURCE_DEFAULT or prefix.strip().lower() in {"challenge", "challenges"}:
            return source, remote_id
    return normalize_challenge_source(fallback_source), raw


def challenge_cache_key(source, chal_id):
    source, remote_id = split_challenge_ref(chal_id, fallback_source=source)
    return str(remote_id) if source == CHALLENGE_SOURCE_DEFAULT else f"{source}:{remote_id}"


def challenge_remote_id(challenge_or_id, fallback_source=CHALLENGE_SOURCE_DEFAULT):
    if isinstance(challenge_or_id, dict):
        return challenge_or_id.get("remote_id") or split_challenge_ref(challenge_or_id.get("id"), challenge_source(challenge_or_id))[1]
    return split_challenge_ref(challenge_or_id, fallback_source)[1]


def challenge_source(challenge):
    if isinstance(challenge, dict):
        source, _ = split_challenge_ref(challenge.get("id"), challenge.get("source") or CHALLENGE_SOURCE_DEFAULT)
        return normalize_challenge_source(challenge.get("source") or source)
    source, _ = split_challenge_ref(challenge)
    return source


def cached_challenge_source(chal_id, fallback=None):
    source, _ = split_challenge_ref(chal_id, fallback_source=(fallback or {}).get("source") or CHALLENGE_SOURCE_DEFAULT)
    if source != CHALLENGE_SOURCE_DEFAULT:
        return source
    if fallback and fallback.get("source"):
        return challenge_source(fallback)
    challenge = load_challenges().get("items", {}).get(str(chal_id), {})
    return challenge_source(challenge)


def challenge_source_label(source):
    return {
        CHALLENGE_SOURCE_DEFAULT: "练武",
        CHALLENGE_SOURCE_ARENA: "擂台",
        CHALLENGE_SOURCE_MEASURE: "实战",
    }.get(normalize_challenge_source(source), "练武")


def challenge_display_category(challenge):
    if challenge_source(challenge) == CHALLENGE_SOURCE_MEASURE:
        return "实战"
    if isinstance(challenge, dict):
        return challenge.get("category") or "未分类"
    return "未分类"


def challenge_sort_key(chal_id):
    source, remote_id = split_challenge_ref(chal_id)
    source_order = {source_name: index for index, source_name in enumerate(CHALLENGE_SOURCES)}.get(source, len(CHALLENGE_SOURCES))
    remote_text = str(remote_id or "")
    return (source_order, int(remote_text) if remote_text.isdigit() else 0, remote_text)


def looks_like_login_page(html):
    text = (html or "").lower()
    return "用户登录" in (html or "") or "/login" in text or "name=\"password\"" in text or "name='password'" in text


def challenge_display_name(challenge):
    return challenge.get("custom_name") or challenge.get("name") or f"#{challenge.get('id', '')}"


def apply_challenge_custom_name(challenge):
    if challenge.get("custom_name"):
        challenge = dict(challenge)
        challenge["name"] = challenge["custom_name"]
    return challenge


def account_public(account):
    login_ok = account.get("last_login_ok") if account.get("last_login_run_id") == PROCESS_RUN_ID else None
    return {
        "id": account.get("id"),
        "username": account.get("username", ""),
        "enabled": bool(account.get("enabled", True)),
        "password_set": bool(account.get("password")),
        "last_login_ok": login_ok,
        "last_login_at": account.get("last_login_at") if login_ok is not None else None,
        "last_error": account.get("last_error") if login_ok is False else None,
    }


def find_account(accounts_data, account_id):
    for account in accounts_data.get("accounts", []):
        if account.get("id") == account_id:
            return account
    return None


def get_accounts_by_ids(account_ids=None, enabled_only=True):
    accounts_data = load_accounts()
    accounts = accounts_data.get("accounts", [])
    if account_ids:
        wanted = set(account_ids)
        accounts = [account for account in accounts if account.get("id") in wanted]
    if enabled_only:
        accounts = [account for account in accounts if account.get("enabled", True)]
    return accounts_data, accounts


def mark_account_login(accounts_data, account_id, ok, error=None):
    account = find_account(accounts_data, account_id)
    if account:
        account["last_login_ok"] = bool(ok)
        account["last_login_at"] = utc_now()
        account["last_login_run_id"] = PROCESS_RUN_ID
        account["last_error"] = None if ok else str(error or "登录失败")


def delete_account_related_data(account_id):
    stats = {"removed_solves": False, "removed_files": False, "kept_flags": True}

    solves_data = load_solves()
    if solves_data.get("accounts", {}).pop(account_id, None) is not None:
        solves_data["updated_at"] = utc_now()
        save_solves(solves_data)
        stats["removed_solves"] = True

    files_data = load_files()
    if files_data.get("accounts", {}).pop(account_id, None) is not None:
        files_data["updated_at"] = utc_now()
        save_files(files_data)
        stats["removed_files"] = True

    return stats


def safe_config(cfg):
    return {
        "local_auth": {
            "enabled": bool(cfg.get("local_auth", {}).get("enabled", True)),
            "username": cfg.get("local_auth", {}).get("username"),
            "configured": is_setup_complete(cfg),
        },
        "server": cfg.get("server", {}),
        "iscc": cfg.get("iscc", {}),
    }


def normalize_proxy(proxy_raw):
    proxy_raw = str(proxy_raw or "").strip()
    if not proxy_raw:
        return None
    if proxy_raw.startswith(("http://", "https://")):
        return proxy_raw
    return f"http://{proxy_raw}"


def redact_proxy_url(proxy_url):
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.netloc:
        return proxy_url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    if parsed.username or parsed.password:
        auth = "***:***@" if parsed.password else "***@"
        return f"{parsed.scheme}://{auth}{host}{port}"
    return f"{parsed.scheme}://{host}{port}"


def proxy_log_label(proxies):
    if not proxies:
        return None
    if proxies.get("mode") == "random":
        current = proxies.get("current") or proxies.get("next")
        if current:
            return redact_proxy_url(current)
        return f"随机代理池（{len(proxies.get('pool') or [])} 个）"
    return redact_proxy_url(proxies.get("https") or proxies.get("http"))


def unique_proxy_list(raw_items):
    proxies = []
    seen = set()
    for proxy in (normalize_proxy(item) for item in raw_items):
        if proxy and proxy not in seen:
            proxies.append(proxy)
            seen.add(proxy)
    return proxies


def normalized_proxy_list(cfg):
    proxy_cfg = cfg.get("iscc", {}).get("proxy", {})
    if not proxy_cfg.get("enabled"):
        return []
    return unique_proxy_list(proxy_cfg.get("list", []))


def parse_proxy_list(raw_items):
    if isinstance(raw_items, str):
        raw_items = raw_items.split()
    if not isinstance(raw_items, list):
        return []
    return unique_proxy_list(raw_items)


def account_proxy_index(account, accounts_data):
    account_id = account.get("id")
    for index, item in enumerate(accounts_data.get("accounts", [])):
        if item.get("id") == account_id:
            return index
    return 0


def proxy_dict(proxy_url):
    proxy_url = normalize_proxy(proxy_url)
    return {"http": proxy_url, "https": proxy_url} if proxy_url else None


def proxy_url_from_proxies(proxies):
    if not proxies:
        return None
    if proxies.get("mode") == "random":
        return proxies.get("current") or proxies.get("next")
    return proxies.get("https") or proxies.get("http")


def get_bound_proxy_url(cfg, account=None, accounts_data=None):
    proxy_list = normalized_proxy_list(cfg)
    if not proxy_list:
        return None
    account_id = (account or {}).get("id")
    if account_id and ACCOUNT_PROXY_BINDINGS.get(account_id) in proxy_list:
        return ACCOUNT_PROXY_BINDINGS[account_id]
    if not account_id:
        index = account_proxy_index(account or {}, accounts_data or {"accounts": []})
        return proxy_list[index % len(proxy_list)]
    used_by_others = {
        normalize_proxy(proxy)
        for bound_account_id, proxy in ACCOUNT_PROXY_BINDINGS.items()
        if bound_account_id != account_id and normalize_proxy(proxy) in proxy_list
    }
    unused_choices = [proxy for proxy in proxy_list if proxy not in used_by_others]
    if unused_choices:
        index = account_proxy_index(account or {}, accounts_data or {"accounts": []})
        proxy_url = unused_choices[index % len(unused_choices)]
    else:
        proxy_url = random.choice(proxy_list)
    ACCOUNT_PROXY_BINDINGS[account_id] = proxy_url
    return proxy_url


def get_configured_proxy(cfg, account=None, accounts_data=None):
    proxy_cfg = cfg.get("iscc", {}).get("proxy", {})
    proxy_list = normalized_proxy_list(cfg)
    if not proxy_list:
        return None
    if proxy_cfg.get("mode") == "random":
        return {"mode": "random", "pool": proxy_list}
    proxy_url = get_bound_proxy_url(cfg, account, accounts_data)
    return proxy_dict(proxy_url)


def sync_cached_clients_after_config_saved(cfg):
    accounts_data = load_accounts()
    proxy_cfg = cfg.get("iscc", {}).get("proxy", {})
    if proxy_cfg.get("enabled") and proxy_cfg.get("mode") != "random":
        ACCOUNT_PROXY_BINDINGS.clear()
        for account in accounts_data.get("accounts", []):
            get_bound_proxy_url(cfg, account, accounts_data)
    for entry in CLIENT_CACHE.values():
        client = entry.get("client")
        if not client:
            continue
        client.cfg = cfg
        client.iscc_cfg = cfg.get("iscc", {})
        client.verify_tls = bool(client.iscc_cfg.get("verify_tls", False))
        client.timeout = int(client.iscc_cfg.get("timeout") or 15)
        client.proxies = get_configured_proxy(cfg, client.account, accounts_data)
        client.proxy_label = proxy_log_label(client.proxies)


def sync_cached_clients_after_proxy_removed(cfg, proxy_url):
    for key, entry in list(CLIENT_CACHE.items()):
        client = entry.get("client")
        if not client or not client.proxies:
            continue
        client.cfg = cfg
        client.iscc_cfg = cfg.get("iscc", {})
        if client.proxies.get("mode") == "random":
            client.proxies["pool"] = [proxy for proxy in client.proxies.get("pool", []) if normalize_proxy(proxy) != proxy_url]
            if normalize_proxy(client.proxies.get("current")) == proxy_url:
                client.proxies.pop("current", None)
            if normalize_proxy(client.proxies.get("next")) == proxy_url:
                client.proxies.pop("next", None)
            client.proxy_label = proxy_log_label(client.proxies)
            continue
        if normalize_proxy(proxy_url_from_proxies(client.proxies)) == proxy_url:
            CLIENT_CACHE.pop(key, None)


def remove_failed_proxy(proxy_url):
    proxy_url = normalize_proxy(proxy_url)
    if not proxy_url:
        return False
    with PROXY_CONFIG_LOCK:
        cfg = load_config()
        proxy_cfg = cfg.get("iscc", {}).get("proxy", {})
        proxy_list = unique_proxy_list(proxy_cfg.get("list", []))
        if proxy_url not in proxy_list:
            return False
        proxy_list = [proxy for proxy in proxy_list if proxy != proxy_url]
        proxy_cfg["list"] = proxy_list
        save_config(cfg)
        for account_id, bound_proxy in list(ACCOUNT_PROXY_BINDINGS.items()):
            if normalize_proxy(bound_proxy) == proxy_url:
                ACCOUNT_PROXY_BINDINGS.pop(account_id, None)
        with CLIENT_CACHE_LOCK:
            sync_cached_clients_after_proxy_removed(cfg, proxy_url)
        log_event("warn", "proxy_removed", proxy=redact_proxy_url(proxy_url), remaining=len(proxy_list), reason="proxy_connection_failed", message=f"已删除不可用代理：{redact_proxy_url(proxy_url)}")
        return True


def rebind_account_proxy(account, cfg, accounts_data, failed_proxy_url=None):
    proxy_list = normalized_proxy_list(cfg)
    failed_proxy_url = normalize_proxy(failed_proxy_url)
    if failed_proxy_url:
        proxy_list = [proxy for proxy in proxy_list if proxy != failed_proxy_url]
    if cfg.get("iscc", {}).get("proxy", {}).get("mode") == "random":
        choices = proxy_list
        if not choices:
            return None
        next_proxy = random.choice(choices)
        log_event("warn", "proxy_random_switched", account_id=account.get("id"), username=account.get("username"), old_proxy=redact_proxy_url(failed_proxy_url), proxy=redact_proxy_url(next_proxy), reason="proxy_connection_failed")
        return next_proxy
    if not proxy_list or not account.get("id"):
        return None
    account_id = account.get("id")
    current = failed_proxy_url or ACCOUNT_PROXY_BINDINGS.get(account_id) or get_bound_proxy_url(cfg, account, accounts_data)
    used_by_others = {
        normalize_proxy(proxy)
        for bound_account_id, proxy in ACCOUNT_PROXY_BINDINGS.items()
        if bound_account_id != account_id
    }
    unused_choices = [proxy for proxy in proxy_list if proxy != current and proxy not in used_by_others]
    if unused_choices:
        candidate = unused_choices[0]
        source = "unused"
    else:
        choices = [proxy for proxy in proxy_list if proxy != current]
        if not choices:
            return None
        candidate = random.choice(choices)
        source = "random_fallback"
    ACCOUNT_PROXY_BINDINGS[account_id] = candidate
    log_event("warn", "proxy_rebound", account_id=account_id, username=account.get("username"), old_proxy=redact_proxy_url(current), proxy=redact_proxy_url(candidate), reason="proxy_connection_failed", source=source)
    return candidate


def is_submit_gateway_error(exc):
    text = str(exc or "")
    return getattr(exc, "transient", False) and any(f"HTTP {code}" in text for code in (502, 503, 504))


class ISCCClient:
    def __init__(self, username, password, cfg, proxies=None, account=None, accounts_data=None):
        self.username = username
        self.password = password
        self.account = account or {}
        self.accounts_data = accounts_data or {"accounts": []}
        self.cfg = cfg
        self.iscc_cfg = cfg.get("iscc", {})
        self.base_url = self.iscc_cfg.get("base_url") or ISCC_BASE_URL
        self.base_url = self.base_url.rstrip("/")
        self.verify_tls = bool(self.iscc_cfg.get("verify_tls", False))
        self.timeout = int(self.iscc_cfg.get("timeout") or 15)
        self.nonce = None
        self.logged_in = False
        self.lock = threading.RLock()
        self.session = requests.Session()
        self.proxies = proxies
        self.proxy_label = proxy_log_label(self.proxies)
        self.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "User-Agent": "Mozilla/5.0 (compatible; ISCC-Flask-Manager/1.0)",
        }
        if not self.verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def url(self, path_or_url):
        if str(path_or_url).startswith(("http://", "https://")):
            return str(path_or_url)
        return urljoin(f"{self.base_url}/", str(path_or_url).lstrip("/"))

    def request(self, method, path_or_url, **kwargs):
        headers = dict(self.headers)
        headers.update(kwargs.pop("headers", {}) or {})
        kwargs.setdefault("headers", headers)
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify_tls)
        request_proxies = self.proxies
        proxy_label = self.proxy_label
        if request_proxies and request_proxies.get("mode") == "random":
            proxy_url = request_proxies.pop("next", None) or random.choice(request_proxies.get("pool") or [])
            request_proxies["current"] = proxy_url
            kwargs.setdefault("proxies", proxy_dict(proxy_url))
            proxy_label = proxy_log_label(request_proxies)
            self.proxy_label = proxy_label
        elif request_proxies:
            kwargs.setdefault("proxies", request_proxies)
        try:
            response = self.session.request(method, self.url(path_or_url), **kwargs)
        except requests.exceptions.Timeout as exc:
            proxy = f"，代理：{proxy_label}" if proxy_label else ""
            if self.switch_proxy_after_failure(proxy_label):
                kwargs.pop("proxies", None)
                return self.request(method, path_or_url, **kwargs)
            raise ISCCError(f"请求超时：{path_or_url}{proxy}", transient=True) from exc
        except requests.exceptions.ProxyError as exc:
            proxy = f"，代理：{proxy_label}" if proxy_label else ""
            if self.switch_proxy_after_failure(proxy_label):
                kwargs.pop("proxies", None)
                return self.request(method, path_or_url, **kwargs)
            raise ISCCError(f"代理请求失败：{path_or_url}{proxy}", transient=True) from exc
        except requests.exceptions.ConnectionError as exc:
            proxy = f"，代理：{proxy_label}" if proxy_label else ""
            if self.switch_proxy_after_failure(proxy_label):
                kwargs.pop("proxies", None)
                return self.request(method, path_or_url, **kwargs)
            raise ISCCError(f"网络连接失败：{path_or_url}{proxy}", transient=True) from exc
        except requests.exceptions.RequestException as exc:
            proxy = f"，代理：{proxy_label}" if proxy_label else ""
            raise ISCCError(f"请求异常：{path_or_url}{proxy}：{exc}", transient=True) from exc

        if response.status_code >= 400:
            snippet = re.sub(r"\s+", " ", (response.text or "")[:300]).strip()
            response_headers = []
            for header_name in ("Server", "Via", "X-Cache", "X-Request-Id", "CF-Ray"):
                header_value = response.headers.get(header_name)
                if header_value:
                    response_headers.append(f"{header_name}={header_value}")
            proxy = f"，代理：{proxy_label}" if proxy_label else ""
            header_detail = f"，响应头：{'; '.join(response_headers)}" if response_headers else ""
            detail = f"，响应片段：{snippet}" if snippet else ""
            raise ISCCError(f"ISCC 返回 HTTP {response.status_code}：{path_or_url}{proxy}{header_detail}{detail}", auth_error=response.status_code in {401, 403}, transient=response.status_code >= 500)
        return response

    def switch_proxy_after_failure(self, failed_proxy_label=None):
        failed_proxy = proxy_url_from_proxies(self.proxies)
        remove_failed_proxy(failed_proxy)
        self.cfg = load_config()
        next_proxy = rebind_account_proxy(self.account, self.cfg, self.accounts_data, failed_proxy_url=failed_proxy)
        if not next_proxy:
            return False
        if self.proxies and self.proxies.get("mode") == "random":
            self.proxies["pool"] = [proxy for proxy in normalized_proxy_list(self.cfg) if normalize_proxy(proxy) != normalize_proxy(failed_proxy)]
            self.proxies["next"] = next_proxy
            self.proxy_label = proxy_log_label(self.proxies)
        else:
            self.proxies = proxy_dict(next_proxy)
            self.proxy_label = proxy_log_label(self.proxies)
        log_event("warn", "client_proxy_switched", account_id=self.account.get("id"), username=self.username, old_proxy=failed_proxy_label, proxy=self.proxy_label)
        return True

    def parse_json(self, response, label):
        try:
            return response.json()
        except ValueError as exc:
            snippet = (response.text or "")[:200]
            raise ISCCError(f"解析 {label} JSON 失败，响应片段：{snippet}") from exc

    def login(self, force=False):
        if self.logged_in and self.nonce and not force:
            return True
        with self.lock:
            if self.logged_in and self.nonce and not force:
                return True
            if force:
                self.reset_login_state(clear_session=True)
            login_response = self.request("POST", "/login", data={"name": self.username, "password": self.password})
            if looks_like_login_page(login_response.text):
                self.reset_login_state(clear_session=True)
                raise ISCCError("登录失败：ISCC 仍返回登录页，请检查账号密码或比赛登录状态", auth_error=True)
            self.nonce = self.get_nonce()
            lists = [self.list_challenges_once(source) for source in CHALLENGE_SOURCES]
            if not all(isinstance(items, list) for items in lists):
                self.logged_in = False
                raise ISCCError("登录校验失败：无法读取题目列表")
            self.logged_in = True
            return True

    def get_nonce(self, source=CHALLENGE_SOURCE_DEFAULT):
        source = normalize_challenge_source(source)
        page_path = challenge_endpoint(source, "referer")
        with self.lock:
            response = self.request("GET", page_path)
            html = response.text or ""
            if not html.strip():
                raise ISCCError(f"获取 nonce 失败：{page_path} 返回空内容", auth_error=True)
            if looks_like_login_page(html):
                raise ISCCError("登录失败：ISCC 仍返回登录页，请检查账号密码或比赛登录状态", auth_error=True)
            patterns = [
                r'<input\b(?=[^>]*\bname=["\']nonce["\'])(?=[^>]*\bvalue=["\']?([^"\'>\s]+))[^>]*>',
                r'<input\b(?=[^>]*\bid=["\']nonce["\'])(?=[^>]*\bvalue=["\']?([^"\'>\s]+))[^>]*>',
            ]
            for pattern in patterns:
                match = re.search(pattern, html, flags=re.IGNORECASE)
                if match:
                    self.nonce = match.group(1)
                    return self.nonce
            raise ISCCError("获取 nonce 失败：页面未返回提交令牌", auth_error=True)

    def reset_login_state(self, clear_session=False):
        self.logged_in = False
        self.nonce = None
        if clear_session:
            self.session.cookies.clear()

    def submit_flag_once(self, chal_id, flag, source=CHALLENGE_SOURCE_DEFAULT):
        source = normalize_challenge_source(source)

        def payload_modes():
            preferred = self.iscc_cfg.get("submit_payload_mode") or "data"
            modes = []
            for mode in (preferred, "data", "params"):
                if mode in {"data", "params"} and mode not in modes:
                    modes.append(mode)
            return modes

        last_error = None
        last_failure = None
        raw = ""
        headers = {
            "Accept": "text/plain,application/json,*/*",
            "Referer": self.url(challenge_endpoint(source, "referer")),
            "X-Requested-With": "XMLHttpRequest",
        }
        remote_id = challenge_remote_id(chal_id, source)
        submit_path = challenge_endpoint(source, "submit").format(id=remote_id)
        for payload_mode in payload_modes():
            if not self.nonce:
                self.nonce = self.get_nonce(source)
            payload = {"key": flag, "nonce": self.nonce}
            if source == CHALLENGE_SOURCE_MEASURE:
                payload["id"] = remote_id
            try:
                response = self.request("POST", submit_path, headers=headers, **{payload_mode: payload})
                raw = response.text.strip()
            except ISCCError as exc:
                if exc.auth_error:
                    self.nonce = None
                raise
            if raw == "1":
                return {"ok": True, "raw": raw, "message": "提交成功", "payload_mode": payload_mode}
            if raw.startswith("{"):
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    status = str(data.get("status") or "").strip().lower()
                    message = str(data.get("message") or data.get("msg") or raw[:80])
                    ok = bool(data.get("ok")) or bool(data.get("success")) or status in {"success", "ok", "correct"} or str(data.get("code")) == "1"
                    return {
                        "ok": ok,
                        "raw": raw[:200],
                        "message": "提交成功" if ok and not message else message,
                        "payload_mode": payload_mode,
                        "no_retry": not ok,
                    }
            if "已经解答过" in raw:
                return {"ok": False, "raw": raw[:200], "message": "提交失败：该账号已经解答过这道题", "payload_mode": payload_mode, "no_retry": True}
            if raw == "0":
                return {
                    "ok": False,
                    "raw": raw,
                    "message": "提交失败：ISCC 返回 0，通常是 flag 错误、题目不接受、账号已解或提交参数不匹配",
                    "payload_mode": payload_mode,
                }
            if raw == "2":
                return {"ok": False, "raw": raw, "message": "提交失败：ISCC 返回 2，该账号已解过该题", "payload_mode": payload_mode}
            if raw == "-1":
                self.reset_login_state(clear_session=True)
                raise ISCCError("提交失败：ISCC 返回 -1，登录状态已失效", auth_error=True)
            if raw == "3":
                return {"ok": False, "raw": raw, "message": "提交失败：ISCC 返回 3，提交过快，请稍后再试", "payload_mode": payload_mode, "no_retry": True}
            if raw == "4":
                return {"ok": False, "raw": raw, "message": "提交失败：ISCC 返回 4，账号未缴费或无提交权限", "payload_mode": payload_mode, "no_retry": True}
            last_failure = {"ok": False, "raw": raw[:200], "message": f"提交返回了无法识别的内容：{raw[:40] or '空响应'}", "payload_mode": payload_mode}
        if last_failure:
            return last_failure
        raise ISCCError(last_error or "未知提交结果")

    def submit_flag(self, chal_id, flag, source=CHALLENGE_SOURCE_DEFAULT):
        source = normalize_challenge_source(source)
        log_chal_id = challenge_cache_key(source, chal_id)
        remote_chal_id = challenge_remote_id(chal_id, source)
        with self.lock:
            last_result = None
            last_error = None
            for attempt in range(1, ISCC_RETRY_ATTEMPTS + 1):
                try:
                    if not self.logged_in or not self.nonce:
                        self.login(force=True)
                    result = self.submit_flag_once(chal_id, flag, source=source)
                    if result.get("ok") or result.get("no_retry") or str(result.get("raw")) in {"0", "2", "3", "4"}:
                        result["attempts"] = attempt
                        return result
                    last_result = result
                    log_event("warn", "iscc_submit_retry", username=self.username, proxy=self.proxy_label, chal_id=log_chal_id, source=source, remote_chal_id=remote_chal_id, attempt=attempt, reason=result.get("message"), raw=result.get("raw"))
                except ISCCError as exc:
                    last_error = str(exc)
                    if exc.auth_error:
                        self.reset_login_state(clear_session=exc.auth_error)
                    if is_submit_gateway_error(exc):
                        try:
                            self.get_challenge_detail_once(chal_id, source=source)
                        except ISCCError as detail_exc:
                            log_event("warn", "submit_gateway_chal_detail_refresh_failed", username=self.username, proxy=self.proxy_label, chal_id=log_chal_id, source=source, remote_chal_id=remote_chal_id, error=str(detail_exc))
                    log_event("warn" if attempt < ISCC_RETRY_ATTEMPTS else "error", "iscc_submit_retry_failed", username=self.username, proxy=self.proxy_label, chal_id=log_chal_id, source=source, remote_chal_id=remote_chal_id, attempt=attempt, auth_error=exc.auth_error, transient=exc.transient, error=str(exc))
                    if attempt >= ISCC_RETRY_ATTEMPTS:
                        break
                if attempt < ISCC_RETRY_ATTEMPTS:
                    time.sleep(0.35 * attempt)
            if last_result:
                last_result["attempts"] = ISCC_RETRY_ATTEMPTS
                return last_result
            raise ISCCError(last_error or "提交失败")

    def retry_with_login(self, action, label):
        last_error = None
        for attempt in range(1, ISCC_RETRY_ATTEMPTS + 1):
            try:
                if not self.logged_in or not self.nonce:
                    self.login(force=True)
                return action()
            except ISCCError as exc:
                last_error = exc
                if exc.auth_error:
                    self.reset_login_state(clear_session=exc.auth_error)
                log_event("warn" if attempt < ISCC_RETRY_ATTEMPTS else "error", "iscc_request_retry_failed", username=self.username, action=label, attempt=attempt, auth_error=exc.auth_error, transient=exc.transient, error=str(exc))
                if attempt < ISCC_RETRY_ATTEMPTS:
                    time.sleep(0.35 * attempt)
        raise last_error

    def list_challenges_once(self, source=CHALLENGE_SOURCE_DEFAULT):
        source = normalize_challenge_source(source)
        response = self.request("GET", challenge_endpoint(source, "list"), headers={"Accept": "application/json,*/*"})
        data = self.parse_json(response, "题目列表")
        if not isinstance(data, dict):
            raise ISCCError("题目列表格式异常")
        if "game" not in data:
            raise ISCCError(f"题目列表缺少 game 字段，返回字段：{', '.join(data.keys())}")
        items = data.get("game", [])
        for item in items:
            if isinstance(item, dict):
                item.setdefault("source", source)
        return items

    def list_challenges(self, source=CHALLENGE_SOURCE_DEFAULT):
        source = normalize_challenge_source(source)
        with self.lock:
            return self.retry_with_login(lambda: self.list_challenges_once(source), f"list_challenges:{source}")

    def list_all_challenges(self):
        items = []
        for source in CHALLENGE_SOURCES:
            items.extend(self.list_challenges(source))
        return items

    def get_challenge_detail_once(self, chal_id, source=CHALLENGE_SOURCE_DEFAULT):
        source = normalize_challenge_source(source)
        remote_id = challenge_remote_id(chal_id, source)
        detail_path = challenge_endpoint(source, "detail").format(id=remote_id)
        response = self.request("GET", detail_path, headers={"Accept": "application/json,*/*"})
        data = self.parse_json(response, "题目详情")
        if not isinstance(data, dict):
            raise ISCCError(f"题目 {chal_id} 详情格式异常")
        data.setdefault("source", source)
        return data

    def get_challenge_detail(self, chal_id, source=CHALLENGE_SOURCE_DEFAULT):
        source = normalize_challenge_source(source)
        with self.lock:
            return self.retry_with_login(lambda: self.get_challenge_detail_once(chal_id, source), f"get_challenge_detail:{source}:{chal_id}")

    def list_solves_once(self, source=CHALLENGE_SOURCE_DEFAULT):
        source = normalize_challenge_source(source)
        response = self.request("GET", challenge_endpoint(source, "solves"), headers={"Accept": "application/json,*/*"})
        data = self.parse_json(response, "solves")
        if not isinstance(data, dict):
            raise ISCCError("solves 格式异常")
        items = data.get("solves", [])
        for item in items:
            if isinstance(item, dict):
                item.setdefault("source", source)
        return items

    def list_solves(self, source=CHALLENGE_SOURCE_DEFAULT):
        source = normalize_challenge_source(source)
        with self.lock:
            return self.retry_with_login(lambda: self.list_solves_once(source), f"list_solves:{source}")

    def list_all_solves(self):
        items = []
        for source in CHALLENGE_SOURCES:
            items.extend(self.list_solves(source))
        return items

    def download_challenge_files(self, account_id, challenge_info, reusable_by_source=None):
        with self.lock:
            source = challenge_source(challenge_info)
            display_category = challenge_display_category(challenge_info)
            category = normalize_category(display_category)
            skip_categories = {normalize_category(item) for item in self.iscc_cfg.get("skip_file_categories", [])}
            if category in skip_categories:
                return []

            files = challenge_info.get("files") or []
            if not files:
                return []

            chal_id = challenge_cache_key(source, challenge_info.get("id"))
            chal_name = challenge_info.get("name") or f"challenge-{chal_id}"
            results = []

            for source_path in files:
                source_url = urljoin(f"{self.base_url}/", str(source_path).lstrip("/"))
                original_name = sanitize_name(Path(unquote(urlparse(source_url).path)).name, f"attachment-{chal_id}")
                source_stem = sanitize_name(Path(original_name).stem, f"attachment-{chal_id}")
                challenge_dir = DOWNLOAD_DIR / source_stem
                reused = reusable_file_by_source(source_url, reusable_by_source=reusable_by_source)
                if reused:
                    file_md5 = str(reused.get("md5") or "")
                    stored_name = reused.get("stored_name") or Path(reused.get("path") or "").name
                    final_path = Path(reused.get("path") or "")
                    size = int(reused.get("size") or final_path.stat().st_size)
                    results.append(
                        download_file_record(
                            account_id,
                            self.username,
                            chal_id,
                            chal_name,
                            display_category,
                            source,
                            source_url,
                            original_name,
                            stored_name,
                            final_path,
                            file_md5,
                            size,
                            reused=True,
                        )
                    )
                    continue
                challenge_dir.mkdir(parents=True, exist_ok=True)
                tmp_path = challenge_dir / f".{uuid.uuid4().hex}.{original_name}.tmp"
                md5_hash = hashlib.md5()
                size = 0
                try:
                    response = self.retry_with_login(lambda source_url=source_url: self.request("GET", source_url, stream=True), f"download_file:{chal_id}")
                    last_chunk_at = time.monotonic()
                    with tmp_path.open("wb") as f:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if time.monotonic() - last_chunk_at > max(self.timeout * 2, 30):
                                raise ISCCError(f"下载附件超时：{source_url}", transient=True)
                            if chunk:
                                last_chunk_at = time.monotonic()
                                f.write(chunk)
                                md5_hash.update(chunk)
                                size += len(chunk)
                    file_md5 = md5_hash.hexdigest()
                    stored_name = sanitize_name(f"{chal_id}_{file_md5}_{original_name}", f"{chal_id}_{file_md5}")
                    final_path = challenge_dir / stored_name
                    if final_path.exists():
                        tmp_path.unlink(missing_ok=True)
                    else:
                        tmp_path.replace(final_path)
                    results.append(
                        download_file_record(
                            account_id,
                            self.username,
                            chal_id,
                            chal_name,
                            display_category,
                            source,
                            source_url,
                            original_name,
                            stored_name,
                            final_path,
                            file_md5,
                            size,
                        )
                    )
                finally:
                    if tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
            return results


def client_cache_key(account, cfg, proxies=None):
    iscc_cfg = cfg.get("iscc", {})
    proxy_cfg = iscc_cfg.get("proxy", {})
    raw = json.dumps(
        {
            "account_id": account.get("id"),
            "username": account.get("username"),
            "password": account.get("password"),
            "base_url": iscc_cfg.get("base_url") or ISCC_BASE_URL,
            "verify_tls": bool(iscc_cfg.get("verify_tls", False)),
            "timeout": int(iscc_cfg.get("timeout") or 15),
            "submit_payload_mode": iscc_cfg.get("submit_payload_mode") or "data",
            "skip_file_categories": [normalize_category(item) for item in iscc_cfg.get("skip_file_categories", [])],
            "proxy_enabled": bool(proxy_cfg.get("enabled")),
            "proxy_mode": proxy_cfg.get("mode") or "round_robin",
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clear_client_cache(account_id=None):
    with CLIENT_CACHE_LOCK:
        if account_id is None:
            CLIENT_CACHE.clear()
        else:
            for key, entry in list(CLIENT_CACHE.items()):
                if entry.get("account_id") == account_id:
                    CLIENT_CACHE.pop(key, None)


def make_client(account, cfg=None, reuse=True, accounts_data=None):
    cfg = cfg or load_config()
    accounts_data = accounts_data or load_accounts()
    if not account.get("password"):
        raise ISCCError(f"账号 {account.get('username')} 未设置密码")
    proxies = get_configured_proxy(cfg, account, accounts_data)
    if not reuse:
        return ISCCClient(account.get("username"), account.get("password"), cfg, proxies=proxies, account=account, accounts_data=accounts_data)
    key = client_cache_key(account, cfg, proxies)
    with CLIENT_CACHE_LOCK:
        entry = CLIENT_CACHE.get(key)
        if entry and entry.get("account_id") == account.get("id") and entry.get("username") == account.get("username"):
            return entry["client"]
        if entry:
            CLIENT_CACHE.pop(key, None)
        client = ISCCClient(account.get("username"), account.get("password"), cfg, proxies=proxies, account=account, accounts_data=accounts_data)
        CLIENT_CACHE[key] = {"client": client, "account_id": account.get("id"), "username": account.get("username")}
        return client


def ensure_client_logged_in(account, accounts_data, cfg, force=False):
    client = make_client(account, cfg, accounts_data=accounts_data)
    if client.logged_in and client.nonce and not force:
        mark_account_login(accounts_data, account["id"], True)
        log_event("info", "iscc_session_reused", account_id=account.get("id"), username=account.get("username"), proxy=client.proxy_label)
        return client
    last_error = None
    for attempt in range(1, ISCC_RETRY_ATTEMPTS + 1):
        try:
            client.login(force=force or attempt > 1)
            mark_account_login(accounts_data, account["id"], True)
            log_event("info", "iscc_login_ok", account_id=account.get("id"), username=account.get("username"), proxy=client.proxy_label, attempts=attempt)
            return client
        except ISCCError as exc:
            last_error = exc
            if exc.auth_error:
                client.reset_login_state(clear_session=True)
            log_event("warn" if attempt < ISCC_RETRY_ATTEMPTS else "error", "iscc_login_retry_failed", account_id=account.get("id"), username=account.get("username"), proxy=client.proxy_label, attempt=attempt, auth_error=exc.auth_error, transient=exc.transient, error=str(exc))
            if attempt < ISCC_RETRY_ATTEMPTS:
                time.sleep(0.35 * attempt)
    clear_client_cache(account.get("id"))
    mark_account_login(accounts_data, account["id"], False, last_error)
    log_event("error", "iscc_login_failed", account_id=account.get("id"), username=account.get("username"), proxy=client.proxy_label, error=str(last_error))
    raise last_error


def get_ready_client(account, accounts_data, cfg):
    return ensure_client_logged_in(account, accounts_data, cfg, force=False)


def refresh_account_after_login(account, accounts_data, cfg, progress_id=None, force=True, previsit=True):
    client = ensure_client_logged_in(account, accounts_data, cfg, force=force)
    check_progress_control(progress_id)
    if previsit:
        previsit_cached_challenge_details(client, account_id=account["id"], progress_id=progress_id, username=account.get("username"))
    solves_data = load_solves()
    set_account_solves(solves_data, account, client.list_all_solves())
    save_solves(solves_data)
    save_accounts(accounts_data)
    return client


def set_account_solves(solves_data, account, solves):
    solves_data.setdefault("accounts", {})[account["id"]] = {
        "username": account.get("username"),
        "updated_at": utc_now(),
        "solves": solves,
    }
    solves_data["updated_at"] = utc_now()


def challenge_solve_map(solves_data):
    solved_by_chal = {}
    for account_id, entry in solves_data.get("accounts", {}).items():
        for solve in entry.get("solves", []) or []:
            chal_id = str(solve.get("chalid"))
            if chal_id and chal_id != "None":
                key = challenge_cache_key(solve.get("source") or CHALLENGE_SOURCE_DEFAULT, chal_id)
                solved_by_chal.setdefault(key, set()).add(account_id)
    return solved_by_chal


def flatten_files(files_data, account_id=None, chal_id=None):
    accounts_data = load_accounts()
    account_names = {a.get("id"): a.get("username") for a in accounts_data.get("accounts", [])}
    challenges = load_challenges().get("items", {})
    rows = []
    for aid, chal_map in files_data.get("accounts", {}).items():
        if account_id and aid != account_id:
            continue
        for cid, files in chal_map.items():
            if chal_id and str(cid) != str(chal_id):
                continue
            chal = challenges.get(str(cid), {})
            for item in files or []:
                row = dict(item)
                row.setdefault("account_id", aid)
                row.setdefault("account_username", account_names.get(aid, item.get("account_username", aid)))
                row.setdefault("challenge_id", cid)
                row.setdefault("challenge_name", challenge_display_name(chal) or item.get("challenge_name") or cid)
                row.setdefault("category", challenge_display_category(chal) if chal else item.get("category"))
                row.setdefault("source", chal.get("source") or item.get("source") or CHALLENGE_SOURCE_DEFAULT)
                row.setdefault("source_label", challenge_source_label(row.get("source")))
                rows.append(row)
    rows.sort(key=lambda item: (item.get("account_username", ""), str(item.get("challenge_id", "")), item.get("original_name", "")))
    return rows


def attachment_md5_map(files_data):
    md5_by_key = {}
    for aid, chal_map in files_data.get("accounts", {}).items():
        for cid, files in chal_map.items():
            for item in files or []:
                if item.get("md5"):
                    md5_by_key.setdefault((aid, str(cid)), str(item.get("md5")).strip().lower())
                    break
    return md5_by_key


def download_file_record(account_id, account_username, chal_id, chal_name, category, source, source_url, original_name, stored_name, path, file_md5, size, reused=False):
    file_id = hashlib.sha256(
        f"{account_id}|{chal_id}|{source_url}|{file_md5}|{stored_name}".encode("utf-8")
    ).hexdigest()[:24]
    row = {
        "file_id": file_id,
        "account_id": account_id,
        "account_username": account_username,
        "challenge_id": chal_id,
        "challenge_name": chal_name,
        "category": category,
        "source": source,
        "source_url": source_url,
        "original_name": original_name,
        "stored_name": stored_name,
        "path": str(path),
        "md5": file_md5,
        "size": size,
        "updated_at": utc_now(),
    }
    if reused:
        row["reused"] = True
    return row


def reusable_file_index(files_data=None):
    rows = {}
    for item in flatten_files(files_data or load_files()):
        source_url = str(item.get("source_url") or "")
        if not source_url or not item.get("md5"):
            continue
        path = Path(item.get("path") or "")
        if path.exists() and path.is_file():
            rows.setdefault(source_url, item)
    return rows


def reusable_file_by_source(source_url, reusable_by_source=None):
    source_url = str(source_url or "")
    if not source_url:
        return None
    if reusable_by_source is None:
        reusable_by_source = reusable_file_index()
    item = reusable_by_source.get(source_url)
    if not item:
        return None
    path = Path(item.get("path") or "")
    return item if path.exists() and path.is_file() else None


def existing_account_file_by_source(files_data, account_id, chal_id, source_url):
    source_url = str(source_url or "")
    for item in files_data.get("accounts", {}).get(account_id, {}).get(str(chal_id), []) or []:
        if item.get("source_url") != source_url:
            continue
        path = Path(item.get("path") or "")
        if path.exists() and path.is_file():
            row = dict(item)
            row["skipped"] = True
            return row
    return None


def cleanup_empty_download_dirs():
    if not DOWNLOAD_DIR.exists():
        return
    for path in sorted(DOWNLOAD_DIR.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_file() and path.name.startswith(".") and path.name.endswith(".tmp"):
            path.unlink(missing_ok=True)
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


def flatten_flags(flags_data, account_id=None, chal_id=None):
    challenges = load_challenges().get("items", {})
    md5_by_key = attachment_md5_map(load_files())
    by_key = {}
    for item in flags_data.get("items", []) or []:
        if account_id and item.get("account_id") != account_id:
            continue
        if chal_id and str(item.get("chal_id")) != str(chal_id):
            continue
        cid = str(item.get("chal_id"))
        chal = challenges.get(cid, {})
        row = dict(item)
        row.pop("account_username", None)
        row.setdefault("flag_md5", flag_md5(row.get("flag")))
        row.setdefault("challenge_name", challenge_display_name(chal) or item.get("challenge_name") or cid)
        row.setdefault("category", challenge_display_category(chal) if chal else item.get("category"))
        row.setdefault("source", chal.get("source") or item.get("source") or CHALLENGE_SOURCE_DEFAULT)
        row.setdefault("source_label", challenge_source_label(row.get("source")))
        if not row.get("attachment_md5"):
            row["attachment_md5"] = md5_by_key.get((row.get("account_id"), str(row.get("chal_id"))), "")
        key = row.get("dedupe_key") or flag_record_key(row.get("chal_id") or 0, row.get("flag_md5"), row.get("attachment_md5"))
        previous = by_key.get(key)
        if not previous or (row.get("updated_at") or row.get("created_at") or "") > (previous.get("updated_at") or previous.get("created_at") or ""):
            by_key[key] = row
    rows = list(by_key.values())
    for row in rows:
        row.pop("flag_md5", None)
    rows.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return rows


def flag_attachment_md5(account_id, chal_id, md5=None):
    if md5:
        return str(md5).strip().lower()
    for item in flatten_files(load_files(), account_id=account_id, chal_id=chal_id):
        if item.get("md5"):
            return str(item.get("md5")).strip().lower()
    return ""


def challenge_cache_has(chal_id):
    return str(chal_id) in load_challenges().get("items", {})


def save_challenge_detail(chal_id, detail, fallback=None):
    challenges_data = load_challenges()
    items = dict(challenges_data.get("items", {}))
    source = normalize_challenge_source((detail or {}).get("source") or (fallback or {}).get("source") or cached_challenge_source(chal_id, fallback=fallback))
    remote_id = challenge_remote_id((detail or {}).get("id") or chal_id, source)
    cache_key = challenge_cache_key(source, remote_id)
    previous = items.get(cache_key, {})
    custom_name = previous.get("custom_name")
    merged = dict(fallback or previous)
    merged.update(detail or {})
    merged["id"] = cache_key
    merged["remote_id"] = int(remote_id) if str(remote_id).isdigit() else remote_id
    merged["source"] = source
    if source == CHALLENGE_SOURCE_MEASURE:
        merged["category"] = "实战"
    if custom_name:
        merged["custom_name"] = custom_name
        merged["name"] = custom_name
    merged.setdefault("name", f"#{cache_key}")
    merged.setdefault("files", [])
    items[cache_key] = merged
    save_challenges({"updated_at": utc_now(), "items": items})
    return merged


def account_challenge_detail_key(account_id, client, chal_id):
    return (str(account_id or client.account.get("id") or client.username), str(chal_id))


def cache_account_challenge_detail(account_id, client, chal_id, detail):
    ACCOUNT_CHALLENGE_DETAILS[account_challenge_detail_key(account_id, client, chal_id)] = copy.deepcopy(detail or {})


def cached_account_challenge_detail(account_id, client, chal_id):
    detail = ACCOUNT_CHALLENGE_DETAILS.get(account_challenge_detail_key(account_id, client, chal_id))
    return copy.deepcopy(detail) if detail else None


def refresh_challenge_detail_for_account(client, chal_id, fallback=None, account_id=None, source=None):
    source = normalize_challenge_source(source or cached_challenge_source(chal_id, fallback=fallback))
    detail = client.get_challenge_detail(chal_id, source=source)
    detail.setdefault("source", source)
    merged = save_challenge_detail(chal_id, detail, fallback=fallback)
    cache_account_challenge_detail(account_id, client, chal_id, merged)
    return merged


def challenge_visit_key(account_id, client, chal_id):
    return (str(account_id or client.account.get("id") or client.username), str(chal_id))


def mark_challenge_detail_visited(account_id, client, chal_id):
    CHALLENGE_DETAIL_VISITS.add(challenge_visit_key(account_id, client, chal_id))


def has_challenge_detail_visit(account_id, client, chal_id):
    return challenge_visit_key(account_id, client, chal_id) in CHALLENGE_DETAIL_VISITS


def challenge_visit_account_ids(chal_id, accounts):
    chal_id = str(chal_id)
    return sorted(
        account.get("id")
        for account in accounts
        if account.get("id") and (str(account.get("id")), chal_id) in CHALLENGE_DETAIL_VISITS
    )


def ensure_challenge_detail_before_submit(client, chal_id, account_id=None, force=False, source=None):
    visit_key = challenge_visit_key(account_id, client, chal_id)
    if not force and visit_key in CHALLENGE_DETAIL_VISITS:
        return None
    detail = refresh_challenge_detail_for_account(client, chal_id, account_id=account_id, source=source)
    mark_challenge_detail_visited(account_id, client, chal_id)
    return detail


def previsit_cached_challenge_details(client, account_id=None, challenge_ids=None, progress_id=None, username=None, delay_seconds=0):
    items = load_challenges().get("items", {})
    ids = [str(item) for item in (challenge_ids or items.keys()) if str(item).strip()]
    for index, chal_id in enumerate(ids, start=1):
        check_progress_control(progress_id)
        challenge = items.get(str(chal_id), {})
        source = cached_challenge_source(chal_id, fallback=challenge)
        if progress_id:
            update_progress(progress_id, index, len(ids), username=username, message=f"正在预访问题目详情：{index}/{len(ids)} [{challenge_source_label(source)}] {chal_id}")
        if consume_progress_skip(progress_id):
            add_progress_event(progress_id, username or client.username, True, f"已跳过题目详情预访问：{chal_id}", kind="skip", chal_id=chal_id)
            continue
        if challenge_visit_key(account_id, client, chal_id) in CHALLENGE_DETAIL_VISITS:
            continue
        try:
            refresh_challenge_detail_for_account(client, chal_id, fallback=items.get(str(chal_id), {}), account_id=account_id)
            mark_challenge_detail_visited(account_id, client, chal_id)
        except ISCCError as exc:
            log_event("warn", "challenge_detail_previsit_failed", account_id=account_id, username=username or client.username, chal_id=chal_id, proxy=client.proxy_label, error=str(exc))
        sleep_between_progress_items(progress_id, delay_seconds, index, len(ids))


def flag_record_key(chal_id, flag_hash, attachment_md5=""):
    attachment_md5 = str(attachment_md5 or "").strip().lower()
    if attachment_md5:
        return f"flag-md5:{flag_hash}:{attachment_md5}"
    return f"flag-chal:{flag_hash}:{chal_id}"


def submit_flag_for_account(account, client, chal_id, flag, flags_data, md5="", source="submit"):
    challenge_track = cached_challenge_source(chal_id)
    cache_key = challenge_cache_key(challenge_track, chal_id)
    remote_id = challenge_remote_id(chal_id, challenge_track)
    ensure_challenge_detail_before_submit(client, cache_key, account_id=account.get("id"), source=challenge_track)
    submit_result = client.submit_flag(remote_id, flag, source=challenge_track)
    row = {
        "account_id": account.get("id"),
        "account_username": account.get("username"),
        "proxy": client.proxy_label,
        "chal_id": cache_key,
        "remote_chal_id": int(remote_id) if str(remote_id).isdigit() else remote_id,
        "source": challenge_track,
    }
    row.update(submit_result)
    row["ok"] = bool(submit_result.get("ok"))
    log_event(
        "info" if row["ok"] else "warn",
        "flag_submit_result",
        account_id=account.get("id"),
        username=account.get("username"),
        proxy=client.proxy_label,
        chal_id=cache_key,
        ok=row["ok"],
        message=row.get("message"),
        raw=row.get("raw"),
        payload_mode=row.get("payload_mode"),
        attempts=row.get("attempts"),
        flag_md5=flag_md5(flag),
        attachment_md5=md5,
    )
    if row["ok"]:
        record_successful_flag(flags_data, account, cache_key, flag, md5=md5, source=challenge_track, message=row.get("message"))
        save_flags(flags_data)
    return row


def record_successful_flag(flags_data, account, chal_id, flag, md5=None, source="submit", message=""):
    flag = str(flag or "").strip()
    if not flag:
        return None
    source = normalize_challenge_source(source)
    chal_id = challenge_cache_key(source, chal_id)
    now = utc_now()
    account_id = account.get("id")
    flag_hash = flag_md5(flag)
    attachment_md5 = flag_attachment_md5(account_id, chal_id, md5)
    record_key = flag_record_key(chal_id, flag_hash, attachment_md5)
    item_id = hashlib.sha256(record_key.encode("utf-8")).hexdigest()[:24]
    row = None
    for item in flags_data.setdefault("items", []):
        existing_hash = item.get("flag_md5") or flag_md5(item.get("flag"))
        existing_md5 = str(item.get("attachment_md5") or "").strip().lower()
        existing_key = item.get("dedupe_key") or flag_record_key(item.get("chal_id") or chal_id, existing_hash, existing_md5)
        if existing_key == record_key:
            row = item
            break
    if row is None:
        row = {"id": item_id, "created_at": now}
        flags_data["items"].append(row)
    account_ids = set(row.get("account_ids") or [])
    if row.get("account_id"):
        account_ids.add(row.get("account_id"))
    if account_id:
        account_ids.add(account_id)
    row.update(
        {
            "id": row.get("id") or item_id,
            "dedupe_key": record_key,
            "account_id": account_id,
            "account_ids": sorted(account_ids),
            "chal_id": chal_id,
            "flag": flag,
            "flag_md5": flag_hash,
            "attachment_md5": attachment_md5,
            "source": source,
            "message": message or "提交成功",
            "updated_at": now,
        }
    )
    row.pop("account_username", None)
    flags_data["updated_at"] = now
    return row


def submitted_account_ids_for_challenge(chal_id, solves_data=None, flags_data=None):
    chal_id = challenge_cache_key(cached_challenge_source(chal_id), chal_id)
    solved = set(challenge_solve_map(solves_data or load_solves()).get(str(chal_id), set()))
    for item in (flags_data or load_flags()).get("items", []) or []:
        if str(item.get("chal_id")) != str(chal_id):
            continue
        for account_id in item.get("account_ids") or []:
            if account_id:
                solved.add(account_id)
        if item.get("account_id"):
            solved.add(item.get("account_id"))
    return solved


ensure_runtime_dirs()
_runtime_config = init_config()
app = Flask(__name__)
app.secret_key = _runtime_config["secret_key"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)



@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    cfg = load_config()
    setup_required = not is_setup_complete(cfg)
    return api_ok(
        {
            "setup_required": setup_required,
            "authenticated": is_local_session_authenticated() and not setup_required,
            "username": cfg.get("local_auth", {}).get("username"),
        }
    )


@app.route("/api/setup", methods=["POST"])
def api_setup():
    cfg = load_config()
    if is_setup_complete(cfg):
        return api_error("本站管理员已初始化", 400)
    body = json_body()
    username = str(body.get("username") or "admin").strip()
    password = str(body.get("password") or "")
    if not username:
        return api_error("用户名不能为空")
    if len(password) < 6:
        return api_error("本站密码至少 6 位")
    cfg["local_auth"]["username"] = username
    cfg["local_auth"]["password_hash"] = generate_password_hash(password)
    save_config(cfg)
    mark_local_session(username)
    return api_status()[0]


@app.route("/api/login", methods=["POST"])
def api_login():
    cfg = load_config()
    if not is_setup_complete(cfg):
        return api_error("请先完成本站管理员初始化", 401)
    body = json_body()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    auth = cfg.get("local_auth", {})
    if username != auth.get("username") or not check_password_hash(auth.get("password_hash") or "", password):
        return api_error("本站用户名或密码错误", 401)
    mark_local_session(username)
    return api_ok({"username": username})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return api_ok()


@app.route("/api/accounts")
@require_auth
def api_accounts():
    return api_ok([account_public(account) for account in load_accounts().get("accounts", [])])


def parse_accounts_import(text, accounts):
    parsed = []
    if text:
        normalized_text = str(text).replace("\r", "\n")
        for raw_line in normalized_text.splitlines():
            line = raw_line.strip().strip("﻿")
            if not line or line.startswith("#"):
                continue
            row = None
            if "," in line:
                try:
                    row = next(csv.reader(io.StringIO(line)))
                except Exception:
                    row = None
            if row and len(row) >= 2:
                username, password = row[0].strip(), row[1].strip()
            else:
                parts = re.split(r"\s+", line, maxsplit=1)
                if len(parts) < 2:
                    continue
                username, password = parts[0].strip(), parts[1].strip()
            password = password.strip().strip('"').strip("'")
            if username and password:
                parsed.append({"username": username, "password": password})
    for item in accounts or []:
        username = str(item.get("username") or "").strip()
        password = str(item.get("password") or "")
        if username and password:
            parsed.append({"username": username, "password": password})
    return parsed


@app.route("/api/accounts/import", methods=["POST"])
@require_auth
def api_accounts_import():
    body = json_body()
    progress_id = progress_id_from_body(body)
    accounts_data = load_accounts()
    parsed = parse_accounts_import(body.get("text"), body.get("accounts"))
    if not parsed:
        return api_error("没有解析到账号，请使用：账号 密码")

    existing = {account.get("username"): account for account in accounts_data.get("accounts", [])}
    imported_usernames = set()
    created = 0
    updated = 0
    total = max(len(parsed), 1)
    current = 0
    init_progress(progress_id, "导入并登录账号", total)
    for item in parsed:
        check_progress_control(progress_id)
        current += 1
        username = item["username"]
        password = item["password"]
        imported_usernames.add(username)
        update_progress(progress_id, current, total, username, f"正在导入/更新：{username}")
        if username in existing:
            existing[username]["password"] = password
            existing[username]["enabled"] = True
            existing[username]["updated_at"] = utc_now()
            clear_client_cache(existing[username].get("id"))
            updated += 1
            add_progress_event(progress_id, username, True, "账号已更新")
            log_event("info", "account_import_updated", account_id=existing[username].get("id"), username=username, message="账号已更新")
        else:
            account = {
                "id": uuid.uuid4().hex,
                "username": username,
                "password": password,
                "enabled": True,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "last_login_ok": None,
                "last_login_at": None,
                "last_error": None,
            }
            accounts_data["accounts"].append(account)
            existing[username] = account
            created += 1
            add_progress_event(progress_id, username, True, "账号已导入")
            log_event("info", "account_import_created", account_id=account.get("id"), username=username, message="账号已导入")
        save_accounts(accounts_data)
    cfg = load_config()
    login_results = []
    imported_accounts = [account for account in accounts_data.get("accounts", []) if account.get("username") in imported_usernames]
    login_total = max(len(imported_accounts), 1)
    for login_index, account in enumerate(imported_accounts, start=1):
        check_progress_control(progress_id)
        if consume_progress_skip(progress_id):
            login_results.append({"account_id": account["id"], "username": account.get("username"), "ok": False, "skipped": True})
            add_progress_event(progress_id, account.get("username"), True, "已跳过登录")
            continue
        update_progress(progress_id, login_index, login_total, account.get("username"), f"正在登录：{account.get('username')}")
        try:
            refresh_account_after_login(account, accounts_data, cfg, progress_id=progress_id, force=True)
            login_results.append({"account_id": account["id"], "username": account.get("username"), "ok": True})
            add_progress_event(progress_id, account.get("username"), True, "登录成功，已更新解题状态和题目访问")
        except ISCCError as exc:
            login_results.append({"account_id": account["id"], "username": account.get("username"), "ok": False, "error": str(exc)})
            add_progress_event(progress_id, account.get("username"), False, f"登录失败：{exc}")
        save_accounts(accounts_data)
    save_accounts(accounts_data)
    ok_count = sum(1 for item in login_results if item.get("ok"))
    sync_result = None
    if ok_count:
        sync_accounts = [item["account_id"] for item in login_results if item.get("ok")]
        update_progress(progress_id, login_total, login_total, message="登录完成，正在自动同步全部内容")
        sync_result = sync_run_operation(progress_id, {"account_ids": sync_accounts})
        latest_challenge_ids = list(load_challenges().get("items", {}).keys())
        for account in imported_accounts:
            if account.get("id") not in sync_accounts:
                continue
            check_progress_control(progress_id)
            update_progress(progress_id, login_total, login_total, account.get("username"), f"正在预访问题目详情：{account.get('username')}")
            try:
                client = get_ready_client(account, accounts_data, cfg)
                previsit_cached_challenge_details(client, account_id=account["id"], challenge_ids=latest_challenge_ids, progress_id=progress_id, username=account.get("username"))
            except ISCCError as exc:
                add_progress_event(progress_id, account.get("username"), False, f"题目详情预访问失败：{exc}")
    final_total = max(len(imported_accounts), 1)
    update_progress(progress_id, final_total, final_total, message=f"导入完成，登录成功 {ok_count}/{len(login_results)}，已自动同步全部内容" if ok_count else f"导入完成，登录成功 {ok_count}/{len(login_results)}", done=True, ok=ok_count == len(login_results))
    return api_ok({"created": created, "updated": updated, "login_results": login_results, "sync": sync_result, "accounts": [account_public(a) for a in accounts_data["accounts"]]})


@app.route("/api/accounts/<account_id>", methods=["PATCH"])
@require_auth
def api_account_patch(account_id):
    body = json_body()
    accounts_data = load_accounts()
    account = find_account(accounts_data, account_id)
    if not account:
        return api_error("账号不存在", 404)
    if body.get("username"):
        account["username"] = str(body.get("username")).strip()
    if body.get("password"):
        account["password"] = str(body.get("password"))
    account["updated_at"] = utc_now()
    clear_client_cache(account_id)
    save_accounts(accounts_data)
    return api_ok(account_public(account))


@app.route("/api/accounts/<account_id>", methods=["DELETE"])
@require_auth
def api_account_delete(account_id):
    accounts_data = load_accounts()
    account = find_account(accounts_data, account_id)
    if not account:
        return api_error("账号不存在", 404)
    accounts_data["accounts"] = [item for item in accounts_data.get("accounts", []) if item.get("id") != account_id]
    clear_client_cache(account_id)
    stats = delete_account_related_data(account_id)
    save_accounts(accounts_data)
    log_event("info", "account_deleted", account_id=account_id, username=account.get("username"), **stats)
    return api_ok(stats)


@app.route("/api/accounts/test-login-all", methods=["POST"])
@require_auth
def api_accounts_test_login_all():
    cfg = load_config()
    body = json_body()
    progress_id = progress_id_from_body(body)
    account_ids = body.get("account_ids") if isinstance(body.get("account_ids"), list) and body.get("account_ids") else None
    accounts_data, accounts = get_accounts_by_ids(account_ids, enabled_only=False)
    if not accounts:
        return api_error("没有可用账号")
    results = []
    sync_account_ids = []
    total = len(accounts)
    init_progress(progress_id, "登录所选账号", total)
    for index, account in enumerate(accounts, start=1):
        check_progress_control(progress_id)
        if consume_progress_skip(progress_id):
            results.append({"account_id": account["id"], "username": account.get("username"), "ok": False, "skipped": True})
            add_progress_event(progress_id, account.get("username"), True, "已跳过登录")
            continue
        update_progress(progress_id, index, total, account.get("username"), f"正在登录 {account.get('username')}")
        try:
            refresh_account_after_login(account, accounts_data, cfg, progress_id=progress_id, force=True)
        except ISCCError as exc:
            results.append({"account_id": account["id"], "username": account.get("username"), "ok": False, "error": str(exc)})
            add_progress_event(progress_id, account.get("username"), False, f"登录失败：{exc}")
            save_accounts(accounts_data)
            continue
        results.append({"account_id": account["id"], "username": account.get("username"), "ok": True, "last_login_at": account.get("last_login_at")})
        sync_account_ids.append(account["id"])
        add_progress_event(progress_id, account.get("username"), True, "登录成功")
        save_accounts(accounts_data)
    sync_result = None
    if sync_account_ids:
        update_progress(progress_id, total, total, message="登录完成，正在自动同步全部内容")
        sync_result = sync_run_operation(progress_id, {"account_ids": sync_account_ids})
    save_accounts(accounts_data)
    update_progress(progress_id, total, total, message="登录完成，已自动同步全部内容" if sync_account_ids else "登录完成", done=True, ok=True)
    return api_ok({"results": results, "sync": sync_result})


@app.route("/api/accounts/<account_id>/test-login", methods=["POST"])
@require_auth
def api_account_test_login(account_id):
    cfg = load_config()
    body = json_body()
    progress_id = progress_id_from_body(body)
    accounts_data = load_accounts()
    account = find_account(accounts_data, account_id)
    if not account:
        return api_error("账号不存在", 404)
    try:
        init_progress(progress_id, "登录账号", 1)
        update_progress(progress_id, 1, 1, account.get("username"), f"正在登录 {account.get('username')}")
        refresh_account_after_login(account, accounts_data, cfg, progress_id=progress_id, force=True)
        add_progress_event(progress_id, account.get("username"), True, "登录成功，已更新解题状态")
        update_progress(progress_id, 1, 1, message="登录完成，正在自动同步全部内容")
        sync_result = sync_run_operation(progress_id, {"account_ids": [account_id]})
        update_progress(progress_id, 1, 1, message="登录完成，已自动同步全部内容", done=True, ok=True)
        return api_ok({"username": account.get("username"), "last_login_at": account.get("last_login_at"), "sync": sync_result})
    except ISCCError as exc:
        save_accounts(accounts_data)
        add_progress_event(progress_id, account.get("username"), False, f"登录失败：{exc}")
        update_progress(progress_id, 1, 1, message=f"登录失败：{exc}", done=True, ok=False)
        return api_error(f"{account.get('username')} 登录失败：{exc}", 400)


@app.route("/api/config", methods=["GET"])
@require_auth
def api_config_get():
    return api_ok(safe_config(load_config()))


@app.route("/api/config", methods=["PATCH"])
@require_auth
def api_config_patch():
    cfg = load_config()
    old_cfg = json.loads(json.dumps(cfg))
    body = json_body()

    server = body.get("server") if isinstance(body.get("server"), dict) else {}
    if server.get("host"):
        cfg["server"]["host"] = str(server.get("host")).strip()
    if server.get("port"):
        try:
            cfg["server"]["port"] = max(1, min(65535, int(server.get("port"))))
        except (TypeError, ValueError):
            return api_error("监听端口必须是数字")

    iscc = body.get("iscc") if isinstance(body.get("iscc"), dict) else {}
    if iscc.get("base_url"):
        cfg["iscc"]["base_url"] = str(iscc.get("base_url")).strip().rstrip("/")
    if "timeout" in iscc:
        try:
            cfg["iscc"]["timeout"] = max(3, min(120, int(iscc.get("timeout") or 15)))
        except (TypeError, ValueError):
            return api_error("请求超时必须是数字")
    if "operation_delay_seconds" in iscc:
        try:
            cfg["iscc"]["operation_delay_seconds"] = parse_delay_seconds(
                iscc.get("operation_delay_seconds"),
                default=OPERATION_DELAY_DEFAULT_SECONDS,
                max_seconds=OPERATION_DELAY_MAX_SECONDS,
            )
        except ISCCError as exc:
            return api_error(str(exc))
    if "verify_tls" in iscc:
        cfg["iscc"]["verify_tls"] = bool(iscc.get("verify_tls"))
    if "skip_file_categories" in iscc and isinstance(iscc.get("skip_file_categories"), list):
        cfg["iscc"]["skip_file_categories"] = [normalize_category(item) for item in iscc.get("skip_file_categories") if str(item).strip()]
    if iscc.get("submit_payload_mode") in {"params", "data"}:
        cfg["iscc"]["submit_payload_mode"] = iscc.get("submit_payload_mode")
    proxy = iscc.get("proxy") if isinstance(iscc.get("proxy"), dict) else {}
    if proxy:
        cfg["iscc"].setdefault("proxy", {})
        if "enabled" in proxy:
            cfg["iscc"]["proxy"]["enabled"] = bool(proxy.get("enabled"))
        if isinstance(proxy.get("list"), list):
            cfg["iscc"]["proxy"]["list"] = parse_proxy_list(proxy.get("list"))
        mode = proxy.get("mode") if proxy.get("mode") in {"round_robin", "random"} else "round_robin"
        cfg["iscc"]["proxy"]["mode"] = mode

    cfg = save_config(cfg)
    old_iscc = old_cfg.get("iscc", {})
    new_iscc = cfg.get("iscc", {})
    cache_breaking_changed = any(old_iscc.get(key) != new_iscc.get(key) for key in ("base_url", "submit_payload_mode"))
    client_config_changed = cache_breaking_changed or any(
        old_iscc.get(key) != new_iscc.get(key)
        for key in ("verify_tls", "timeout", "proxy")
    )
    if client_config_changed:
        if cache_breaking_changed:
            clear_client_cache()
        else:
            with CLIENT_CACHE_LOCK:
                sync_cached_clients_after_config_saved(cfg)
    return api_ok(safe_config(cfg))


@app.route("/api/config/proxy-test", methods=["POST"])
@require_auth
def api_config_proxy_test():
    cfg = load_config()
    body = json_body()
    iscc = body.get("iscc") if isinstance(body.get("iscc"), dict) else {}
    proxy_cfg = iscc.get("proxy") if isinstance(iscc.get("proxy"), dict) else {}
    if "list" in proxy_cfg:
        proxies_to_test = parse_proxy_list(proxy_cfg.get("list"))
    else:
        proxies_to_test = parse_proxy_list(cfg.get("iscc", {}).get("proxy", {}).get("list", []))
    if not proxies_to_test:
        return api_ok({"results": [], "message": "没有代理可测试"})

    base_url = str(iscc.get("base_url") or cfg.get("iscc", {}).get("base_url") or ISCC_BASE_URL).strip().rstrip("/")
    verify_tls = bool(iscc.get("verify_tls")) if "verify_tls" in iscc else bool(cfg.get("iscc", {}).get("verify_tls", False))
    if not verify_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        timeout = max(1, min(8, int(iscc.get("timeout") or cfg.get("iscc", {}).get("timeout") or 8)))
    except (TypeError, ValueError):
        timeout = 8

    results = []
    for proxy_url in proxies_to_test:
        started = time.monotonic()
        row = {"proxy": redact_proxy_url(proxy_url), "ok": False, "latency_ms": None, "status_code": None, "error": None}
        try:
            response = requests.get(
                base_url or ISCC_BASE_URL,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ISCC-Flask-Manager/1.0)"},
                timeout=timeout,
                verify=verify_tls,
                proxies=proxy_dict(proxy_url),
            )
            row["latency_ms"] = int((time.monotonic() - started) * 1000)
            row["status_code"] = response.status_code
            row["ok"] = response.status_code < 500
        except requests.exceptions.Timeout:
            row["latency_ms"] = int((time.monotonic() - started) * 1000)
            row["error"] = "请求超时"
        except requests.exceptions.ProxyError as exc:
            row["latency_ms"] = int((time.monotonic() - started) * 1000)
            row["error"] = f"代理失败：{exc}"
        except requests.exceptions.RequestException as exc:
            row["latency_ms"] = int((time.monotonic() - started) * 1000)
            row["error"] = f"请求失败：{exc}"
        results.append(row)
    return api_ok({"results": results})


@app.route("/api/sync/challenges", methods=["POST"])
@require_auth
def api_sync_challenges():
    body = json_body()
    progress_id = progress_id_from_body(body)
    account_id = body.get("account_id")
    if not account_id:
        return api_error("请选择用于同步题目的账号")
    cfg = load_config()
    delay_seconds = configured_operation_delay(cfg)
    accounts_data, accounts = get_accounts_by_ids([account_id], enabled_only=False)
    if not accounts:
        return api_error("账号不存在或不可用，请重新选择")
    account = accounts[0]
    init_progress(progress_id, "同步题目", 1)
    update_progress(progress_id, 0, 1, account.get("username"), f"正在准备同步题目：{account.get('username')}")
    try:
        client = get_ready_client(account, accounts_data, cfg)
        listed = client.list_all_challenges()
        requested_ids = body.get("chal_ids") if isinstance(body.get("chal_ids"), list) and body.get("chal_ids") else None
        requested_ids = {str(item) for item in requested_ids if str(item).strip()} if requested_ids else None
        listed = [chal for chal in listed if not requested_ids or challenge_cache_key(challenge_source(chal), chal.get("id")) in requested_ids]
        total = max(len(listed), 1)
        update_progress(progress_id, 0, total, account.get("username"), f"正在同步题目详情：0/{total}")
        challenges_data = load_challenges()
        items = dict(challenges_data.get("items", {}))
        seen_ids = set()
        errors = []
        for index, chal in enumerate(listed, start=1):
            check_progress_control(progress_id)
            chal_id = chal.get("id")
            if chal_id is None:
                continue
            source = challenge_source(chal)
            cache_key = challenge_cache_key(source, chal_id)
            name = chal.get("name") or f"#{cache_key}"
            update_progress(progress_id, index, total, account.get("username"), f"正在同步题目详情：{index}/{total} [{challenge_source_label(source)}] {name}")
            seen_ids.add(cache_key)
            try:
                detail = client.get_challenge_detail(chal_id, source=source)
                mark_challenge_detail_visited(account.get("id"), client, cache_key)
                merged = dict(chal)
                merged.update(detail)
                merged["id"] = cache_key
                merged["remote_id"] = int(chal_id) if str(chal_id).isdigit() else chal_id
                merged["source"] = source
                if source == CHALLENGE_SOURCE_MEASURE:
                    merged["category"] = "实战"
                merged.setdefault("files", [])
                items[cache_key] = merged
                save_challenges({"updated_at": utc_now(), "items": items})
            except ISCCError as exc:
                partial = dict(items.get(cache_key, {}))
                partial.update(chal)
                partial["id"] = cache_key
                partial["remote_id"] = int(chal_id) if str(chal_id).isdigit() else chal_id
                partial["source"] = source
                if source == CHALLENGE_SOURCE_MEASURE:
                    partial["category"] = "实战"
                partial.setdefault("name", f"#{cache_key}")
                partial.setdefault("files", [])
                items[cache_key] = partial
                errors.append({"chal_id": cache_key, "error": str(exc)})
                save_challenges({"updated_at": utc_now(), "items": items})
            sleep_between_progress_items(progress_id, delay_seconds, index, total)
        items = {chal_id: item for chal_id, item in items.items() if chal_id in seen_ids or chal_id not in challenges_data.get("items", {})}
        save_challenges({"updated_at": utc_now(), "items": items})
        save_accounts(accounts_data)
        update_progress(progress_id, total, total, message="同步题目完成", done=True, ok=True)
        return api_ok({"listed": len(listed), "details_updated": len(seen_ids), "cache_total": len(items), "errors": errors})
    except ISCCError as exc:
        save_accounts(accounts_data)
        update_progress(progress_id, message=f"同步题目失败：{exc}", done=True, ok=False)
        return api_error(f"同步题目失败：{exc}", 400)


@app.route("/api/sync/solves", methods=["POST"])
@require_auth
def api_sync_solves():
    body = json_body()
    progress_id = progress_id_from_body(body)
    account_ids = body.get("account_ids") if isinstance(body.get("account_ids"), list) and body.get("account_ids") else None
    if not account_ids:
        return api_error("请选择需要同步题解的账号")
    cfg = load_config()
    delay_seconds = configured_operation_delay(cfg)
    accounts_data, accounts = get_accounts_by_ids(account_ids, enabled_only=False)
    if not accounts:
        return api_error("账号不存在或不可用，请重新选择")

    solves_data = load_solves()
    results = []
    total = len(accounts)
    init_progress(progress_id, "同步题解", total)
    for index, account in enumerate(accounts, start=1):
        check_progress_control(progress_id)
        if consume_progress_skip(progress_id):
            results.append({"account_id": account["id"], "account_username": account.get("username"), "ok": False, "skipped": True, "count": 0})
            add_progress_event(progress_id, account.get("username"), True, "已跳过同步题解")
            continue
        update_progress(progress_id, index, total, account.get("username"), f"正在同步题解：{account.get('username')}")
        try:
            client = get_ready_client(account, accounts_data, cfg)
            solves = client.list_all_solves()
            set_account_solves(solves_data, account, solves)
            solves_data["updated_at"] = utc_now()
            save_solves(solves_data)
            save_accounts(accounts_data)
            results.append({"account_id": account["id"], "account_username": account.get("username"), "ok": True, "count": len(solves)})
            add_progress_event(progress_id, account.get("username"), True, f"同步题解成功：{len(solves)} 条")
        except ISCCError as exc:
            save_accounts(accounts_data)
            results.append({"account_id": account["id"], "account_username": account.get("username"), "ok": False, "error": str(exc), "count": 0})
            add_progress_event(progress_id, account.get("username"), False, f"同步题解失败：{exc}")
        sleep_between_progress_items(progress_id, delay_seconds, index, total)
    solves_data["updated_at"] = utc_now()
    save_solves(solves_data)
    save_accounts(accounts_data)
    update_progress(progress_id, total, total, message="同步题解完成", done=True, ok=True)
    return api_ok({"results": results})




def sync_run_operation(progress_id, body):
    account_ids = body.get("account_ids") if isinstance(body.get("account_ids"), list) and body.get("account_ids") else None
    if not account_ids:
        raise ISCCError("请选择需要同步的账号")
    chal_ids = body.get("chal_ids") if isinstance(body.get("chal_ids"), list) and body.get("chal_ids") else None
    requested_ids = {str(item) for item in chal_ids if str(item).strip()} if chal_ids else None
    cfg = load_config()
    delay_seconds = configured_operation_delay(cfg)
    accounts_data, accounts = get_accounts_by_ids(account_ids, enabled_only=False)
    if not accounts:
        raise ISCCError("账号不存在或不可用，请重新选择")

    total = max(len(accounts), 1)
    init_progress(progress_id, "同步题目与题解", total)
    first = accounts[0]
    challenges_updated = 0
    cache_total = 0
    errors = []
    seen_ids = set()
    try:
        update_progress(progress_id, 0, total, first.get("username"), f"正在同步题目：{first.get('username')}")
        client = get_ready_client(first, accounts_data, cfg)
        listed = client.list_all_challenges()
        listed = [chal for chal in listed if not requested_ids or challenge_cache_key(challenge_source(chal), chal.get("id")) in requested_ids]
        challenges_data = load_challenges()
        items = dict(challenges_data.get("items", {}))
        for detail_index, chal in enumerate(listed, start=1):
            check_progress_control(progress_id)
            chal_id = chal.get("id")
            if chal_id is None:
                continue
            source = challenge_source(chal)
            cache_key = challenge_cache_key(source, chal_id)
            name = chal.get("name") or f"#{cache_key}"
            update_progress(progress_id, detail_index, len(listed), first.get("username"), f"正在同步题目详情：{detail_index}/{len(listed)} [{challenge_source_label(source)}] {name}")
            seen_ids.add(cache_key)
            try:
                detail = client.get_challenge_detail(chal_id, source=source)
                mark_challenge_detail_visited(first.get("id"), client, cache_key)
                previous = items.get(cache_key, {})
                custom_name = previous.get("custom_name")
                merged = dict(chal)
                merged.update(detail)
                merged["id"] = cache_key
                merged["remote_id"] = int(chal_id) if str(chal_id).isdigit() else chal_id
                merged["source"] = source
                if custom_name:
                    merged["custom_name"] = custom_name
                    merged["name"] = custom_name
                merged.setdefault("files", [])
                items[cache_key] = merged
            except ISCCError as exc:
                partial = dict(items.get(cache_key, {}))
                partial.update(chal)
                partial["id"] = cache_key
                partial["remote_id"] = int(chal_id) if str(chal_id).isdigit() else chal_id
                partial["source"] = source
                if source == CHALLENGE_SOURCE_MEASURE:
                    partial["category"] = "实战"
                partial.setdefault("name", name)
                partial.setdefault("files", [])
                items[cache_key] = partial
                errors.append({"chal_id": cache_key, "error": str(exc)})
            sleep_between_progress_items(progress_id, delay_seconds, detail_index, len(listed))
        if requested_ids is None:
            items = {chal_id: item for chal_id, item in items.items() if chal_id in seen_ids}
        save_challenges({"updated_at": utc_now(), "items": items})
        challenges_updated = len(seen_ids)
        cache_total = len(items)
        add_progress_event(progress_id, first.get("username"), True, f"题目同步完成：{challenges_updated} 个")
    except ISCCError as exc:
        errors.append({"account_id": first.get("id"), "error": str(exc)})
        add_progress_event(progress_id, first.get("username"), False, f"题目同步失败：{exc}")

    synced_challenge_ids = sorted(seen_ids, key=challenge_sort_key)
    solves_data = load_solves()
    results = []
    for index, account in enumerate(accounts, start=1):
        check_progress_control(progress_id)
        if consume_progress_skip(progress_id):
            results.append({"account_id": account["id"], "account_username": account.get("username"), "ok": False, "skipped": True, "count": 0})
            add_progress_event(progress_id, account.get("username"), True, "已跳过同步题解")
            continue
        update_progress(progress_id, index, total, account.get("username"), f"正在同步题解：{account.get('username')}")
        try:
            client = get_ready_client(account, accounts_data, cfg)
            if synced_challenge_ids:
                previsit_cached_challenge_details(client, account_id=account["id"], challenge_ids=synced_challenge_ids, progress_id=progress_id, username=account.get("username"), delay_seconds=delay_seconds)
            solves = client.list_all_solves()
            set_account_solves(solves_data, account, solves)
            results.append({"account_id": account["id"], "account_username": account.get("username"), "ok": True, "count": len(solves)})
            add_progress_event(progress_id, account.get("username"), True, f"题解同步成功：{len(solves)} 条，已访问 {len(synced_challenge_ids)} 个题目详情")
        except ISCCError as exc:
            results.append({"account_id": account["id"], "account_username": account.get("username"), "ok": False, "error": str(exc), "count": 0})
            add_progress_event(progress_id, account.get("username"), False, f"题解同步失败：{exc}")
        sleep_between_progress_items(progress_id, delay_seconds, index, total)
    solves_data["updated_at"] = utc_now()
    save_solves(solves_data)
    save_accounts(accounts_data)
    ok = not errors and any(row.get("ok") for row in results)
    update_progress(progress_id, total, total, message="同步完成", done=True, ok=ok)
    return {"details_updated": challenges_updated, "cache_total": cache_total, "solves": results, "errors": errors}


@app.route("/api/sync/run", methods=["POST"])
@require_auth
def api_sync_run():
    body = json_body()
    account_ids = body.get("account_ids") if isinstance(body.get("account_ids"), list) and body.get("account_ids") else None
    if not account_ids:
        return api_error("请选择需要同步的账号")
    progress_id = progress_id_from_body(body) or uuid.uuid4().hex
    start_background_operation(progress_id, "同步题目与题解", len(account_ids), sync_run_operation, body)
    return api_ok({"progress_id": progress_id, "started": True})

@app.route("/api/challenges")
@require_auth
def api_challenges():
    account_id = request.args.get("account_id") or ""
    category = request.args.get("category") or ""
    status_filter = request.args.get("status") or "all"
    q = (request.args.get("q") or "").strip().lower()

    challenges_data = load_challenges()
    solves_data = load_solves()
    accounts_data = load_accounts()
    account_names = {account.get("id"): account.get("username") for account in accounts_data.get("accounts", [])}
    solved_by_chal = challenge_solve_map(solves_data)
    items = []
    for chal_id, challenge in challenges_data.get("items", {}).items():
        display_category = challenge_display_category(challenge)
        solved_ids = sorted(solved_by_chal.get(str(chal_id), set()))
        solved = account_id in solved_ids if account_id else bool(solved_ids)
        if category and display_category != category:
            continue
        if status_filter == "solved" and not solved:
            continue
        if status_filter == "unsolved" and solved:
            continue
        if q:
            haystack = f"{chal_id} {challenge.get('name', '')} {display_category} {challenge_source_label(challenge_source(challenge))}".lower()
            if q not in haystack:
                continue
        item = apply_challenge_custom_name(challenge)
        item["category"] = display_category
        item["id"] = str(challenge.get("id") or chal_id)
        item["source_label"] = challenge_source_label(challenge_source(challenge))
        item["solved"] = solved
        item["solved_by_ids"] = solved_ids
        item["solved_by"] = [{"account_id": aid, "username": account_names.get(aid, aid)} for aid in solved_ids]
        item["unsolved_by"] = [
            {"account_id": account.get("id"), "username": account.get("username")}
            for account in accounts_data.get("accounts", [])
            if account.get("id") not in solved_ids
        ]
        visited_ids = challenge_visit_account_ids(chal_id, accounts_data.get("accounts", []))
        item["visited_by_ids"] = visited_ids
        item["visited_by"] = [{"account_id": aid, "username": account_names.get(aid, aid)} for aid in visited_ids]
        item["unvisited_by"] = [
            {"account_id": account.get("id"), "username": account.get("username")}
            for account in accounts_data.get("accounts", [])
            if account.get("id") not in visited_ids
        ]
        item["visit_count"] = len(visited_ids)
        item["account_count"] = len(accounts_data.get("accounts", []))
        item.setdefault("files", [])
        items.append(item)
    items.sort(key=lambda x: (str(x.get("category", "")), challenge_sort_key(x.get("id"))))
    total = len(challenges_data.get("items", {}))
    solved_any = sum(1 for chal_id in challenges_data.get("items", {}) if solved_by_chal.get(str(chal_id)))
    return api_ok(
        {
            "items": items,
            "updated_at": challenges_data.get("updated_at"),
            "stats": {"total": total, "solved_any": solved_any, "unsolved_any": max(total - solved_any, 0)},
        }
    )


@app.route("/api/challenges/<path:chal_id>", methods=["PATCH"])
@require_auth
def api_challenge_patch(chal_id):
    body = json_body()
    name = str(body.get("name") or "").strip()
    if not name:
        return api_error("题目名称不能为空")
    challenges_data = load_challenges()
    challenge = challenges_data.get("items", {}).get(str(chal_id))
    if not challenge:
        return api_error("题目不存在，请先同步题目", 404)
    challenge["custom_name"] = name
    challenge["name"] = name
    challenges_data["updated_at"] = utc_now()
    save_challenges(challenges_data)
    return api_ok(apply_challenge_custom_name(challenge))


@app.route("/api/challenges/<path:chal_id>")
@require_auth
def api_challenge_detail(chal_id):
    challenges_data = load_challenges()
    challenge = challenges_data.get("items", {}).get(str(chal_id))
    if not challenge:
        return api_error("题目不存在，请先同步题目", 404)
    solves_data = load_solves()
    accounts_data = load_accounts()
    account_names = {account.get("id"): account.get("username") for account in accounts_data.get("accounts", [])}
    solved_ids = sorted(challenge_solve_map(solves_data).get(str(chal_id), set()))
    files = flatten_files(load_files(), chal_id=chal_id)
    item = apply_challenge_custom_name(challenge)
    item["solved_by"] = [{"account_id": aid, "username": account_names.get(aid, aid)} for aid in solved_ids]
    item["unsolved_by"] = [
        {"account_id": account.get("id"), "username": account.get("username")}
        for account in accounts_data.get("accounts", [])
        if account.get("enabled", True) and account.get("id") not in solved_ids
    ]
    item["solved_by_ids"] = solved_ids
    item["downloaded_files"] = files
    return api_ok(item)


def validate_submission_payload(account_id, chal_id, flag):
    if not account_id:
        return "请选择账号"
    if not chal_id:
        return "请选择题目"
    source, remote_id = split_challenge_ref(chal_id, fallback_source=cached_challenge_source(chal_id))
    if not remote_id or source not in CHALLENGE_SOURCES:
        return "题目 ID 无效"
    if not flag:
        return "flag 不能为空"
    return None


@app.route("/api/flags")
@require_auth
def api_flags_list():
    account_id = request.args.get("account_id") or None
    chal_id = request.args.get("chal_id") or None
    return api_ok(flatten_flags(load_flags(), account_id=account_id, chal_id=chal_id))


@app.route("/api/logs")
@require_auth
def api_logs():
    return api_ok(public_logs(request.args.get("limit") or 50, request.args.get("offset") or 0))


@app.route("/api/progress")
@require_auth
def api_progress_list():
    return api_ok({"jobs": list_progress_jobs()})


@app.route("/api/progress/<progress_id>")
@require_auth
def api_progress(progress_id):
    progress = get_progress(progress_id)
    if not progress:
        return api_ok({"done": True, "message": "没有正在运行的操作", "current": 0, "total": 0})
    return api_ok(progress)


@app.route("/api/progress/<progress_id>/pause", methods=["POST"])
@require_auth
def api_progress_pause(progress_id):
    progress = set_progress_paused(progress_id, True)
    if not progress:
        return api_error("任务不存在或已结束", 404)
    return api_ok(get_progress(progress_id))


@app.route("/api/progress/<progress_id>/resume", methods=["POST"])
@require_auth
def api_progress_resume(progress_id):
    progress = set_progress_paused(progress_id, False)
    if not progress:
        return api_error("任务不存在或已结束", 404)
    return api_ok(get_progress(progress_id))


@app.route("/api/progress/<progress_id>/cancel", methods=["POST"])
@require_auth
def api_progress_cancel(progress_id):
    progress = request_progress_cancel(progress_id)
    if not progress:
        return api_error("任务不存在或已结束", 404)
    return api_ok(get_progress(progress_id))


@app.route("/api/progress/<progress_id>/skip", methods=["POST"])
@require_auth
def api_progress_skip(progress_id):
    progress = request_progress_skip(progress_id)
    if not progress:
        return api_error("任务不存在或已结束", 404)
    return api_ok(get_progress(progress_id))


@app.route("/api/submit", methods=["POST"])
@require_auth
def api_submit():
    cfg = load_config()
    body = json_body()
    progress_id = progress_id_from_body(body)
    account_id = body.get("account_id")
    chal_id = body.get("chal_id")
    flag = str(body.get("flag") or "").strip()
    md5 = str(body.get("md5") or "").strip().lower()
    error = validate_submission_payload(account_id, chal_id, flag)
    if error:
        return api_error(error)

    accounts_data = load_accounts()
    account = find_account(accounts_data, account_id)
    if not account:
        return api_error("账号不存在", 404)
    flags_data = load_flags()
    if account_id in submitted_account_ids_for_challenge(chal_id, flags_data=flags_data):
        return api_error("该账号已解或已成功提交过该题，请勿重复提交")

    result = {
        "account_id": account_id,
        "account_username": account.get("username"),
        "chal_id": challenge_cache_key(cached_challenge_source(chal_id), chal_id),
        "ok": True,
        "message": "",
    }
    client = None

    try:
        init_progress(progress_id, "提交 Flag", 1)
        update_progress(progress_id, 1, 1, account.get("username"), f"正在提交：{account.get('username')}")
        client = get_ready_client(account, accounts_data, cfg)
        result.update(submit_flag_for_account(account, client, chal_id, flag, flags_data, md5=md5, source="single"))
        check_progress_control(progress_id)
        save_accounts(accounts_data)
        update_progress(progress_id, 1, 1, message=result.get("message") or "提交完成", done=True, ok=result["ok"])
        if result["ok"]:
            try:
                solves_data = load_solves()
                set_account_solves(solves_data, account, client.list_all_solves())
                save_solves(solves_data)
            except ISCCError as exc:
                log_event("warn", "post_submit_solves_refresh_failed", account_id=account.get("id"), username=account.get("username"), chal_id=result.get("chal_id"), error=str(exc))
        return api_ok(result)
    except ISCCError as exc:
        if exc.auth_error:
            clear_client_cache(account_id)
            mark_account_login(accounts_data, account_id, False, exc)
        save_accounts(accounts_data)
        result.update({"ok": False, "error": str(exc), "message": str(exc), "proxy": client.proxy_label if client else None})
        update_progress(progress_id, 1, 1, message=str(exc), done=True, ok=False)
        log_event(
            "error",
            "flag_submit_error",
            account_id=account_id,
            username=account.get("username"),
            proxy=client.proxy_label if client else None,
            chal_id=result.get("chal_id"),
            error=str(exc),
            flag_md5=flag_md5(flag),
            attachment_md5=md5,
        )
        return api_ok(result)


def submit_batch_operation(progress_id, body):
    cfg = load_config()
    delay_seconds = configured_operation_delay(cfg)
    submissions = body.get("submissions") if isinstance(body.get("submissions"), list) else []
    if not submissions and body.get("chal_id") and body.get("flag"):
        submissions = [{"chal_id": body.get("chal_id"), "flag": body.get("flag")}]
    cleaned = []
    for item in submissions:
        chal_id = str(item.get("chal_id") or "").strip()
        flag = str(item.get("flag") or "").strip()
        if chal_id and flag:
            cleaned.append({"chal_id": challenge_cache_key(cached_challenge_source(chal_id), chal_id), "flag": flag})
    if not cleaned:
        raise ISCCError("请选择题目并输入 flag")
    
    account_ids = body.get("account_ids") if isinstance(body.get("account_ids"), list) else None
    md5 = str(body.get("md5") or "").strip().lower()
    if not account_ids and md5 and len(cleaned) == 1:
        chal_id = cleaned[0]["chal_id"]
        matched = []
        for item in flatten_files(load_files(), chal_id=chal_id):
            if str(item.get("md5") or "").strip().lower() == md5 and item.get("account_id"):
                matched.append(item.get("account_id"))
        account_ids = sorted(set(matched))
    accounts_data, accounts = get_accounts_by_ids(account_ids, enabled_only=False)
    if not accounts:
        raise ISCCError("没有可用账号")
    
    flags_data = load_flags()
    blocked_by_chal = {item["chal_id"]: submitted_account_ids_for_challenge(item["chal_id"], flags_data=flags_data) for item in cleaned}
    results = []
    total = len(accounts)
    init_progress(progress_id, "批量提交 Flag", total)
    for index, account in enumerate(accounts, start=1):
        check_progress_control(progress_id)
        if consume_progress_skip(progress_id):
            add_progress_event(progress_id, account.get("username"), True, "已跳过提交账号")
            continue
        pending_items = [item for item in cleaned if account["id"] not in blocked_by_chal.get(item["chal_id"], set())]
        update_progress(progress_id, index, total, account.get("username"), f"正在提交：{account.get('username')}，待提交 {len(pending_items)} 个")
        for item in cleaned:
            if item not in pending_items:
                results.append(
                    {
                        "account_id": account["id"],
                        "account_username": account.get("username"),
                        "chal_id": item["chal_id"],
                        "ok": False,
                        "skipped": True,
                        "message": "已解或已成功提交过该题，已跳过",
                    }
                )
        if not pending_items:
            add_progress_event(progress_id, account.get("username"), True, "已跳过：没有待提交题目")
            continue
    
        account_ok = False
        account_errors = []
        client = None
        try:
            client = get_ready_client(account, accounts_data, cfg)
            any_success = False
            for item_index, item in enumerate(pending_items, start=1):
                check_progress_control(progress_id)
                if consume_progress_skip(progress_id):
                    add_progress_event(progress_id, account.get("username"), True, f"已跳过提交题目：{item['chal_id']}", chal_id=item["chal_id"])
                    continue
                try:
                    row = submit_flag_for_account(account, client, item["chal_id"], item["flag"], flags_data, md5=md5, source="batch")
                    if row["ok"]:
                        blocked_by_chal.setdefault(item["chal_id"], set()).add(account["id"])
                    else:
                        account_errors.append(row.get("message") or "提交失败")
                    account_ok = account_ok or row["ok"]
                    any_success = any_success or row["ok"]
                    results.append(row)
                except ISCCError as exc:
                    log_event(
                        "error",
                        "flag_submit_error",
                        account_id=account["id"],
                        username=account.get("username"),
                        proxy=client.proxy_label,
                        chal_id=item["chal_id"],
                        error=str(exc),
                        flag_md5=flag_md5(item["flag"]),
                        attachment_md5=md5,
                    )
                    account_errors.append(str(exc))
                    results.append(
                        {
                            "account_id": account["id"],
                            "account_username": account.get("username"),
                            "proxy": client.proxy_label,
                            "chal_id": item["chal_id"],
                            "ok": False,
                            "message": str(exc),
                            "error": str(exc),
                        }
                    )
                sleep_between_progress_items(progress_id, delay_seconds, item_index, len(pending_items))
            add_progress_event(progress_id, account.get("username"), account_ok, "提交成功" if account_ok else (account_errors[0] if account_errors else "提交失败"))
            if any_success:
                try:
                    solves = client.list_all_solves()
                    solves_data = load_solves()
                    set_account_solves(solves_data, account, solves)
                    save_solves(solves_data)
                except ISCCError as exc:
                    log_event("warn", "post_submit_solves_refresh_failed", account_id=account.get("id"), username=account.get("username"), error=str(exc))
        except ISCCError as exc:
            if exc.auth_error:
                clear_client_cache(account.get("id"))
                mark_account_login(accounts_data, account["id"], False, exc)
            log_event("error", "batch_account_failed", account_id=account.get("id"), username=account.get("username"), proxy=client.proxy_label if client else None, error=str(exc))
            add_progress_event(progress_id, account.get("username"), False, str(exc))
            for item in pending_items:
                results.append(
                    {
                        "account_id": account["id"],
                        "account_username": account.get("username"),
                        "proxy": client.proxy_label if client else None,
                        "chal_id": item["chal_id"],
                        "ok": False,
                        "message": str(exc),
                        "error": str(exc),
                    }
                )
        sleep_between_progress_items(progress_id, delay_seconds, index, total)
    save_accounts(accounts_data)
    update_progress(progress_id, total, total, message="批量提交完成", done=True, ok=True)
    return {"results": results}


@app.route("/api/submit/batch", methods=["POST"])
@require_auth
def api_submit_batch():
    body = json_body()
    progress_id = progress_id_from_body(body) or uuid.uuid4().hex
    submissions = body.get("submissions") if isinstance(body.get("submissions"), list) else []
    if not submissions and body.get("chal_id") and body.get("flag"):
        submissions = [{"chal_id": body.get("chal_id"), "flag": body.get("flag")}]
    total = len(body.get("account_ids") or []) or 1
    start_background_operation(progress_id, "批量提交 Flag", total, submit_batch_operation, body)
    return api_ok({"progress_id": progress_id, "started": True})


def files_update_operation(progress_id, body):
    cfg = load_config()
    delay_seconds = configured_operation_delay(cfg)
    account_ids = body.get("account_ids") if isinstance(body.get("account_ids"), list) and body.get("account_ids") else None
    if not account_ids:
        raise ISCCError("请选择需要更新附件的账号")
    chal_ids = body.get("chal_ids") if isinstance(body.get("chal_ids"), list) and body.get("chal_ids") else None
    chal_ids = {str(item) for item in chal_ids if str(item).strip()} if chal_ids else None
    
    accounts_data, accounts = get_accounts_by_ids(account_ids, enabled_only=False)
    if not accounts:
        raise ISCCError("没有可用账号")
    challenges_data = load_challenges()
    challenges = challenges_data.get("items", {})
    selected_challenges = [(chal_id, challenge) for chal_id, challenge in challenges.items() if not chal_ids or str(chal_id) in chal_ids]
    
    files_data = load_files()
    reusable_by_source = reusable_file_index(files_data)
    files_changed = False
    solves_data = load_solves()
    solves_changed = False
    results = []
    total = len(accounts)
    init_progress(progress_id, "更新附件", total)
    for index, account in enumerate(accounts, start=1):
        check_progress_control(progress_id)
        if consume_progress_skip(progress_id):
            add_progress_event(progress_id, account.get("username"), True, "已跳过附件更新账号")
            continue
        update_progress(progress_id, index, total, account.get("username"), f"正在更新附件：{account.get('username')}")
        try:
            client = get_ready_client(account, accounts_data, cfg)
            try:
                solves = client.list_all_solves()
                set_account_solves(solves_data, account, solves)
                solves_data["updated_at"] = utc_now()
                save_solves(solves_data)
                solves_changed = True
            except ISCCError as exc:
                log_event("warn", "iscc_solves_update_failed", account_id=account.get("id"), username=account.get("username"), error=str(exc))
            account_challenges = list(selected_challenges)
            if not account_challenges:
                listed = client.list_all_challenges()
                listed_map = {challenge_cache_key(challenge_source(chal), chal.get("id")): chal for chal in listed if chal.get("id") is not None}
                wanted_ids = chal_ids or set(listed_map.keys())
                account_challenges = [(chal_id, listed_map.get(str(chal_id), {"id": chal_id, "name": f"#{chal_id}"})) for chal_id in wanted_ids if str(chal_id) in listed_map or chal_ids]
            if not account_challenges:
                raise ISCCError("没有匹配的题目")
            account_files = files_data.setdefault("accounts", {}).setdefault(account["id"], {})
            downloaded_count = 0
            skipped_count = 0
            errors = []
            challenge_total = len(account_challenges)
            for challenge_index, (chal_id, challenge) in enumerate(account_challenges, start=1):
                check_progress_control(progress_id)
                try:
                    if consume_progress_skip(progress_id):
                        add_progress_event(progress_id, account.get("username"), True, f"已跳过附件题目：{chal_id}", kind="files", account_id=account.get("id"), chal_id=chal_id)
                        continue
                    challenge_name = challenge.get("name") or f"#{chal_id}"
                    update_progress(progress_id, index, total, account.get("username"), f"正在更新附件：{account.get('username')}，题目 {challenge_index}/{challenge_total} {challenge_name}")
                    cached_detail = cached_account_challenge_detail(account.get("id"), client, chal_id)
                    can_reuse_detail = bool(cached_detail and cached_detail.get("files")) and has_challenge_detail_visit(account.get("id"), client, chal_id)
                    if can_reuse_detail:
                        detail = cached_detail
                    else:
                        detail = refresh_challenge_detail_for_account(client, chal_id, fallback=challenge, account_id=account.get("id"))
                        mark_challenge_detail_visited(account.get("id"), client, chal_id)
                    detail.setdefault("id", str(chal_id))
                    detail.setdefault("name", challenge.get("name") or f"#{chal_id}")
                    detail.setdefault("category", challenge_display_category(challenge))
                    if not detail.get("files"):
                        skipped_count += 1
                        if account_files.pop(str(chal_id), None) is not None:
                            files_changed = True
                        add_progress_event(progress_id, account.get("username"), True, f"{challenge_name}：无附件，已跳过", kind="files", account_id=account.get("id"), chal_id=chal_id)
                        log_event("info", "file_update_no_account_file", account_id=account.get("id"), username=account.get("username"), chal_id=chal_id, message="当前账号该题没有附件")
                        continue
                    file_count = len(detail.get("files") or [])
                    challenge_name = detail.get("name") or challenge_name
                    update_progress(progress_id, index, total, account.get("username"), f"正在下载附件：{account.get('username')}，题目 {challenge_index}/{challenge_total} {challenge_name}，文件 {file_count} 个")
                    existing_rows = []
                    missing_sources = []
                    for source_path in detail.get("files") or []:
                        source_url = urljoin(f"{client.base_url}/", str(source_path).lstrip("/"))
                        existing = existing_account_file_by_source(files_data, account["id"], chal_id, source_url)
                        if existing:
                            existing_rows.append(existing)
                        else:
                            missing_sources.append(source_path)
                    download_detail = dict(detail)
                    download_detail["files"] = missing_sources
                    downloaded = client.download_challenge_files(account["id"], download_detail, reusable_by_source=reusable_by_source) if missing_sources else []
                    final_rows = existing_rows + downloaded
                    if final_rows:
                        account_files[str(chal_id)] = final_rows
                        files_changed = True
                        for item in final_rows:
                            if item.get("source_url") and item.get("md5"):
                                reusable_by_source.setdefault(str(item.get("source_url")), item)
                        downloaded_count += len([item for item in downloaded if not item.get("reused")])
                        skipped_count += len(existing_rows) + len([item for item in downloaded if item.get("reused")])
                    else:
                        skipped_count += 1
                        if account_files.pop(str(chal_id), None) is not None:
                            files_changed = True
                    add_progress_event(progress_id, account.get("username"), True, f"{challenge_name}：下载 {len([item for item in downloaded if not item.get('reused')])}，跳过/复用 {len(existing_rows) + len([item for item in downloaded if item.get('reused')])}", kind="files", account_id=account.get("id"), chal_id=chal_id)
                except ISCCError as exc:
                    log_event("warn", "file_update_challenge_failed", account_id=account.get("id"), username=account.get("username"), chal_id=chal_id, error=str(exc))
                    errors.append({"chal_id": chal_id, "error": str(exc)})
            results.append(
                {
                    "account_id": account["id"],
                    "account_username": account.get("username"),
                    "ok": not errors,
                    "files_downloaded": downloaded_count,
                    "skipped": skipped_count,
                    "errors": errors,
                }
            )
            save_accounts(accounts_data)
            add_progress_event(progress_id, account.get("username"), not errors, f"附件更新完成：新增下载 {downloaded_count} 个，复用/跳过 {skipped_count} 个" if not errors else f"附件更新失败：{len(errors)} 个错误")
        except ISCCError as exc:
            add_progress_event(progress_id, account.get("username"), False, f"附件更新失败：{exc}")
            results.append(
                {
                    "account_id": account["id"],
                    "account_username": account.get("username"),
                    "ok": False,
                    "files_downloaded": 0,
                    "skipped": 0,
                    "errors": [{"error": str(exc)}],
                }
            )
            save_accounts(accounts_data)
        sleep_between_progress_items(progress_id, delay_seconds, index, total)
    if files_changed:
        files_data["updated_at"] = utc_now()
        save_files(files_data)
    if solves_changed:
        save_solves(solves_data)
    save_accounts(accounts_data)
    total_downloaded = sum(item.get("files_downloaded", 0) for item in results)
    total_skipped = sum(item.get("skipped", 0) for item in results)
    update_progress(progress_id, total, total, message=f"附件更新完成：新增下载 {total_downloaded} 个，复用/跳过 {total_skipped} 个", done=True, ok=True)
    return {"results": results}


@app.route("/api/files/update", methods=["POST"])
@require_auth
def api_files_update():
    body = json_body()
    account_ids = body.get("account_ids") if isinstance(body.get("account_ids"), list) and body.get("account_ids") else None
    if not account_ids:
        return api_error("请选择需要更新附件的账号")
    progress_id = progress_id_from_body(body) or uuid.uuid4().hex
    start_background_operation(progress_id, "更新附件", len(account_ids), files_update_operation, body)
    return api_ok({"progress_id": progress_id, "started": True})


@app.route("/api/files")
@require_auth
def api_files_list():
    account_id = request.args.get("account_id") or None
    chal_id = request.args.get("chal_id") or None
    return api_ok(flatten_files(load_files(), account_id=account_id, chal_id=chal_id))



def delete_file_records(file_ids):
    file_ids = {str(file_id) for file_id in file_ids if str(file_id).strip()}
    files_data = load_files()
    removed = []
    paths_to_check = []
    for account_id, chal_map in list(files_data.get("accounts", {}).items()):
        for chal_id, files in list(chal_map.items()):
            kept = []
            for item in files or []:
                if item.get("file_id") in file_ids:
                    removed.append(item)
                    if item.get("path"):
                        paths_to_check.append(Path(item.get("path")))
                else:
                    kept.append(item)
            if kept:
                chal_map[chal_id] = kept
            else:
                chal_map.pop(chal_id, None)
        if not chal_map:
            files_data.get("accounts", {}).pop(account_id, None)
    referenced_paths = {str(item.get("path") or "") for item in flatten_files(files_data) if item.get("path")}
    for path in paths_to_check:
        if path.exists() and path.is_file() and str(path) not in referenced_paths:
            downloads_root = DOWNLOAD_DIR.resolve()
            resolved = path.resolve()
            if downloads_root in resolved.parents or resolved == downloads_root:
                resolved.unlink(missing_ok=True)
    files_data["updated_at"] = utc_now()
    save_files(files_data)
    cleanup_empty_download_dirs()
    return removed


@app.route("/api/files/delete", methods=["POST"])
@require_auth
def api_files_delete_batch():
    body = json_body()
    file_ids = body.get("file_ids") if isinstance(body.get("file_ids"), list) else []
    if not file_ids:
        return api_error("请选择要删除的文件")
    removed = delete_file_records(file_ids)
    return api_ok({"deleted": len(removed)})


@app.route("/api/files/<file_id>", methods=["DELETE"])
@require_auth
def api_file_delete(file_id):
    removed = delete_file_records([file_id])
    if not removed:
        return api_error("文件记录不存在", 404)
    return api_ok({"deleted": len(removed)})


@app.route("/api/files/download/<file_id>")
@require_auth
def api_file_download(file_id):
    for item in flatten_files(load_files()):
        if item.get("file_id") != file_id:
            continue
        path = Path(item.get("path") or "").resolve()
        downloads_root = DOWNLOAD_DIR.resolve()
        if downloads_root not in path.parents and path != downloads_root:
            return api_error("文件路径非法", 400)
        if not path.exists() or not path.is_file():
            return api_error("文件不存在", 404)
        return send_file(path, as_attachment=True, download_name=item.get("original_name") or path.name)
    return api_error("文件记录不存在", 404)


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return api_error("接口不存在", 404)
    return render_template("index.html")


@app.errorhandler(Exception)
def handle_exception(error):
    if isinstance(error, OperationCancelled):
        return api_ok({"cancelled": True, "message": str(error) or "已停止"})
    if isinstance(error, HTTPException):
        if request.path.startswith("/api/"):
            return api_error(error.description or error.name, error.code or 500)
        return render_template("index.html"), error.code
    if request.path.startswith("/api/"):
        return api_error(f"服务异常：{error}", 500)
    raise error


if __name__ == "__main__":
    cfg = load_config()
    server = cfg.get("server", {})
    app.run(
        host=server.get("host") or "0.0.0.0",
        port=int(server.get("port") or 5000),
        debug=bool(server.get("debug", False)),
    )
