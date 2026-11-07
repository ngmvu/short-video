from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from core.config import get_setting, setting_path

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
TIKTOK_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
FACEBOOK_API_VERSION = "v19.0"
REQUEST_TIMEOUT = 60


def parse_hashtags(raw: str | list[str] | None) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        tokens: list[str] = []
        for item in raw:
            if isinstance(item, str):
                tokens.extend(item.replace(",", " ").replace("\n", " ").split())
    elif isinstance(raw, str):
        tokens = raw.replace(",", " ").replace("\n", " ").split()
    else:
        return []

    cleaned: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        tag = token.strip().lstrip("#").strip()
        if not tag:
            continue
        lower = tag.lower()
        if lower not in seen:
            seen.add(lower)
            cleaned.append(tag)
    return cleaned


def build_youtube_shorts_title(
    title: str,
    tags: str | list[str] | None = None,
    max_length: int = 100,
) -> str:
    raw_words = (title or "").strip().split()
    base_words: list[str] = []
    inline_tags: list[str] = []
    for word in raw_words:
        if word.startswith("#") and len(word) > 1:
            inline_tags.append(word.lstrip("#"))
        else:
            base_words.append(word)

    base_title = " ".join(base_words).strip() or (title or "Video ngắn").strip()
    all_tags = parse_hashtags([*inline_tags, *(parse_hashtags(tags))])
    if not any(t.lower() == "shorts" for t in all_tags):
        all_tags.insert(0, "Shorts")
    else:
        shorts_tag = next(t for t in all_tags if t.lower() == "shorts")
        all_tags = ["Shorts" if shorts_tag.lower() == "shorts" else shorts_tag] + [
            t for t in all_tags if t.lower() != "shorts"
        ]

    # Ensure key tags (#Shorts, #dunglakhi, #trending, etc.) fit within max_length (100 chars)
    priority_lowers = {"shorts", "dunglakhi", "trending"}
    must_keep = [f"#{t.replace(' ', '')}" for t in all_tags if t.lower() in priority_lowers]
    if not must_keep:
        must_keep = [f"#{all_tags[0].replace(' ', '')}"]
    min_tags_str = " ".join(must_keep[:3])

    max_base_len = max(20, max_length - len(min_tags_str) - 1)
    if len(base_title) > max_base_len:
        trimmed = base_title[:max_base_len].rsplit(" ", 1)[0].strip()
        base_title = trimmed if len(trimmed) >= 15 else base_title[:max_base_len].strip()

    full_candidates = [f"#{t.replace(' ', '')}" for t in all_tags if t.strip()]
    # First check if all tags fit in original order
    all_joined = f"{base_title} {' '.join(full_candidates)}".strip()
    if len(all_joined) <= max_length:
        return all_joined

    # Otherwise, reserve space for priority tags (like #trending at the end) while adding tags in order
    selected_tags: list[str] = []
    reserved_tail = [
        tag for tag in full_candidates if tag.lstrip("#").lower() in {"trending"}
    ]
    reserved_len = sum(len(t) + 1 for t in reserved_tail)

    current_len = len(base_title)
    for tag in full_candidates:
        if tag in reserved_tail:
            continue
        if current_len + 1 + len(tag) + reserved_len <= max_length:
            selected_tags.append(tag)
            current_len += 1 + len(tag)

    for tag in reserved_tail:
        if tag not in selected_tags and current_len + 1 + len(tag) <= max_length:
            selected_tags.append(tag)
            current_len += 1 + len(tag)

    if not selected_tags and full_candidates:
        selected_tags.append(full_candidates[0])

    result = f"{base_title} {' '.join(selected_tags)}".strip()
    return result[:max_length].rstrip()


def _append_tags(description: str, tags: list[str]) -> str:
    clean_tags = parse_hashtags(tags)
    existing_lower = description.lower()
    missing_hashtags = [
        f"#{tag.replace(' ', '')}"
        for tag in clean_tags
        if tag.strip() and f"#{tag.replace(' ', '').lower()}" not in existing_lower
    ]
    hashtags_str = " ".join(missing_hashtags)
    if not hashtags_str:
        return description.strip()
    if not description.strip():
        return hashtags_str
    return f"{description.rstrip()}\n\n{hashtags_str}".strip()


def _resolve_youtube_made_for_kids(explicit: bool | str | None = None) -> bool:
    if isinstance(explicit, bool):
        return explicit
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower() in {"1", "true", "yes", "on"}
    try:
        raw = get_setting("YOUTUBE_MADE_FOR_KIDS", "false")
    except (KeyError, ValueError):
        return False
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _youtube_upload(
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    made_for_kids: bool | None = None,
) -> dict[str, Any]:
    token_file = setting_path("YOUTUBE_TOKEN_FILE", "yt_token.json")
    if not token_file.is_file():
        raise FileNotFoundError(f"YouTube token file not found: {token_file}")
    credentials = Credentials.from_authorized_user_file(
        str(token_file), scopes=[YOUTUBE_SCOPE]
    )
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_file.write_text(credentials.to_json(), encoding="utf-8")
    if not credentials.valid:
        raise GoogleAuthError("YouTube credentials are invalid or expired.")

    clean_tags = parse_hashtags(tags)
    yt_title = build_youtube_shorts_title(title or video_path.stem, clean_tags, max_length=100)
    yt_description = _append_tags(description, [*clean_tags, "Shorts"])
    self_declared_kids = _resolve_youtube_made_for_kids(made_for_kids)
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": yt_title,
                "description": yt_description,
                "tags": list(dict.fromkeys([*clean_tags, "Shorts"])),
                "categoryId": "23",
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": self_declared_kids,
            },
        },
        media_body=MediaFileUpload(
            str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True
        ),
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    video_id = response.get("id")
    if not video_id:
        raise RuntimeError("YouTube upload completed without returning a video ID.")
    return {
        "success": True,
        "id": video_id,
        "url": f"https://www.youtube.com/shorts/{video_id}",
    }


def _tiktok_upload(video_path: Path, title: str, description: str) -> dict[str, Any]:
    access_token = get_setting("TIKTOK_ACCESS_TOKEN")
    file_size = video_path.stat().st_size
    chunk_size = max(5 * 1024 * 1024, min(file_size, 64 * 1024 * 1024))
    chunk_count = math.ceil(file_size / chunk_size)
    initialized = requests.post(
        TIKTOK_INIT_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={
            "post_info": {
                "title": (title or description or video_path.stem)[:150],
                "description": description[:2200],
                "privacy_level": get_setting(
                    "TIKTOK_PRIVACY_LEVEL", "PUBLIC_TO_EVERYONE"
                ),
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": chunk_size,
                "total_chunk_count": chunk_count,
            },
        },
        timeout=REQUEST_TIMEOUT,
    )
    initialized.raise_for_status()
    payload = initialized.json()
    if payload.get("error", {}).get("code") != "ok":
        raise RuntimeError(f"TikTok initialization failed: {payload.get('error')}")
    data = payload.get("data", {})
    upload_url = data.get("upload_url")
    publish_id = data.get("publish_id")
    if not upload_url or not publish_id:
        raise RuntimeError("TikTok response is missing upload_url or publish_id.")

    with video_path.open("rb") as video:
        offset = 0
        while offset < file_size:
            chunk = video.read(chunk_size)
            end = offset + len(chunk) - 1
            uploaded = requests.put(
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{file_size}",
                },
                data=chunk,
                timeout=REQUEST_TIMEOUT,
            )
            uploaded.raise_for_status()
            offset = end + 1
    return {
        "success": True,
        "id": publish_id,
        "url": None,
        "message": f"Video submitted; processing is pending (publish ID: {publish_id}).",
    }


def _facebook_upload(
    video_path: Path, title: str, description: str
) -> dict[str, Any]:
    page_id = get_setting("FB_PAGE_ID")
    access_token = get_setting("FB_PAGE_ACCESS_TOKEN")
    endpoint = f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/{page_id}/video_reels"
    started = requests.post(
        endpoint,
        data={"upload_phase": "start", "access_token": access_token},
        timeout=REQUEST_TIMEOUT,
    )
    started.raise_for_status()
    start_data = started.json()
    video_id = start_data.get("video_id")
    upload_url = start_data.get("upload_url")
    if not video_id or not upload_url:
        raise RuntimeError(f"Facebook start response is incomplete: {start_data}")

    file_size = video_path.stat().st_size
    with video_path.open("rb") as video:
        uploaded = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {access_token}",
                "offset": "0",
                "file_size": str(file_size),
                "Content-Type": "application/octet-stream",
            },
            data=video,
            timeout=max(REQUEST_TIMEOUT, 300),
        )
    uploaded.raise_for_status()
    clean_title = title.strip()
    clean_desc = description.strip()
    if clean_title and clean_title != clean_desc and not clean_desc.startswith(clean_title):
        fb_description = f"{clean_title}\n\n{clean_desc}".strip()
    else:
        fb_description = clean_desc or clean_title
    finish = requests.post(
        endpoint,
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": fb_description,
            "access_token": access_token,
        },
        timeout=REQUEST_TIMEOUT,
    )
    finish.raise_for_status()
    finish_data = finish.json()
    if finish_data.get("success") is False:
        raise RuntimeError(f"Facebook publish failed: {finish_data}")

    permalink = None
    try:
        lookup = requests.get(
            f"https://graph.facebook.com/{FACEBOOK_API_VERSION}/{video_id}",
            params={"fields": "permalink_url", "access_token": access_token},
            timeout=REQUEST_TIMEOUT,
        )
        lookup.raise_for_status()
        permalink = lookup.json().get("permalink_url")
    except requests.RequestException as error:
        return {
            "success": True,
            "id": video_id,
            "url": None,
            "message": f"Published (video ID: {video_id}); permalink lookup failed: {error}",
        }
    return {"success": True, "id": video_id, "url": permalink}


def _attempt_upload(uploader: Any, *args: Any) -> dict[str, Any]:
    try:
        return uploader(*args)
    except (
        GoogleAuthError,
        HttpError,
        OSError,
        requests.RequestException,
        ValueError,
        RuntimeError,
    ) as error:
        return {"success": False, "error": str(error)}


def publish_to_social(
    video_path: str,
    title: str = "",
    description: str = "",
    tags: list[str] | None = None,
    platform_configs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Video file does not exist: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"Video file is empty: {path}")

    default_tags = parse_hashtags(tags or [])
    if not platform_configs:
        tagged_description = _append_tags(description, default_tags)
        return {
            "youtube": _attempt_upload(
                _youtube_upload, path, title, description, default_tags
            ),
            "tiktok": _attempt_upload(
                _tiktok_upload, path, title, tagged_description
            ),
            "facebook": _attempt_upload(
                _facebook_upload, path, title, tagged_description
            ),
        }

    results: dict[str, dict[str, Any]] = {}

    # YouTube Shorts
    yt_cfg = platform_configs.get("youtube", {})
    if yt_cfg.get("enabled", True):
        yt_caption = str(yt_cfg.get("caption", description) or "").strip()
        yt_title = (
            str(yt_cfg.get("title", "") or "").strip()
            or (yt_caption.splitlines()[0].strip() if yt_caption else "")
            or title
            or path.stem
        )
        yt_tags = parse_hashtags(yt_cfg.get("hashtags", default_tags))
        yt_made_for_kids = _resolve_youtube_made_for_kids(yt_cfg.get("made_for_kids"))
        results["youtube"] = _attempt_upload(
            _youtube_upload, path, yt_title, yt_caption, yt_tags, yt_made_for_kids
        )
    else:
        results["youtube"] = {
            "success": False,
            "skipped": True,
            "message": "Bỏ qua (không chọn đăng)",
        }

    # TikTok
    tt_cfg = platform_configs.get("tiktok", {})
    if tt_cfg.get("enabled", True):
        tt_caption = str(tt_cfg.get("caption", description) or "").strip()
        tt_title = (
            str(tt_cfg.get("title", "") or "").strip()
            or (tt_caption.splitlines()[0].strip() if tt_caption else "")
            or title
            or path.stem
        )
        tt_tags = parse_hashtags(tt_cfg.get("hashtags", default_tags))
        tt_tagged_desc = _append_tags(tt_caption or tt_title, tt_tags)
        results["tiktok"] = _attempt_upload(
            _tiktok_upload, path, tt_title, tt_tagged_desc
        )
    else:
        results["tiktok"] = {
            "success": False,
            "skipped": True,
            "message": "Bỏ qua (không chọn đăng)",
        }

    # Facebook Reels
    fb_cfg = platform_configs.get("facebook", {})
    if fb_cfg.get("enabled", True):
        fb_caption = str(fb_cfg.get("caption", description) or "").strip()
        fb_title = str(fb_cfg.get("title", "") or "").strip() or title
        fb_tags = parse_hashtags(fb_cfg.get("hashtags", default_tags))
        fb_tagged_desc = _append_tags(fb_caption, fb_tags)
        results["facebook"] = _attempt_upload(
            _facebook_upload, path, fb_title, fb_tagged_desc
        )
    else:
        results["facebook"] = {
            "success": False,
            "skipped": True,
            "message": "Bỏ qua (không chọn đăng)",
        }

    return results
