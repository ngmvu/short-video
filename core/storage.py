from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from filelock import FileLock

from core.config import STORAGE_DIR

DRAFTS_FILE = STORAGE_DIR / "drafts.json"
APPROVED_QUEUE_FILE = STORAGE_DIR / "queue_approved.json"
QUEUE_LOCK_FILE = STORAGE_DIR / "queue_approved.lock"
PROCESS_LOCK_FILE = STORAGE_DIR / "process_queue.lock"
TELEGRAM_POLL_LOCK_FILE = STORAGE_DIR / "telegram_poll.lock"
PUBLISH_REPORTS_FILE = STORAGE_DIR / "publish_reports.json"
PUBLISH_REPORTS_LOCK_FILE = STORAGE_DIR / "publish_reports.lock"
VIDEO_METADATA_FILE = STORAGE_DIR / "video_metadata.json"
VIDEO_METADATA_LOCK_FILE = STORAGE_DIR / "video_metadata.lock"


def ensure_storage() -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    (STORAGE_DIR / "downloads").mkdir(parents=True, exist_ok=True)
    for path in (DRAFTS_FILE, APPROVED_QUEUE_FILE):
        if not path.exists() or not path.read_text(encoding="utf-8").strip():
            path.write_text("[]\n", encoding="utf-8")


def read_json(path: Path, default: Any) -> Any:
    ensure_storage()
    if not path.exists():
        return default
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return default
    return json.loads(raw)


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
        raise


def save_drafts(drafts: list[dict[str, Any]]) -> None:
    ensure_storage()
    write_json_atomic(DRAFTS_FILE, drafts)


def approve_draft(draft_id: str, allow_requeue: bool = False) -> bool:
    ensure_storage()
    with FileLock(str(QUEUE_LOCK_FILE)):
        drafts = read_json(DRAFTS_FILE, [])
        draft = next((item for item in drafts if item["id"] == draft_id), None)
        allowed_states = (
            ("pending", "approved", "skipped", "processed")
            if allow_requeue
            else ("pending", "approved", "skipped")
        )
        if draft is None or draft.get("state", "pending") not in allowed_states:
            return False

        queue = read_json(APPROVED_QUEUE_FILE, [])
        if any(item["id"] == draft_id for item in queue):
            draft["state"] = "approved"
            write_json_atomic(DRAFTS_FILE, drafts)
            return True

        from datetime import datetime, timezone

        queue.append(
            {
                "id": draft_id,
                "approved_at": datetime.now(timezone.utc).isoformat(),
                "idea": draft["idea"],
            }
        )
        draft["state"] = "approved"
        write_json_atomic(DRAFTS_FILE, drafts)
        write_json_atomic(APPROVED_QUEUE_FILE, queue)
        return True


def skip_draft(draft_id: str) -> bool:
    ensure_storage()
    with FileLock(str(QUEUE_LOCK_FILE)):
        drafts = read_json(DRAFTS_FILE, [])
        draft = next((item for item in drafts if item["id"] == draft_id), None)
        if draft is None or draft.get("state", "pending") != "pending":
            return False
        draft["state"] = "skipped"
        write_json_atomic(DRAFTS_FILE, drafts)
        return True


def append_pending_draft(draft_id: str, idea: dict[str, Any], source: str = "telegram") -> None:
    """Append one draft without overwriting the existing drafts list."""
    ensure_storage()
    with FileLock(str(QUEUE_LOCK_FILE)):
        drafts = read_json(DRAFTS_FILE, [])
        if not isinstance(drafts, list):
            drafts = []
        drafts = [item for item in drafts if item.get("id") != draft_id]
        drafts.append({"id": draft_id, "idea": idea, "state": "pending", "source": source})
        write_json_atomic(DRAFTS_FILE, drafts)


def enqueue_draft_with_options(draft_id: str, publish: bool, notify_progress: bool = True) -> int:
    """Approve a saved draft with a per-item publish choice.

    Returns the 1-based position in the approved queue, or 0 if the draft does not exist.
    """
    ensure_storage()
    with FileLock(str(QUEUE_LOCK_FILE)):
        drafts = read_json(DRAFTS_FILE, [])
        draft = next((item for item in drafts if item.get("id") == draft_id), None)
        if draft is None or not isinstance(draft.get("idea"), dict):
            return 0
        from datetime import datetime, timezone

        queue = read_json(APPROVED_QUEUE_FILE, [])
        queue = [item for item in queue if item.get("id") != draft_id]
        queue.append(
            {
                "id": draft_id,
                "approved_at": datetime.now(timezone.utc).isoformat(),
                "idea": draft["idea"],
                "publish": bool(publish),
                "notify_progress": bool(notify_progress),
            }
        )
        draft["state"] = "approved"
        write_json_atomic(DRAFTS_FILE, drafts)
        write_json_atomic(APPROVED_QUEUE_FILE, queue)
        return len(queue)


def promote_approved_queue_item(item_id: str) -> int:
    """Move a queued item to the front and return its 1-based position, or 0 if missing."""
    ensure_storage()
    with FileLock(str(QUEUE_LOCK_FILE)):
        queue = read_json(APPROVED_QUEUE_FILE, [])
        item_index = next(
            (
                index
                for index, item in enumerate(queue)
                if isinstance(item, dict) and item.get("id") == item_id
            ),
            None,
        )
        if item_index is None:
            return 0
        if item_index:
            queue.insert(0, queue.pop(item_index))
            write_json_atomic(APPROVED_QUEUE_FILE, queue)
        return 1


def remove_approved_item(item_id: str) -> None:
    ensure_storage()
    with FileLock(str(QUEUE_LOCK_FILE)):
        queue = read_json(APPROVED_QUEUE_FILE, [])
        write_json_atomic(
            APPROVED_QUEUE_FILE,
            [item for item in queue if item["id"] != item_id],
        )
        drafts = read_json(DRAFTS_FILE, [])
        for draft in drafts:
            if draft["id"] == item_id:
                draft["state"] = "processed"
                break
        write_json_atomic(DRAFTS_FILE, drafts)


def delete_queue_item(item_id: str) -> bool:
    ensure_storage()
    with FileLock(str(QUEUE_LOCK_FILE)):
        queue = read_json(APPROVED_QUEUE_FILE, [])
        filtered_queue = [item for item in queue if item.get("id") != item_id]
        removed = len(filtered_queue) != len(queue)
        if removed:
            write_json_atomic(APPROVED_QUEUE_FILE, filtered_queue)
        drafts = read_json(DRAFTS_FILE, [])
        draft_updated = False
        for draft in drafts:
            if draft.get("id") == item_id and draft.get("state") == "approved":
                draft["state"] = "pending"
                draft_updated = True
        if draft_updated:
            write_json_atomic(DRAFTS_FILE, drafts)
        return removed or draft_updated


def clear_approved_queue() -> int:
    ensure_storage()
    with FileLock(str(QUEUE_LOCK_FILE)):
        queue = read_json(APPROVED_QUEUE_FILE, [])
        count = len(queue)
        write_json_atomic(APPROVED_QUEUE_FILE, [])
        drafts = read_json(DRAFTS_FILE, [])
        draft_updated = False
        for draft in drafts:
            if draft.get("state") == "approved":
                draft["state"] = "pending"
                draft_updated = True
        if draft_updated:
            write_json_atomic(DRAFTS_FILE, drafts)
        return count


def delete_draft(draft_id: str) -> bool:
    ensure_storage()
    with FileLock(str(QUEUE_LOCK_FILE)):
        drafts = read_json(DRAFTS_FILE, [])
        filtered_drafts = [item for item in drafts if item.get("id") != draft_id]
        removed_draft = len(filtered_drafts) != len(drafts)
        if removed_draft:
            write_json_atomic(DRAFTS_FILE, filtered_drafts)
        queue = read_json(APPROVED_QUEUE_FILE, [])
        filtered_queue = [item for item in queue if item.get("id") != draft_id]
        removed_queue = len(filtered_queue) != len(queue)
        if removed_queue:
            write_json_atomic(APPROVED_QUEUE_FILE, filtered_queue)
        return removed_draft or removed_queue


def save_publish_report(report: dict[str, Any]) -> None:
    ensure_storage()
    with FileLock(str(PUBLISH_REPORTS_LOCK_FILE)):
        reports = read_json(PUBLISH_REPORTS_FILE, [])
        reports.insert(0, report)
        write_json_atomic(PUBLISH_REPORTS_FILE, reports[:100])


def save_video_social_metadata(
    video_name: str,
    social_posts: dict[str, Any],
    title: str = "",
    item_id: str = "",
) -> None:
    ensure_storage()
    with FileLock(str(VIDEO_METADATA_LOCK_FILE)):
        metadata = read_json(VIDEO_METADATA_FILE, {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata[video_name] = {
            "video_name": video_name,
            "title": title,
            "item_id": item_id,
            "social_posts": {
                k: v
                for k, v in social_posts.items()
                if k in ("tiktok", "facebook", "youtube") and isinstance(v, dict)
            },
        }
        write_json_atomic(VIDEO_METADATA_FILE, metadata)

    if item_id:
        with FileLock(str(QUEUE_LOCK_FILE)):
            drafts = read_json(DRAFTS_FILE, [])
            changed = False
            for draft in drafts:
                if draft.get("id") == item_id:
                    draft["video_name"] = video_name
                    if isinstance(draft.get("idea"), dict) and not draft["idea"].get("social_posts"):
                        draft["idea"]["social_posts"] = social_posts
                    changed = True
                    break
            if changed:
                write_json_atomic(DRAFTS_FILE, drafts)


def get_video_social_metadata() -> dict[str, dict[str, Any]]:
    ensure_storage()
    data = read_json(VIDEO_METADATA_FILE, {})
    return data if isinstance(data, dict) else {}
