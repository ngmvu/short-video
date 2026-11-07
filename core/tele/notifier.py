from __future__ import annotations

import asyncio
import html
import logging
from pathlib import Path
from typing import Any

import requests
from telegram import Bot
from telegram.error import NetworkError, TimedOut

from core.config import get_setting

LOGGER = logging.getLogger("short_video_distributor")
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024
TELEGRAM_WRITE_TIMEOUT = 300.0
TELEGRAM_READ_TIMEOUT = 180.0
TELEGRAM_CONNECT_TIMEOUT = 60.0
TELEGRAM_POOL_TIMEOUT = 60.0


def _report_caption(title: str, status_dict: dict[str, Any]) -> str:
    labels = {
        "youtube": "YouTube Shorts",
        "tiktok": "TikTok",
        "facebook": "Facebook Reels",
    }

    def build(title_limit: int, detail_limit: int, url_limit: int) -> str:
        lines = [f"<b>Kết quả đăng video: {html.escape(title[:title_limit])}</b>"]
        for platform, label in labels.items():
            result = status_dict.get(
                platform, {"success": False, "error": "Không có kết quả"}
            )
            if result.get("skipped"):
                lines.append(f"⏭️ <b>{label}:</b> Bỏ qua")
            elif result.get("success"):
                url = result.get("url")
                if isinstance(url, str) and url.startswith("https://") and len(url) <= url_limit:
                    detail = (
                        f'<a href="{html.escape(url, quote=True)}">Xem bài đăng</a>'
                    )
                else:
                    detail = html.escape(
                        str(
                            result.get("message")
                            or f"Mã: {result.get('id', 'đã gửi')}"
                        )[:detail_limit]
                    )
                lines.append(f"✅ <b>{label}:</b> {detail}")
            else:
                error = str(result.get("error", "Không rõ nguyên nhân"))[
                    :detail_limit
                ]
                lines.append(f"❌ <b>{label}:</b> {html.escape(error)}")
        return "\n".join(lines)

    title_limit, detail_limit, url_limit = 180, 300, 200
    while True:
        caption = build(title_limit, detail_limit, url_limit)
        if len(caption.encode("utf-16-le")) // 2 <= 1000:
            return caption
        title_limit = max(20, title_limit // 2)
        detail_limit = max(20, detail_limit // 2)
        url_limit = max(40, url_limit // 2)


async def _send_single_video_file(
    path: Path,
    caption: str,
    bot: Bot,
    chat_id: str,
    token: str,
) -> None:
    if not path.is_file():
        LOGGER.warning("Tệp video không tồn tại để gửi: %s", path)
        return
    method = (
        "sendDocument"
        if path.stat().st_size > TELEGRAM_UPLOAD_LIMIT
        else "sendVideo"
    )
    field = "document" if method == "sendDocument" else "video"

    try:
        with path.open("rb") as f:
            if method == "sendDocument":
                await bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=caption,
                    parse_mode="HTML",
                    write_timeout=TELEGRAM_WRITE_TIMEOUT,
                    read_timeout=TELEGRAM_READ_TIMEOUT,
                    connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
                    pool_timeout=TELEGRAM_POOL_TIMEOUT,
                )
            else:
                await bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=caption,
                    parse_mode="HTML",
                    write_timeout=TELEGRAM_WRITE_TIMEOUT,
                    read_timeout=TELEGRAM_READ_TIMEOUT,
                    connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
                    pool_timeout=TELEGRAM_POOL_TIMEOUT,
                )
        return
    except (TimedOut, NetworkError) as exc:
        LOGGER.warning(
            "Gửi video '%s' lên Telegram qua httpx bị timeout (%s), đang thử gửi lại qua HTTP trực tiếp...",
            path.name,
            exc,
        )

    def _sync_upload() -> None:
        with path.open("rb") as f:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/{method}",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={field: f},
                timeout=300,
            )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram video upload failed: {payload}")

    await asyncio.to_thread(_sync_upload)


async def send_final_report(
    video_path: str,
    title: str,
    status_dict: dict[str, Any],
    bot: Bot | None = None,
    scene_clips: list[str | Path] | None = None,
    scenes_info: list[dict[str, Any]] | None = None,
) -> None:
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Video file does not exist: {path}")
    chat_id = get_setting("TELEGRAM_CHAT_ID")
    token = get_setting("TELEGRAM_BOT_TOKEN")

    valid_clips = [
        Path(c)
        for c in (scene_clips or [])
        if Path(c).is_file() and Path(c).resolve() != path.resolve()
    ]
    if valid_clips and bot is not None:
        total = len(valid_clips)
        for idx, clip in enumerate(valid_clips, start=1):
            dialogue = ""
            if scenes_info and idx - 1 < len(scenes_info):
                sc = scenes_info[idx - 1]
                if isinstance(sc, dict):
                    dialogue = str(sc.get("dialogue") or "").strip()
            clip_caption = (
                f"🎬 <b>Cảnh {idx}/{total} — {html.escape(title[:300])}</b>"
            )
            if dialogue:
                clip_caption += f'\n💬 <i>"{html.escape(dialogue[:300])}"</i>'
            clip_caption += "\n📁 <i>Clip gốc từng cảnh (dùng để sửa bằng CapCut)</i>"
            try:
                await _send_single_video_file(
                    clip, clip_caption, bot, chat_id, token
                )
                await asyncio.sleep(0.5)
            except Exception as clip_exc:
                LOGGER.warning("Không gửi được clip cảnh %d: %s", idx, clip_exc)

    caption = _report_caption(title, status_dict)
    method = "sendDocument" if path.stat().st_size > TELEGRAM_UPLOAD_LIMIT else "sendVideo"

    if bot is not None:
        try:
            with path.open("rb") as video:
                if method == "sendDocument":
                    await bot.send_document(
                        chat_id=chat_id,
                        document=video,
                        caption=caption,
                        parse_mode="HTML",
                        write_timeout=TELEGRAM_WRITE_TIMEOUT,
                        read_timeout=TELEGRAM_READ_TIMEOUT,
                        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
                        pool_timeout=TELEGRAM_POOL_TIMEOUT,
                    )
                else:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=video,
                        caption=caption,
                        parse_mode="HTML",
                        write_timeout=TELEGRAM_WRITE_TIMEOUT,
                        read_timeout=TELEGRAM_READ_TIMEOUT,
                        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
                        pool_timeout=TELEGRAM_POOL_TIMEOUT,
                    )
            return
        except (TimedOut, NetworkError) as exc:
            LOGGER.warning(
                "Gửi báo cáo video qua httpx/Telegram bị timeout (%s), đang thử lại qua HTTP trực tiếp (timeout 300s)...",
                exc,
            )

    def _post_report_sync() -> None:
        with path.open("rb") as video:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/{method}",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"document" if method == "sendDocument" else "video": video},
                timeout=300,
            )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram report upload failed: {payload}")

    await asyncio.to_thread(_post_report_sync)


async def send_video_to_telegram(
    video_path: str,
    title: str,
    bot: Bot,
    scene_clips: list[str | Path] | None = None,
    scenes_info: list[dict[str, Any]] | None = None,
) -> None:
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Video file does not exist: {path}")
    chat_id = get_setting("TELEGRAM_CHAT_ID")
    token = get_setting("TELEGRAM_BOT_TOKEN")

    valid_clips = [
        Path(c)
        for c in (scene_clips or [])
        if Path(c).is_file() and Path(c).resolve() != path.resolve()
    ]

    # 1. Gửi từng cảnh riêng lẻ (clip gốc không sửa gì để tự sửa bằng CapCut)
    if valid_clips:
        total = len(valid_clips)
        for idx, clip in enumerate(valid_clips, start=1):
            dialogue = ""
            if scenes_info and idx - 1 < len(scenes_info):
                sc = scenes_info[idx - 1]
                if isinstance(sc, dict):
                    dialogue = str(sc.get("dialogue") or "").strip()

            clip_caption = (
                f"🎬 <b>Cảnh {idx}/{total} — {html.escape(title[:300])}</b>"
            )
            if dialogue:
                clip_caption += f'\n💬 <i>"{html.escape(dialogue[:300])}"</i>'
            clip_caption += "\n📁 <i>Clip gốc từng cảnh (dùng để xem & sửa bằng CapCut)</i>"

            try:
                await _send_single_video_file(
                    clip, clip_caption, bot, chat_id, token
                )
                await asyncio.sleep(0.5)
            except Exception as clip_exc:
                LOGGER.warning(
                    "Không gửi được clip cảnh %d (%s): %s",
                    idx,
                    clip.name,
                    clip_exc,
                )

    # 2. Gửi video đã gộp hoàn chỉnh bằng FFmpeg (gốc không chỉnh sửa)
    if valid_clips:
        full_caption = (
            f"✅ <b>Kịch bản đã duyệt: {html.escape(title[:600])}</b>\n"
            f"🎞️ <b>Video hoàn chỉnh (Ghép nối {len(valid_clips)} cảnh bằng FFmpeg — Gốc không chỉnh sửa)</b>"
        )
    else:
        full_caption = f"✅ Kịch bản đã duyệt: {html.escape(title[:800])}"

    await _send_single_video_file(path, full_caption, bot, chat_id, token)
