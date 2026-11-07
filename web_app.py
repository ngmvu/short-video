from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import dotenv_values
from filelock import FileLock, Timeout
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from core.config import DOWNLOADS_DIR, ROOT_DIR, STORAGE_DIR
from core.gemini.idea_generator import (
    _normalize_social_posts,
    generate_social_posts_for_video,
)
from core.social_uploader import (
    build_youtube_shorts_title,
    parse_hashtags,
    publish_to_social,
)
from core.storage import (
    TELEGRAM_POLL_LOCK_FILE,
    approve_draft,
    clear_approved_queue,
    delete_draft,
    delete_queue_item,
    write_json_atomic,
)
from core.tele.notifier import send_final_report

LOGGER = logging.getLogger("short_video_dashboard")
ENV_FILE = ROOT_DIR / ".env"
LOG_FILE = STORAGE_DIR / "dashboard.log"
LOG_BACKUP_FILE = STORAGE_DIR / "dashboard.log.1"
FLOW_STATUS_FILE = STORAGE_DIR / "workflow_status.json"
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_LOG_VIEW_BYTES = 64 * 1024
FLOW_NODE_TYPES = {
    "generate-ideas",
    "telegram-listener",
    "process-queue",
    "approval-video",
    "approval-publish",
    "automation",
}
LONG_RUNNING_FLOW_NODES = {"approval-video", "approval-publish", "automation", "telegram-listener"}
FLOW_SELECTOR_KEYS = (
    "FLOW_AGENT_TOGGLE_SELECTOR",
    "FLOW_SETTINGS_SELECTOR",
    "FLOW_VIDEO_MODE_SELECTOR",
    "FLOW_ASPECT_RATIO_SELECTOR",
    "FLOW_DURATION_SELECTOR",
    "FLOW_OUTPUT_COUNT_SELECTOR",
    "FLOW_PROMPT_SELECTOR",
    "FLOW_SUBMIT_SELECTOR",
    "FLOW_RESULT_SELECTOR",
    "FLOW_DOWNLOAD_MENU_SELECTOR",
    "FLOW_DOWNLOAD_SELECTOR",
)
IGNORED_LEGACY_FLOW_SELECTOR_KEYS = {"FLOW_FINALIZE_SELECTOR"}
SECRET_KEYS = {
    "GEMINI_API_KEY",
    "GEMINI_FALLBACK_API_KEYS",
    "TELEGRAM_BOT_TOKEN",
    "TIKTOK_ACCESS_TOKEN",
    "FB_PAGE_ACCESS_TOKEN",
}
ENV_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Gemini",
        "Sinh ý tưởng video và nhân vật Assets mặc định (@khi, @nguoi)",
        (
            "GEMINI_API_KEY",
            "GEMINI_FALLBACK_API_KEYS",
            "GEMINI_MODEL",
            "GEMINI_FALLBACK_MODELS",
            "IDEA_STYLE",
            "CHARACTER_1_TAG",
            "CHARACTER_1_VOICE",
            "CHARACTER_2_TAG",
            "CHARACTER_2_VOICE",
        ),
    ),
    (
        "Telegram",
        "Duyệt ý tưởng và nhận báo cáo",
        ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    ),
    (
        "Google Flow",
        "Tạo video bằng giao diện Flow",
        (
            "FLOW_PROJECT_URL",
            "FLOW_AUTH_FILE",
            *FLOW_SELECTOR_KEYS,
            "FLOW_RENDER_TIMEOUT_MS",
            "FLOW_DOWNLOAD_TIMEOUT_MS",
        ),
    ),
    (
        "YouTube",
        "Đăng YouTube Shorts",
        ("YOUTUBE_CLIENT_SECRETS_FILE", "YOUTUBE_TOKEN_FILE", "YOUTUBE_MADE_FOR_KIDS"),
    ),
    (
        "TikTok",
        "Đăng TikTok",
        ("TIKTOK_ACCESS_TOKEN", "TIKTOK_PRIVACY_LEVEL"),
    ),
    (
        "Facebook",
        "Đăng Facebook Reels",
        ("FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN"),
    ),
    ("Ứng dụng", "Log và vận hành", ("LOG_LEVEL",)),
)
FIELD_LABELS = {
    "GEMINI_API_KEY": "Gemini API key chính (hoặc nhiều key cách nhau bằng dấu phẩy)",
    "GEMINI_FALLBACK_API_KEYS": "Gemini API key dự phòng (nhiều key cách nhau bằng dấu phẩy)",
    "GEMINI_MODEL": "Gemini model chính",
    "GEMINI_FALLBACK_MODELS": "Các model miễn phí dự phòng (phân cách bằng dấu phẩy)",
    "IDEA_STYLE": "Phong cách/chủ đề ý tưởng (có thể thay đổi)",
    "CHARACTER_1_TAG": "Thẻ Asset Nhân vật 1 (mặc định @khi)",
    "CHARACTER_1_VOICE": "Mẫu giọng Nhân vật 1 (mặc định @Voice_Khi)",
    "CHARACTER_2_TAG": "Thẻ Asset Nhân vật 2 (mặc định @nguoi)",
    "CHARACTER_2_VOICE": "Mẫu giọng Nhân vật 2 (mặc định @Voice_Nguoi)",
    "TELEGRAM_BOT_TOKEN": "Telegram bot token",
    "TELEGRAM_CHAT_ID": "Telegram chat ID",
    "FLOW_PROJECT_URL": "Flow project URL",
    "FLOW_AUTH_FILE": "Playwright auth file",
    "FLOW_AGENT_TOGGLE_SELECTOR": "Disable Flow Agent mode selector",
    "FLOW_SETTINGS_SELECTOR": "Flow settings selector",
    "FLOW_VIDEO_MODE_SELECTOR": "Video mode selector",
    "FLOW_ASPECT_RATIO_SELECTOR": "Vertical 9:16 selector",
    "FLOW_DURATION_SELECTOR": "10-second duration selector",
    "FLOW_OUTPUT_COUNT_SELECTOR": "Single output selector",
    "FLOW_PROMPT_SELECTOR": "Prompt CSS selector",
    "FLOW_SUBMIT_SELECTOR": "Submit CSS selector",
    "FLOW_RESULT_SELECTOR": "Result CSS selector",
    "FLOW_DOWNLOAD_MENU_SELECTOR": "Asset options menu selector",
    "FLOW_DOWNLOAD_SELECTOR": "720p download selector",
    "FLOW_RENDER_TIMEOUT_MS": "Render timeout (ms)",
    "FLOW_DOWNLOAD_TIMEOUT_MS": "Download timeout (ms)",
    "YOUTUBE_CLIENT_SECRETS_FILE": "YouTube OAuth client JSON",
    "YOUTUBE_TOKEN_FILE": "YouTube OAuth token",
    "YOUTUBE_MADE_FOR_KIDS": "YouTube Audience: Yes, it's made for kids (true/false)",
    "TIKTOK_ACCESS_TOKEN": "TikTok access token",
    "TIKTOK_PRIVACY_LEVEL": "TikTok privacy level",
    "FB_PAGE_ID": "Facebook Page ID",
    "FB_PAGE_ACCESS_TOKEN": "Facebook Page access token",
    "LOG_LEVEL": "Log level",
}
DEFAULT_FIELD_VALUES = {
    "GEMINI_FALLBACK_MODELS": "gemini-flash-latest,gemini-flash-lite-latest,gemini-3.5-flash-lite,gemini-2.5-pro",
    "CHARACTER_1_TAG": "@khi",
    "CHARACTER_1_VOICE": "@Voice_Khi",
    "CHARACTER_2_TAG": "@nguoi",
    "CHARACTER_2_VOICE": "@Voice_Nguoi",
    "YOUTUBE_MADE_FOR_KIDS": "true",
}

def _get_or_create_secret_key() -> str:
    env_secret = os.getenv("DASHBOARD_SECRET_KEY")
    if env_secret and env_secret.strip():
        return env_secret.strip()
    secret_file = STORAGE_DIR / ".secret_key"
    if secret_file.is_file():
        try:
            cached = secret_file.read_text(encoding="utf-8").strip()
            if len(cached) >= 32:
                return cached
        except OSError:
            pass
    new_key = secrets.token_urlsafe(32)
    try:
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(new_key, encoding="utf-8")
    except OSError:
        pass
    return new_key


app = Flask(__name__)
app.secret_key = _get_or_create_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=False,
    MAX_CONTENT_LENGTH=512 * 1024 * 1024,
)

_process_lock = threading.Lock()
_managed_process: subprocess.Popen[bytes] | None = None
_managed_mode: str | None = None
_managed_started_at: float | None = None
_telegram_listener_process: subprocess.Popen[bytes] | None = None
_csrf_session_key = "_dashboard_csrf"


@dataclass(frozen=True)
class DashboardField:
    key: str
    label: str
    value: str
    secret: bool


def _read_environment() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    return {
        key: str(value)
        for key, value in dotenv_values(ENV_FILE).items()
        if value is not None
    }


def _character_defaults() -> dict[str, str]:
    values = _read_environment()
    return {
        key: (values.get(key, "").strip() or default)
        for key, default in DEFAULT_FIELD_VALUES.items()
    }


def _save_character_overrides_from_form() -> None:
    char_keys = (
        "CHARACTER_1_TAG",
        "CHARACTER_1_VOICE",
        "CHARACTER_2_TAG",
        "CHARACTER_2_VOICE",
    )
    updates: dict[str, str] = {}
    for key in char_keys:
        if key in request.form:
            submitted = request.form.get(key, "").strip()
            if submitted:
                updates[key] = submitted
    if updates:
        _save_environment(updates)


def _field_groups() -> list[dict[str, Any]]:
    values = _read_environment()
    groups: list[dict[str, Any]] = []
    for group_name, description, keys in ENV_GROUPS:
        fields = [
            DashboardField(
                key=key,
                label=FIELD_LABELS[key],
                value=""
                if key in SECRET_KEYS
                else (values.get(key, "") or DEFAULT_FIELD_VALUES.get(key, "")),
                secret=key in SECRET_KEYS,
            )
            for key in keys
        ]
        groups.append(
            {"name": group_name, "description": description, "fields": fields}
        )
    return groups


def _quoted_env_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("Environment values cannot contain line breaks.")
    return json.dumps(value, ensure_ascii=False)


def _save_environment(updates: dict[str, str]) -> None:
    current_text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    lines = current_text.splitlines()
    keys_to_update = {key: _quoted_env_value(value) for key, value in updates.items()}
    seen: set[str] = set()
    output: list[str] = []
    key_pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

    for line in lines:
        match = key_pattern.match(line)
        if match and match.group(1) in keys_to_update:
            key = match.group(1)
            output.append(f"{key}={keys_to_update[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in keys_to_update.items():
        if key not in seen:
            output.append(f"{key}={value}")

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ENV_FILE.with_name(f".{ENV_FILE.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
        os.replace(temporary, ENV_FILE)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _resolved_setting_path(value: str, default: str) -> Path:
    configured = Path(value or default).expanduser()
    return configured if configured.is_absolute() else ROOT_DIR / configured


def _has_google_session_cookies(auth_file: Path) -> bool:
    if not auth_file.is_file():
        return False
    try:
        data = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        return True
    cookie_names = {
        c.get("name") for c in cookies if isinstance(c, dict) and c.get("name")
    }
    return bool(
        cookie_names
        & {"SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"}
    )


def _flow_status() -> dict[str, Any]:
    values = _read_environment()
    auth_file = _resolved_setting_path(values.get("FLOW_AUTH_FILE", ""), "auth.json")
    project_url = values.get("FLOW_PROJECT_URL", "").strip()
    try:
        parsed_url = urlparse(project_url)
        valid_project_url = parsed_url.scheme == "https" and bool(parsed_url.netloc)
    except ValueError:
        valid_project_url = False
    missing_selectors = [
        key
        for key in FLOW_SELECTOR_KEYS
        if key not in IGNORED_LEGACY_FLOW_SELECTOR_KEYS
        and not values.get(key, "").strip()
    ]
    auth_exists = auth_file.is_file()
    auth_has_session = _has_google_session_cookies(auth_file)
    configured = valid_project_url and auth_exists and not missing_selectors
    modified = (
        datetime.fromtimestamp(auth_file.stat().st_mtime).astimezone().strftime(
            "%Y-%m-%d %H:%M"
        )
        if auth_exists
        else None
    )
    if not auth_exists:
        auth_note = "Chưa có phiên đăng nhập Playwright. Bấm 'Mở trình duyệt đăng nhập Flow' để lưu."
    elif not auth_has_session:
        auth_note = (
            "⚠️ File auth.json hiện chưa có cookie đăng nhập Google (chỉ có cookie khách). "
            "Hãy bấm 'Mở trình duyệt đăng nhập Flow' để đăng nhập lại."
        )
    else:
        auth_note = (
            "Đã có cookie đăng nhập Google trong auth.json; runner sẽ dùng phiên này "
            "khi mở project bằng Playwright."
        )
    return {
        "configured": configured,
        "auth_exists": auth_exists,
        "auth_has_session": auth_has_session,
        "auth_path": str(auth_file),
        "auth_modified": modified,
        "project_configured": valid_project_url,
        "missing_selectors": missing_selectors,
        "auth_note": auth_note,
        "note": "Flow không cung cấp health API ổn định để kiểm tra render trực tiếp.",
    }


def _load_queue() -> list[dict[str, Any]]:
    queue_file = STORAGE_DIR / "queue_approved.json"
    if not queue_file.exists():
        return []
    try:
        raw = queue_file.read_text(encoding="utf-8").strip()
        if not raw:
            queue_file.write_text("[]\n", encoding="utf-8")
            return []
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Could not read approved queue.")
        return []
    return data if isinstance(data, list) else []


def _load_drafts() -> list[dict[str, Any]]:
    drafts_file = STORAGE_DIR / "drafts.json"
    if not drafts_file.exists():
        return []
    try:
        raw = drafts_file.read_text(encoding="utf-8").strip()
        if not raw:
            drafts_file.write_text("[]\n", encoding="utf-8")
            return []
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Could not read idea drafts.")
        return []
    return data if isinstance(data, list) else []


def _load_reports() -> list[dict[str, Any]]:
    reports_file = STORAGE_DIR / "publish_reports.json"
    if not reports_file.exists():
        return []
    try:
        raw = reports_file.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Could not read publish reports.")
        return []
    reports = data if isinstance(data, list) else []
    for report in reports:
        statuses = report.get("statuses", {})
        for result in statuses.values():
            if isinstance(result, dict):
                for key in ("error", "message"):
                    if isinstance(result.get(key), str):
                        result[key] = _redact_logs(result[key])
    return reports[:30]


def _load_video_metadata() -> dict[str, dict[str, Any]]:
    meta_file = STORAGE_DIR / "video_metadata.json"
    if not meta_file.exists():
        return {}
    try:
        raw = meta_file.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_video_metadata_entry(
    video_name: str,
    social_posts: dict[str, Any],
    title: str = "",
) -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    meta_file = STORAGE_DIR / "video_metadata.json"
    current = _load_video_metadata()
    current[video_name] = {
        "video_name": video_name,
        "title": title,
        "social_posts": {
            k: v
            for k, v in social_posts.items()
            if k in ("tiktok", "facebook", "youtube") and isinstance(v, dict)
        },
    }
    write_json_atomic(meta_file, current)


def _resolve_social_posts_for_video(
    video_name: str,
    fallback_stem: str,
    video_meta: dict[str, dict[str, Any]],
    drafts: list[dict[str, Any]],
) -> tuple[str, dict[str, dict[str, str]] | None]:
    meta_entry = video_meta.get(video_name)
    if isinstance(meta_entry, dict) and isinstance(meta_entry.get("social_posts"), dict):
        resolved_title = str(meta_entry.get("title") or fallback_stem)
        return (
            resolved_title,
            _normalize_social_posts(meta_entry["social_posts"], fallback_title=resolved_title),
        )
    for draft in drafts:
        if not isinstance(draft, dict):
            continue
        draft_id = str(draft.get("id") or "")
        idea = draft.get("idea") if isinstance(draft.get("idea"), dict) else {}
        if (
            draft.get("video_name") == video_name
            or (
                draft_id
                and (
                    video_name.startswith(f"{draft_id}-")
                    or f"-{draft_id}-" in video_name
                    or video_name.startswith(draft_id)
                )
            )
        ):
            resolved_title = str(idea.get("title") or fallback_stem)
            return (
                resolved_title,
                _normalize_social_posts(
                    idea.get("social_posts"),
                    fallback_title=resolved_title,
                    scenes=idea.get("scenes"),
                ),
            )
    return fallback_stem, None


def _video_inventory() -> list[dict[str, Any]]:
    if not DOWNLOADS_DIR.exists():
        return []
    video_meta = _load_video_metadata()
    drafts = _load_drafts()
    videos: list[dict[str, Any]] = []
    for path in sorted(
        DOWNLOADS_DIR.glob("*.mp4"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        try:
            stat = path.stat()
        except OSError:
            continue
        resolved_title, social_posts = _resolve_social_posts_for_video(
            path.name, path.stem, video_meta, drafts
        )
        videos.append(
            {
                "name": path.name,
                "title": resolved_title,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M"),
                "social_posts": social_posts,
            }
        )
    return videos[:50]


def _cleanup_orphan_workers() -> None:
    if app.config.get("TESTING"):
        return
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*short-video-distributor*main.py*' "
                    "-and $_.CommandLine -notlike '*--telegram-poll*' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def _start_telegram_listener() -> bool:
    global _telegram_listener_process
    if _telegram_listener_process is not None:
        if _telegram_listener_process.poll() is None:
            return True
        _telegram_listener_process = None

    if not ENV_FILE.is_file():
        LOGGER.warning(
            "Telegram listener not started because %s does not exist.", ENV_FILE
        )
        return False
    env_values = _read_environment()
    if not env_values.get("TELEGRAM_BOT_TOKEN") or not env_values.get("TELEGRAM_CHAT_ID"):
        LOGGER.warning(
            "Telegram listener not started: configure TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID in the dashboard settings."
        )
        return False

    try:
        with FileLock(str(TELEGRAM_POLL_LOCK_FILE), timeout=0):
            pass
    except Timeout:
        LOGGER.info("Telegram listener is already running in another process.")
        return True

    executable = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if not executable.is_file():
        executable = Path(sys.executable)
    env = os.environ.copy()
    env.update(env_values)
    env["PYTHONUNBUFFERED"] = "1"
    try:
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("ab", buffering=0) as log_file:
            _telegram_listener_process = subprocess.Popen(
                [
                    str(executable),
                    str(ROOT_DIR / "main.py"),
                    "--telegram-poll",
                ],
                cwd=ROOT_DIR,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except OSError:
        _telegram_listener_process = None
        LOGGER.exception("Could not start the Telegram listener.")
        return False

    time.sleep(0.5)
    if _telegram_listener_process.poll() is not None:
        LOGGER.error(
            "Telegram listener exited during startup with code %s. See %s.",
            _telegram_listener_process.returncode,
            LOG_FILE,
        )
        _telegram_listener_process = None
        return False
    LOGGER.info(
        "Telegram listener started with PID %s.",
        _telegram_listener_process.pid,
    )
    return True


def _stop_telegram_listener() -> None:
    global _telegram_listener_process
    process = _telegram_listener_process
    _telegram_listener_process = None
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    except OSError:
        LOGGER.exception("Could not stop the Telegram listener.")


def _process_status() -> dict[str, Any]:
    global _managed_process, _managed_mode, _managed_started_at
    with _process_lock:
        running = _managed_process is not None and _managed_process.poll() is None
        pid = _managed_process.pid if running and _managed_process else None
        return_code = (
            _managed_process.poll()
            if _managed_process is not None and not running
            else None
        )
        started = (
            datetime.fromtimestamp(_managed_started_at)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
            if _managed_started_at is not None
            else None
        )
        if not running and _managed_process is not None:
            _managed_process = None
            _managed_mode = None
            _managed_started_at = None
        return {
            "running": running,
            "mode": _managed_mode,
            "pid": pid,
            "started": started,
            "last_exit_code": return_code,
        }


def _workflow_status() -> dict[str, Any]:
    if not FLOW_STATUS_FILE.is_file():
        return {
            "running": False,
            "current_node": None,
            "completed_nodes": [],
            "error": None,
        }
    try:
        status = json.loads(FLOW_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Could not read workflow status.")
        return {
            "running": False,
            "current_node": None,
            "completed_nodes": [],
            "error": "Không đọc được trạng thái flow.",
        }
    if not isinstance(status, dict):
        LOGGER.error("Workflow status file does not contain a JSON object.")
        return {
            "running": False,
            "current_node": None,
            "completed_nodes": [],
            "error": "Trạng thái flow không hợp lệ.",
        }
    if isinstance(status.get("error"), str):
        status["error"] = _redact_logs(status["error"])
    return status


def _order_workflow_nodes(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not nodes or len(nodes) > 20:
        raise ValueError("Flow phải có từ 1 đến 20 node.")
    node_by_id: dict[str, dict[str, str]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("Dữ liệu node không hợp lệ.")
        node_id = node.get("id")
        node_type = node.get("type")
        if (
            not isinstance(node_id, str)
            or not node_id
            or len(node_id) > 100
            or node_id in node_by_id
            or not isinstance(node_type, str)
            or node_type not in FLOW_NODE_TYPES
        ):
            raise ValueError("Node bị thiếu ID hợp lệ hoặc có loại không hỗ trợ.")
        node_dict: dict[str, Any] = {"id": node_id, "type": node_type}
        for attr in ("schedule_mode", "schedule_datetime", "schedule_time", "interval_minutes"):
            if attr in node and node[attr] is not None:
                node_dict[attr] = node[attr]
        node_by_id[node_id] = node_dict

    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_by_id}
    indegree = {node_id: 0 for node_id in node_by_id}
    seen_edges: set[tuple[str, str]] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("Dữ liệu kết nối không hợp lệ.")
        source = edge.get("from")
        target = edge.get("to")
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in node_by_id
            or target not in node_by_id
            or source == target
        ):
            raise ValueError("Kết nối flow không hợp lệ.")
        pair = (source, target)
        if pair in seen_edges:
            raise ValueError("Flow có kết nối bị lặp.")
        seen_edges.add(pair)
        adjacency[source].append(target)
        indegree[target] += 1

    ready = [node_id for node_id in node_by_id if indegree[node_id] == 0]
    ordered: list[dict[str, str]] = []
    while ready:
        node_id = ready.pop(0)
        ordered.append(node_by_id[node_id])
        for target in adjacency[node_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if len(ordered) != len(nodes):
        raise ValueError("Flow có vòng lặp; hãy bỏ kết nối tạo thành vòng.")

    ALLOWED_AFTER_LISTENER = {
        "approval-video",
        "approval-publish",
        "process-queue",
        "automation",
    }
    for index, node in enumerate(ordered):
        if node["type"] in LONG_RUNNING_FLOW_NODES and index != len(ordered) - 1:
            trailing = [n["type"] for n in ordered[index + 1 :]]
            if any(t not in ALLOWED_AFTER_LISTENER for t in trailing):
                raise ValueError(
                    "Node 'Tạo ý tưởng' phải đặt ở đầu flow (trước các node Duyệt / Telegram / Sản xuất & đăng)."
                )
    return ordered


def _redact_logs(text: str) -> str:
    env_values = _read_environment()
    for key in SECRET_KEYS:
        raw_secret = env_values.get(key, "")
        for part in raw_secret.replace(";", ",").split(","):
            secret = part.strip()
            if len(secret) >= 6:
                text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(access_token|bot_token|api[_-]?key)(=|%3D|%3d)[^&\s\"']+",
        r"\1\2[REDACTED]",
        text,
    )
    text = re.sub(r"\bbot\d{5,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_BOT_TOKEN]", text)
    return text


def _read_log_tail(path: Path, max_bytes: int) -> str:
    if max_bytes <= 0 or not path.is_file():
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            read_size = min(size, max_bytes)
            handle.seek(size - read_size)
            raw = handle.read(read_size)
    except OSError:
        LOGGER.exception("Could not read workflow log file %s.", path)
        return ""
    raw = raw.replace(b"\x00", b"")
    text = raw.decode("utf-8", errors="replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def _recent_logs(max_bytes: int = MAX_LOG_VIEW_BYTES) -> str:
    current = _read_log_tail(LOG_FILE, max_bytes)
    remaining = max_bytes - len(current.encode("utf-8"))
    if remaining > 0 and LOG_BACKUP_FILE.is_file():
        previous = _read_log_tail(LOG_BACKUP_FILE, remaining)
        combined = (
            f"{previous.rstrip()}\n{current.lstrip()}"
            if previous and current
            else (previous or current)
        )
    else:
        combined = current
    return _redact_logs(combined.strip())


def _csrf_token() -> str:
    token = session.get(_csrf_session_key)
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session[_csrf_session_key] = token
    return token


def _launch_process(
    mode: str, extra_args: list[str] | None = None
) -> tuple[bool, str]:
    global _managed_process, _managed_mode, _managed_started_at
    if not ENV_FILE.is_file():
        return False, "Chưa có file .env. Hãy lưu cấu hình trước."

    with _process_lock:
        if _managed_process is not None and _managed_process.poll() is None:
            return False, "Đã có tiến trình đang chạy. Dừng tiến trình đó trước."

        _cleanup_orphan_workers()

        executable = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        if not executable.is_file():
            executable = Path(sys.executable)
        try:
            env = os.environ.copy()
            env.update(_read_environment())
            env["PYTHONUNBUFFERED"] = "1"
            command_args = [f"--{mode}", *(extra_args or [])]
            if mode in {
                "run-flow",
                "generate-ideas",
                "resend-drafts",
                "auto",
                "approval-video",
                "approval-publish",
            }:
                command_args.append("--external-telegram-listener")
            STORAGE_DIR.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
                LOG_FILE.replace(LOG_BACKUP_FILE)
            with LOG_FILE.open("ab", buffering=0) as log_file:
                _managed_process = subprocess.Popen(
                    [
                        str(executable),
                        str(ROOT_DIR / "main.py"),
                        *command_args,
                    ],
                    cwd=ROOT_DIR,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            _managed_mode = mode
            _managed_started_at = time.time()
        except OSError:
            _managed_process = None
            _managed_mode = None
            _managed_started_at = None
            LOGGER.exception("Could not start workflow process.")
            return False, "Không khởi động được workflow. Xem log hoặc kiểm tra .venv."
    return True, f"Đã khởi động workflow: --{mode}."


def _launch_flow_login_browser() -> tuple[bool, str]:
    global _managed_process, _managed_mode, _managed_started_at
    if not ENV_FILE.is_file():
        return False, "Chưa có file .env. Hãy lưu cấu hình trước."

    with _process_lock:
        if _managed_process is not None and _managed_process.poll() is None:
            try:
                _managed_process.terminate()
                _managed_process.wait(timeout=5)
            except Exception:
                try:
                    _managed_process.kill()
                except Exception:
                    pass
            _managed_process = None
            _managed_mode = None
            _managed_started_at = None

        _cleanup_orphan_workers()
        if FLOW_STATUS_FILE.is_file():
            try:
                status = json.loads(FLOW_STATUS_FILE.read_text(encoding="utf-8"))
                if isinstance(status, dict):
                    status["running"] = False
                    status["auth_error"] = False
                    status["error"] = None
                    FLOW_STATUS_FILE.write_text(
                        json.dumps(status, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
            except Exception:
                pass

        executable = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        if not executable.is_file():
            executable = Path(sys.executable)
        try:
            env = os.environ.copy()
            env.update(_read_environment())
            env["PYTHONUNBUFFERED"] = "1"
            STORAGE_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("ab", buffering=0) as log_file:
                _managed_process = subprocess.Popen(
                    [
                        str(executable),
                        str(ROOT_DIR / "scripts" / "save_flow_auth.py"),
                        "--auto-save",
                    ],
                    cwd=ROOT_DIR,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
            _managed_mode = "flow-login"
            _managed_started_at = time.time()
        except OSError:
            _managed_process = None
            _managed_mode = None
            _managed_started_at = None
            LOGGER.exception("Could not start Flow login browser.")
            return False, "Không mở được trình duyệt đăng nhập Flow."
    return (
        True,
        "Đã mở trình duyệt đăng nhập Google Flow. Hãy đăng nhập và mở vào Project để tự động lưu auth.json.",
    )


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(500)
def _handle_http_errors(error):
    if request.path.startswith("/api/") or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        desc = getattr(error, "description", str(error))
        code = getattr(error, "code", 500)
        return {"error": str(desc), "status": code}, code
    return error


@app.before_request
def _protect_local_dashboard() -> None:
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        if request.path.startswith("/api/"):
            return {"error": "Truy cập bị từ chối (chỉ cho phép localhost)."}, 403
        abort(403)
    host = request.host.split(":", 1)[0].strip("[]").lower()
    if host not in {"127.0.0.1", "localhost"}:
        if request.path.startswith("/api/"):
            return {"error": "Host truy cập không hợp lệ."}, 403
        abort(403)

    if request.method == "POST":
        supplied = request.form.get("csrf_token", "")
        expected = session.get(_csrf_session_key, "")
        if not expected or not hmac.compare_digest(supplied, expected):
            if request.path.startswith("/api/"):
                return {
                    "error": "Phiên làm việc hoặc CSRF token đã hết hạn. Hãy bấm F5 để tải lại trang rồi thử lại."
                }, 400
            abort(400, description="Invalid or missing CSRF token.")
        origin = request.headers.get("Origin")
        if origin:
            origin_host = urlparse(origin).hostname
            if origin_host not in {"127.0.0.1", "localhost"}:
                if request.path.startswith("/api/"):
                    return {"error": "Origin không hợp lệ."}, 403
                abort(403)


@app.get("/")
def dashboard() -> str:
    queue = _load_queue()
    videos = _video_inventory()
    return render_template(
        "dashboard.html",
        csrf_token=_csrf_token(),
        groups=_field_groups(),
        characters=_character_defaults(),
        flow=_flow_status(),
        queue=queue,
        drafts=_load_drafts(),
        reports=_load_reports(),
        videos=videos,
        process=_process_status(),
        workflow=_workflow_status(),
        logs=_recent_logs(),
    )


@app.post("/config")
def save_config():
    updates: dict[str, str] = {}
    for _, _, keys in ENV_GROUPS:
        for key in keys:
            if key not in request.form:
                continue
            submitted = request.form.get(key, "")
            if key in SECRET_KEYS and not submitted:
                continue
            updates[key] = submitted.strip()
    try:
        _save_environment(updates)
    except (OSError, ValueError):
        LOGGER.exception("Could not save dashboard configuration.")
        flash("Không lưu được cấu hình. Kiểm tra quyền ghi file .env.", "error")
    else:
        flash(
            "Đã lưu .env. Secret để trống được giữ nguyên; tiến trình đang chạy "
            "cần khởi động lại để nhận cấu hình mới.",
            "success",
        )
    settings_page = request.form.get("settings_page", "")
    valid_settings_pages = {
        "settings-" + name.lower().replace(" ", "-")
        for name, _, _ in ENV_GROUPS
    }
    redirect_target = url_for("dashboard")
    if settings_page in valid_settings_pages:
        redirect_target += f"#{settings_page}"
    return redirect(redirect_target)


@app.post("/logs/clear")
def clear_logs():
    with _process_lock:
        if _managed_process is not None and _managed_process.poll() is None:
            flash("Hãy dừng workflow trước khi xóa log.", "error")
            return redirect(f"{url_for('dashboard')}#logs")
        _cleanup_orphan_workers()
        try:
            for log_path in (LOG_FILE, LOG_BACKUP_FILE):
                log_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Could not clear dashboard logs.")
            flash("Không xóa được log. Kiểm tra quyền truy cập file.", "error")
        else:
            flash("Đã xóa log workflow.", "success")
    return redirect(f"{url_for('dashboard')}#logs")


@app.post("/process/<mode>")
def start_process(mode: str):
    allowed_modes = {
        "auto",
        "approval-video",
        "approval-publish",
        "generate-ideas",
        "process-queue",
        "produce-telegram",
        "resend-drafts",
        "telegram-poll",
    }
    if mode not in allowed_modes:
        abort(404)
    started, message = _launch_process(mode)
    flash(message, "success" if started else "error")
    return redirect(url_for("dashboard"))


@app.post("/api/flow/login")
def start_flow_login():
    started, message = _launch_flow_login_browser()
    if not started:
        return {"error": message, "process": _process_status()}, 409
    return {"message": message, "process": _process_status()}, 202


@app.post("/api/drafts/<draft_id>/approve")
def approve_saved_draft(draft_id: str):
    if not approve_draft(draft_id, allow_requeue=True):
        return {"error": "Không tìm thấy kịch bản đã lưu này."}, 404
    return {
        "message": f"Đã đưa kịch bản {draft_id} vào hàng đợi duyệt.",
        "drafts": _load_drafts(),
        "queue": _load_queue(),
    }, 200


@app.post("/api/drafts/<draft_id>/delete")
def delete_saved_draft(draft_id: str):
    if not delete_draft(draft_id):
        return {"error": "Không tìm thấy kịch bản này để xóa."}, 404
    return {
        "message": f"Đã xóa kịch bản {draft_id}.",
        "drafts": _load_drafts(),
        "queue": _load_queue(),
    }, 200


@app.post("/api/queue/<item_id>/delete")
def delete_queued_item(item_id: str):
    if not delete_queue_item(item_id):
        return {"error": "Không tìm thấy kịch bản này trong hàng đợi."}, 404
    return {
        "message": f"Đã xóa kịch bản {item_id} khỏi hàng đợi đã duyệt.",
        "drafts": _load_drafts(),
        "queue": _load_queue(),
    }, 200


@app.post("/api/queue/clear")
def clear_queued_items():
    count = clear_approved_queue()
    return {
        "message": f"Đã xóa toàn bộ {count} kịch bản khỏi hàng đợi đã duyệt.",
        "drafts": _load_drafts(),
        "queue": _load_queue(),
    }, 200


@app.post("/api/drafts/<draft_id>/produce-telegram")
def produce_saved_draft_telegram(draft_id: str):
    if not approve_draft(draft_id, allow_requeue=True):
        return {"error": "Không tìm thấy kịch bản đã lưu này."}, 404
    proc = _process_status()
    if proc.get("running") and proc.get("mode") in {
        "run-flow",
        "approval-video",
        "approval-publish",
        "auto",
        "generate-ideas",
        "resend-drafts",
    }:
        return {
            "message": f"Đã duyệt kịch bản {draft_id} trực tiếp trên Web! Tiến trình đang chạy sẽ tự động tạo video trong vài giây tới.",
            "process": proc,
            "drafts": _load_drafts(),
            "queue": _load_queue(),
        }, 202
    started, message = _launch_process("produce-telegram")
    if not started:
        return {"error": message, "process": _process_status()}, 409
    return {
        "message": f"Đã duyệt kịch bản {draft_id} và bắt đầu tạo video gửi Telegram.",
        "process": _process_status(),
        "drafts": _load_drafts(),
        "queue": _load_queue(),
    }, 202


@app.post("/api/drafts/<draft_id>/produce-publish")
def produce_saved_draft_publish(draft_id: str):
    if not approve_draft(draft_id, allow_requeue=True):
        return {"error": "Không tìm thấy kịch bản đã lưu này."}, 404
    proc = _process_status()
    if proc.get("running") and proc.get("mode") in {
        "run-flow",
        "approval-video",
        "approval-publish",
        "auto",
        "generate-ideas",
        "resend-drafts",
    }:
        return {
            "message": f"Đã duyệt kịch bản {draft_id} trực tiếp trên Web! Tiến trình đang chạy sẽ tự động xử lý hàng đợi.",
            "process": proc,
            "drafts": _load_drafts(),
            "queue": _load_queue(),
        }, 202
    started, message = _launch_process("process-queue")
    if not started:
        return {"error": message, "process": _process_status()}, 409
    return {
        "message": f"Đã duyệt kịch bản {draft_id} và bắt đầu tạo video & đăng MXH.",
        "process": _process_status(),
        "drafts": _load_drafts(),
        "queue": _load_queue(),
    }, 202


@app.post("/api/queue/produce-telegram")
def produce_queue_telegram():
    if not _load_queue():
        return {"error": "Hàng đợi đã duyệt đang trống. Hãy chọn duyệt 1 ý tưởng trước."}, 400
    started, message = _launch_process("produce-telegram")
    if not started:
        return {"error": message, "process": _process_status()}, 409
    return {
        "message": "Đang tạo video từ kịch bản đã duyệt trong hàng đợi và gửi về Telegram.",
        "process": _process_status(),
    }, 202


@app.post("/api/drafts/resend-telegram")
def resend_drafts_to_telegram():
    if not _load_drafts():
        return {"error": "Chưa có ý tưởng nào được lưu trong storage/drafts.json."}, 400
    started, message = _launch_process("resend-drafts")
    if not started:
        return {"error": message, "process": _process_status()}, 409
    return {
        "message": "Đang gửi lại các ý tưởng đã lưu qua Telegram và chờ duyệt.",
        "process": _process_status(),
    }, 202


@app.post("/api/workflow/nodes/<node_type>/run")
def run_workflow_node(node_type: str):
    node_modes = {
        "generate-ideas": "generate-ideas",
        "telegram-listener": "telegram-poll",
        "process-queue": "process-queue",
        "approval-video": "approval-video",
        "approval-publish": "approval-publish",
        "automation": "auto",
    }
    mode = node_modes.get(node_type)
    if mode is None:
        abort(404)
    if node_type == "telegram-listener":
        return {
            "message": "Telegram listener đã chạy cùng dashboard; không cần khởi động tiến trình khác.",
            "process": _process_status(),
        }, 202

    extra_args: list[str] = []
    sched_mode = request.form.get("schedule_mode", "").strip()
    if sched_mode in {"interval", "datetime", "daily"}:
        extra_args.extend(["--schedule-mode", sched_mode])
    sched_dt = request.form.get("schedule_datetime", "").strip()
    if sched_dt:
        extra_args.extend(["--schedule-datetime", sched_dt])
    sched_tm = request.form.get("schedule_time", "").strip()
    if sched_tm:
        extra_args.extend(["--schedule-time", sched_tm])
    interval_raw = request.form.get("interval_minutes", "").strip()
    if interval_raw and interval_raw.isdigit():
        extra_args.extend(["--interval-minutes", str(int(interval_raw))])
    ideas_count_raw = request.form.get("ideas_count", "").strip()
    if ideas_count_raw and ideas_count_raw.isdigit():
        extra_args.extend(["--ideas-count", str(int(ideas_count_raw))])

    if extra_args:
        started, message = _launch_process(mode, extra_args=extra_args)
    else:
        started, message = _launch_process(mode)
    if not started:
        return {"error": message, "process": _process_status()}, 409
    return {"message": message, "process": _process_status()}, 202


@app.post("/api/workflow/run")
def run_workflow():
    try:
        nodes = json.loads(request.form.get("nodes", "[]"))
        edges = json.loads(request.form.get("edges", "[]"))
        scene_count = int(request.form.get("scene_count", "3"))
        duration_seconds = int(request.form.get("duration_seconds", "10"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"error": "Thông tin flow hoặc thông số tạo video không hợp lệ."}, 400
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return {"error": "Danh sách node hoặc kết nối không hợp lệ."}, 400
    try:
        ordered_nodes = _order_workflow_nodes(nodes, edges)
    except ValueError as error:
        return {"error": str(error)}, 400

    prompt = request.form.get("prompt", "").strip()
    if len(prompt) > 12_000:
        return {"error": "Prompt không được dài quá 12.000 ký tự."}, 400
    if not 1 <= scene_count <= 20:
        return {"error": "Số cảnh phải nằm trong khoảng 1 đến 20."}, 400
    if not 1 <= duration_seconds <= 180:
        return {"error": "Thời lượng phải nằm trong khoảng 1 đến 180 giây."}, 400

    try:
        _save_character_overrides_from_form()
    except (OSError, ValueError):
        return {"error": "Không lưu được thông tin nhân vật vào .env."}, 400

    FLOW_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    FLOW_STATUS_FILE.write_text(
        json.dumps(
            {
                "running": True,
                "current_node": None,
                "completed_nodes": [],
                "node_order": ordered_nodes,
                "error": None,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    extra_args = [
        "--flow-nodes-json",
        json.dumps(ordered_nodes, ensure_ascii=False),
        "--idea-prompt",
        prompt,
        "--scene-count",
        str(scene_count),
        "--duration-seconds",
        str(duration_seconds),
    ]
    try:
        ideas_cnt = int(request.form.get("ideas_count", "10"))
        if 1 <= ideas_cnt <= 25:
            extra_args.extend(["--ideas-count", str(ideas_cnt)])
    except (TypeError, ValueError):
        pass
    auto_node = next(
        (n for n in ordered_nodes if isinstance(n, dict) and n.get("type") == "automation"),
        None,
    )
    sched_mode = request.form.get("schedule_mode", "").strip()
    if not sched_mode and auto_node:
        sched_mode = str(auto_node.get("schedule_mode", "")).strip()
    if sched_mode in {"interval", "datetime", "daily"}:
        extra_args.extend(["--schedule-mode", sched_mode])

    sched_dt = request.form.get("schedule_datetime", "").strip()
    if not sched_dt and auto_node:
        sched_dt = str(auto_node.get("schedule_datetime", "")).strip()
    if sched_dt:
        extra_args.extend(["--schedule-datetime", sched_dt])

    sched_tm = request.form.get("schedule_time", "").strip()
    if not sched_tm and auto_node:
        sched_tm = str(auto_node.get("schedule_time", "")).strip()
    if sched_tm:
        extra_args.extend(["--schedule-time", sched_tm])

    interval_raw = request.form.get("interval_minutes", "").strip()
    if not interval_raw and auto_node and auto_node.get("interval_minutes"):
        interval_raw = str(auto_node["interval_minutes"]).strip()
    if interval_raw and interval_raw.isdigit():
        extra_args.extend(["--interval-minutes", str(int(interval_raw))])

    started, message = _launch_process("run-flow", extra_args)
    if not started:
        FLOW_STATUS_FILE.write_text(
            json.dumps(
                {
                    "running": False,
                    "current_node": None,
                    "completed_nodes": [],
                    "node_order": ordered_nodes,
                    "error": message,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return {"error": message, "process": _process_status()}, 409
    return {
        "message": message,
        "process": _process_status(),
        "workflow": _workflow_status(),
    }, 202


@app.post("/api/ideas/run")
def run_custom_ideas():
    prompt = request.form.get("prompt", "").strip()
    try:
        scene_count = int(request.form.get("scene_count", "3"))
        duration_seconds = int(request.form.get("duration_seconds", "10"))
        ideas_count = int(request.form.get("ideas_count", "10"))
    except ValueError:
        return {"error": "Số cảnh, thời lượng và số ý tưởng phải là số nguyên."}, 400

    if not prompt:
        return {"error": "Hãy nhập prompt mô tả ý tưởng video."}, 400
    if len(prompt) > 12_000:
        return {"error": "Prompt không được dài quá 12.000 ký tự."}, 400
    if not 1 <= scene_count <= 20:
        return {"error": "Số cảnh phải nằm trong khoảng 1 đến 20."}, 400
    if not 1 <= duration_seconds <= 180:
        return {"error": "Thời lượng phải nằm trong khoảng 1 đến 180 giây."}, 400
    if not 1 <= ideas_count <= 25:
        return {"error": "Số ý tưởng phải nằm trong khoảng 1 đến 25."}, 400

    try:
        _save_character_overrides_from_form()
    except (OSError, ValueError):
        return {"error": "Không lưu được thông tin nhân vật vào .env."}, 400

    extra_args = [
        "--idea-prompt",
        prompt,
        "--scene-count",
        str(scene_count),
        "--duration-seconds",
        str(duration_seconds),
        "--ideas-count",
        str(ideas_count),
    ]
    started, message = _launch_process("generate-ideas", extra_args)
    if not started:
        return {"error": message, "process": _process_status()}, 409
    return {"message": message, "process": _process_status()}, 202


@app.post("/stop")
def stop_process():
    global _managed_process, _managed_mode, _managed_started_at
    with _process_lock:
        process = _managed_process
        if process is None or process.poll() is not None:
            _managed_process = None
            _managed_mode = None
            _managed_started_at = None
            _cleanup_orphan_workers()
            flash("Đã dừng tiến trình (và dọn dẹp các worker chạy ngầm).", "error")
            return redirect(url_for("dashboard"))
        try:
            process.terminate()
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            LOGGER.exception("Could not stop workflow process.")
            flash("Không dừng được workflow.", "error")
            return redirect(url_for("dashboard"))
        _cleanup_orphan_workers()
        if _managed_mode == "run-flow" and FLOW_STATUS_FILE.is_file():
            try:
                status = json.loads(FLOW_STATUS_FILE.read_text(encoding="utf-8"))
                if isinstance(status, dict):
                    status["running"] = False
                    status["current_node"] = None
                    status["error"] = "Đã dừng theo yêu cầu."
                    FLOW_STATUS_FILE.write_text(
                        json.dumps(status, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
            except (OSError, json.JSONDecodeError):
                LOGGER.exception("Could not update workflow status after stop.")
        _managed_process = None
        _managed_mode = None
        _managed_started_at = None
    flash("Đã dừng workflow.", "success")
    return redirect(url_for("dashboard"))


ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def _sanitize_video_filename(original_name: str) -> str:
    raw_path = Path(original_name or "imported_video.mp4")
    ext = raw_path.suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise ValueError(
            "Định dạng file không hỗ trợ. Hãy chọn file video (.mp4, .mov, .webm, .mkv, .m4v)."
        )
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", raw_path.stem).strip(" ._")
    if not stem:
        stem = f"video_{int(time.time())}"
    stem = stem[:90]
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    candidate = f"{stem}.mp4"
    target = DOWNLOADS_DIR / candidate
    if target.exists():
        candidate = f"{stem}_{int(time.time())}.mp4"
    return candidate


def _save_imported_video(file_storage: Any = None, local_path: str = "") -> Path:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    if file_storage is not None and getattr(file_storage, "filename", ""):
        safe_name = _sanitize_video_filename(file_storage.filename)
        destination = (DOWNLOADS_DIR / safe_name).resolve()
        if destination.parent != DOWNLOADS_DIR.resolve():
            raise ValueError("Tên file video không hợp lệ.")
        file_storage.save(str(destination))
        if not destination.is_file() or destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise ValueError("File video tải lên bị rỗng.")
        return destination

    cleaned_local = (local_path or "").strip().strip('"').strip("'")
    if cleaned_local:
        source = Path(cleaned_local).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"Không tìm thấy file video tại đường dẫn: {source}")
        if source.stat().st_size == 0:
            raise ValueError("File video tại đường dẫn đã chọn bị rỗng.")
        safe_name = _sanitize_video_filename(source.name)
        destination = (DOWNLOADS_DIR / safe_name).resolve()
        if source.resolve() != destination:
            shutil.copy2(source, destination)
        return destination

    raise ValueError("Hãy chọn file video từ máy tính hoặc nhập đường dẫn file hợp lệ.")


def _append_publish_report(report: dict[str, Any]) -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    reports_file = STORAGE_DIR / "publish_reports.json"
    existing: list[dict[str, Any]] = []
    if reports_file.exists():
        try:
            raw = reports_file.read_text(encoding="utf-8").strip()
            if raw:
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = []
    existing.insert(0, report)
    write_json_atomic(reports_file, existing[:100])


@app.post("/api/videos/import")
def import_video_api():
    uploaded_file = request.files.get("video_file")
    local_path = request.form.get("local_path", "").strip()
    hint_text = request.form.get("hint_text", "").strip()
    auto_generate = request.form.get("auto_generate_captions", "1").strip() != "0"
    try:
        saved_path = _save_imported_video(uploaded_file, local_path)
    except (FileNotFoundError, ValueError, OSError) as error:
        return {"error": str(error)}, 400

    social_posts: dict[str, dict[str, str]] | None = None
    ai_generated = False
    ai_error: str | None = None
    resolved_title = saved_path.stem

    if auto_generate:
        try:
            generated = generate_social_posts_for_video(
                saved_path,
                video_title=saved_path.stem,
                hint_text=hint_text,
            )
            raw_gt = generated.get("general_title")
            if isinstance(raw_gt, dict):
                gen_title = raw_gt.get("title")
            elif isinstance(raw_gt, str):
                gen_title = raw_gt
            else:
                gen_title = None
            if gen_title:
                resolved_title = gen_title
            social_posts = {
                k: generated[k]
                for k in ("tiktok", "facebook", "youtube")
                if k in generated
            }
            _save_video_metadata_entry(saved_path.name, social_posts, title=resolved_title)
            ai_generated = True
        except Exception as error:
            LOGGER.warning("Could not auto-generate social captions via Gemini on import: %s", error)
            ai_error = str(error)
            social_posts = _normalize_social_posts(None, fallback_title=saved_path.stem)

    stat = saved_path.stat()
    video_info = {
        "name": saved_path.name,
        "title": resolved_title,
        "size_mb": round(stat.st_size / (1024 * 1024), 2),
        "modified": datetime.fromtimestamp(stat.st_mtime)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M"),
        "url": url_for("serve_video", filename=saved_path.name),
        "social_posts": social_posts,
    }
    msg = f"Đã import video '{saved_path.name}' vào thư viện."
    if ai_generated:
        msg += " ✨ Gemini AI đã tự động viết Caption & Hashtag riêng cho TikTok, Facebook và YouTube!"
    return {
        "message": msg,
        "video": video_info,
        "social_posts": social_posts,
        "ai_generated": ai_generated,
        "ai_error": ai_error,
        "videos": _video_inventory(),
    }, 201


@app.post("/api/videos/generate-captions")
def generate_video_captions_api():
    uploaded_file = request.files.get("video_file")
    selected_name = request.form.get("video_name", "").strip()
    custom_title = request.form.get("general_title", "").strip()
    hint_text = request.form.get("hint_text", "").strip()

    video_path: Path | None = None
    if uploaded_file is not None and getattr(uploaded_file, "filename", ""):
        try:
            video_path = _save_imported_video(uploaded_file, "")
            selected_name = video_path.name
        except (FileNotFoundError, ValueError, OSError) as error:
            return {"error": str(error)}, 400
    elif selected_name:
        if Path(selected_name).name == selected_name:
            candidate = (DOWNLOADS_DIR / selected_name).resolve()
            if candidate.parent == DOWNLOADS_DIR.resolve() and candidate.is_file():
                video_path = candidate

    fallback_title = custom_title or (video_path.stem if video_path else "Video ngắn hài hước")
    try:
        generated = generate_social_posts_for_video(
            video_path,
            video_title=fallback_title,
            hint_text=hint_text,
        )
    except Exception as error:
        return {"error": f"Không gọi được Gemini AI để tạo caption: {error}"}, 502

    raw_gt = generated.get("general_title")
    if isinstance(raw_gt, dict):
        resolved_title = raw_gt.get("title") or fallback_title
    elif isinstance(raw_gt, str) and raw_gt.strip():
        resolved_title = raw_gt.strip()
    else:
        resolved_title = fallback_title
    social_posts = {
        k: generated[k]
        for k in ("tiktok", "facebook", "youtube")
        if k in generated
    }
    if video_path is not None:
        _save_video_metadata_entry(video_path.name, social_posts, title=resolved_title)

    return {
        "message": "✨ Đã tạo Caption & Hashtag riêng cho TikTok, Facebook Reels và YouTube Shorts bằng Gemini AI!",
        "general_title": resolved_title,
        "social_posts": social_posts,
        "video_name": video_path.name if video_path else selected_name,
        "videos": _video_inventory(),
    }, 200


@app.post("/api/videos/publish")
def publish_video_multiplatform_api():
    uploaded_file = request.files.get("video_file")
    local_path = request.form.get("local_path", "").strip()
    selected_name = request.form.get("video_name", "").strip()

    video_path: Path | None = None
    if (uploaded_file is not None and getattr(uploaded_file, "filename", "")) or local_path:
        try:
            video_path = _save_imported_video(uploaded_file, local_path)
        except (FileNotFoundError, ValueError, OSError) as error:
            return {"error": str(error)}, 400
    elif selected_name:
        if Path(selected_name).name != selected_name:
            return {"error": "Tên file video không hợp lệ."}, 400
        candidate = (DOWNLOADS_DIR / selected_name).resolve()
        if candidate.parent != DOWNLOADS_DIR.resolve() or not candidate.is_file():
            return {"error": f"Không tìm thấy video '{selected_name}' trong storage/downloads."}, 404
        video_path = candidate
    else:
        return {
            "error": "Hãy chọn một video có sẵn hoặc tải lên (import) video từ máy tính trước khi đăng."
        }, 400

    def _is_checked(field_name: str) -> bool:
        val = str(request.form.get(field_name, "")).strip().lower()
        return val in {"1", "true", "on", "yes"}

    tiktok_enabled = _is_checked("tiktok_enabled")
    facebook_enabled = _is_checked("facebook_enabled")
    youtube_enabled = _is_checked("youtube_enabled")

    if not (tiktok_enabled or facebook_enabled or youtube_enabled):
        return {
            "error": "Hãy chọn ít nhất 1 nền tảng (TikTok, Facebook hoặc YouTube) để đăng video."
        }, 400

    tiktok_title = request.form.get("tiktok_title", "").strip()
    tiktok_caption = request.form.get("tiktok_caption", "").strip()
    tiktok_hashtags = request.form.get("tiktok_hashtags", "").strip()

    facebook_title = request.form.get("facebook_title", "").strip()
    facebook_caption = request.form.get("facebook_caption", "").strip()
    facebook_hashtags = request.form.get("facebook_hashtags", "").strip()

    youtube_title = request.form.get("youtube_title", "").strip()
    youtube_caption = request.form.get("youtube_caption", "").strip()
    youtube_hashtags = request.form.get("youtube_hashtags", "").strip()
    raw_yt_kids = request.form.get("youtube_made_for_kids")
    if raw_yt_kids is None or not str(raw_yt_kids).strip():
        raw_yt_kids = _read_environment().get("YOUTUBE_MADE_FOR_KIDS", "true")
    youtube_made_for_kids = str(raw_yt_kids).strip().lower() not in {"0", "false", "no", "off"}

    general_title = (
        request.form.get("general_title", "").strip()
        or youtube_title
        or tiktok_title
        or facebook_title
        or (tiktok_caption.splitlines()[0].strip() if tiktok_caption else "")
        or (facebook_caption.splitlines()[0].strip() if facebook_caption else "")
        or (youtube_caption.splitlines()[0].strip() if youtube_caption else "")
        or video_path.stem
    )[:120]

    platform_configs: dict[str, dict[str, Any]] = {
        "tiktok": {
            "enabled": tiktok_enabled,
            "title": tiktok_title or general_title,
            "caption": tiktok_caption,
            "hashtags": parse_hashtags(tiktok_hashtags),
        },
        "facebook": {
            "enabled": facebook_enabled,
            "title": facebook_title,
            "caption": facebook_caption,
            "hashtags": parse_hashtags(facebook_hashtags),
        },
        "youtube": {
            "enabled": youtube_enabled,
            "title": build_youtube_shorts_title(
                youtube_title or general_title,
                youtube_hashtags,
                max_length=100,
            ),
            "caption": youtube_caption,
            "hashtags": parse_hashtags(youtube_hashtags),
            "made_for_kids": youtube_made_for_kids,
        },
    }

    try:
        statuses = publish_to_social(
            str(video_path),
            title=general_title,
            description="",
            tags=[],
            platform_configs=platform_configs,
        )
    except (FileNotFoundError, ValueError, OSError) as error:
        return {"error": str(error)}, 400

    published_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    report = {
        "id": f"pub_{int(time.time())}_{secrets.token_hex(3)}",
        "title": general_title,
        "video_path": str(video_path),
        "video_name": video_path.name,
        "published_at": published_at,
        "platform_configs": {
            platform: {
                "enabled": cfg["enabled"],
                "title": cfg["title"],
                "caption": cfg["caption"],
                "hashtags": [f"#{t}" for t in cfg["hashtags"]],
            }
            for platform, cfg in platform_configs.items()
        },
        "statuses": statuses,
    }
    _append_publish_report(report)

    telegram_notified = False
    telegram_error: str | None = None
    if _is_checked("notify_telegram"):
        try:
            asyncio.run(send_final_report(str(video_path), general_title, statuses))
            telegram_notified = True
        except Exception as error:
            LOGGER.warning("Could not send Telegram publish report: %s", error)
            telegram_error = str(error)

    enabled_platforms = [
        label
        for key, label in (
            ("tiktok", "TikTok"),
            ("facebook", "Facebook Reels"),
            ("youtube", "YouTube Shorts"),
        )
        if platform_configs[key]["enabled"]
    ]
    success_count = sum(
        1
        for key in ("tiktok", "facebook", "youtube")
        if platform_configs[key]["enabled"] and statuses.get(key, {}).get("success")
    )

    summary_msg = (
        f"Đã xử lý đăng video '{video_path.name}' lên {', '.join(enabled_platforms)}: "
        f"thành công {success_count}/{len(enabled_platforms)} nền tảng."
    )
    return {
        "message": summary_msg,
        "report": report,
        "reports": _load_reports(),
        "videos": _video_inventory(),
        "telegram_notified": telegram_notified,
        "telegram_error": telegram_error,
    }, 200


@app.get("/videos/<path:filename>")
def serve_video(filename: str):
    if Path(filename).name != filename or not filename.lower().endswith(".mp4"):
        abort(404)
    path = (DOWNLOADS_DIR / filename).resolve()
    if path.parent != DOWNLOADS_DIR.resolve() or not path.is_file():
        abort(404)
    return send_file(path, conditional=True)


@app.get("/api/logs")
def logs_api():
    return {
        "logs": _recent_logs(),
        "process": _process_status(),
    }


@app.get("/api/status")
def status_api():
    drafts = _load_drafts()
    queue = _load_queue()
    return {
        "flow": _flow_status(),
        "queue_count": len(queue),
        "draft_count": len(drafts),
        "queue": queue,
        "drafts": drafts,
        "report_count": len(_load_reports()),
        "videos": _video_inventory(),
        "process": _process_status(),
        "workflow": _workflow_status(),
        "logs": _recent_logs(),
    }


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    port = int(os.getenv("DASHBOARD_PORT", "5000"))
    _start_telegram_listener()
    try:
        app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
    finally:
        _stop_telegram_listener()


if __name__ == "__main__":
    main()
