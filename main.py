from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from filelock import FileLock
from telegram import Bot
from telegram.constants import ParseMode

from core.tele.approval_bot import (
    send_auth_error_notification,
    send_draft_ideas,
    send_existing_drafts,
)
from core.tele.listener import run_telegram_listener
from core.config import get_setting
from core.flow_runner import (
    FlowAuthError,
    FlowPolicyViolationError,
    acknowledge_processed,
    generate_video,
    get_scene_clips_for_item,
    read_approved_queue,
)
from core.gemini.idea_generator import _normalize_social_posts, generate_ideas
from core.social_uploader import parse_hashtags, publish_to_social
from core.storage import (
    DRAFTS_FILE,
    PROCESS_LOCK_FILE,
    STORAGE_DIR,
    approve_draft,
    ensure_storage,
    read_json,
    save_publish_report,
    save_video_social_metadata,
    write_json_atomic,
)
from core.tele.notifier import send_final_report, send_video_to_telegram

LOGGER = logging.getLogger("short_video_distributor")
WORKFLOW_STATUS_FILE = STORAGE_DIR / "workflow_status.json"
WORKFLOW_TERMINAL_NODES = {"approval-video", "approval-publish", "automation", "telegram-listener"}


async def generate_and_send(
    bot: Bot | None = None,
    prompt: str | None = None,
    scene_count: int = 3,
    duration_seconds: int = 10,
    ideas_count: int = 10,
) -> None:
    ideas = await asyncio.to_thread(
        generate_ideas,
        prompt=prompt,
        scene_count=scene_count,
        duration_seconds=duration_seconds,
        ideas_count=ideas_count,
    )
    LOGGER.info("Generated %s idea(s); sending Telegram approval messages.", len(ideas))
    await send_draft_ideas(ideas, bot=bot)
    LOGGER.info("Sent %s ideas to the configured Telegram chat.", len(ideas))


async def _process_one(
    item: dict[str, Any], bot: Bot, publish: bool = True
) -> None:
    idea = item["idea"]
    item_id = item["id"]
    item_publish = (
        bool(item["publish"])
        if isinstance(item, dict) and "publish" in item
        else bool(publish)
    )
    LOGGER.info(
        "Starting Flow video generation for item %s (%s)...",
        item_id,
        idea.get("title", "Untitled"),
    )
    if bot is not None and item.get("notify_progress", True):
        try:
            await bot.send_message(
                chat_id=get_setting("TELEGRAM_CHAT_ID"),
                text=(
                    f"🎬 Đang mở Google Flow tạo video cho: *{idea.get('title', item_id)}*...\n"
                    f"(Hệ thống sẽ ghép các Scene và gửi lại video sau khi render)"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    video_path = await generate_video(item_id, idea)
    scene_clips = get_scene_clips_for_item(item_id, video_path)
    social_posts = _normalize_social_posts(
        idea.get("social_posts"),
        fallback_title=idea.get("title", ""),
        scenes=idea.get("scenes"),
    )
    idea["social_posts"] = social_posts
    await asyncio.to_thread(
        save_video_social_metadata,
        video_path.name,
        social_posts,
        idea.get("title", ""),
        item_id,
    )
    if not item_publish:
        acknowledge_processed(item_id)
        try:
            if scene_clips:
                await send_video_to_telegram(
                    str(video_path),
                    idea["title"],
                    bot,
                    scene_clips=scene_clips,
                    scenes_info=idea.get("scenes"),
                )
            else:
                await send_video_to_telegram(str(video_path), idea["title"], bot)
            LOGGER.info("Sent approved video %s (and %d scene clips) to Telegram.", item_id, len(scene_clips))
        except Exception as tg_exc:
            LOGGER.warning(
                "Đã tạo và ghép xong video tại '%s' nhưng gửi qua Telegram bị nghẽn mạng (%s). Bạn có thể xem/đăng trực tiếp trên Web UI!",
                video_path,
                tg_exc,
            )
        return

    acknowledge_processed(item_id)
    platform_configs = {
        platform: {
            "enabled": True,
            "title": social_posts.get(platform, {}).get("title", idea["title"]),
            "caption": social_posts.get(platform, {}).get("caption", ""),
            "hashtags": parse_hashtags(
                social_posts.get(platform, {}).get("hashtags", "")
            ),
        }
        for platform in ("tiktok", "facebook", "youtube")
    }
    statuses = await asyncio.to_thread(
        publish_to_social,
        str(video_path),
        idea["title"],
        f"{idea['title']} — video ngắn hài hước đời thường Việt Nam.",
        ["Vietnam", "comedy", "shorts"],
        platform_configs=platform_configs,
    )
    await asyncio.to_thread(
        save_publish_report,
        {
            "item_id": item_id,
            "title": idea["title"],
            "video": video_path.name,
            "video_name": video_path.name,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "platform_configs": {
                platform: {
                    "enabled": True,
                    "title": cfg["title"],
                    "caption": cfg["caption"],
                    "hashtags": [f"#{t}" for t in cfg["hashtags"]],
                }
                for platform, cfg in platform_configs.items()
            },
            "statuses": statuses,
        },
    )
    if scene_clips:
        await send_final_report(
            str(video_path),
            idea["title"],
            statuses,
            bot=bot,
            scene_clips=scene_clips,
            scenes_info=idea.get("scenes"),
        )
    else:
        await send_final_report(str(video_path), idea["title"], statuses, bot=bot)
    LOGGER.info("Processed approved video %s at %s.", item_id, video_path)


async def _notify_processing_failure(item_id: str, bot: Bot | None) -> None:
    message = (
        f"⚠️ Không xử lý được kịch bản đã duyệt {item_id}. "
        "Kiểm tra log dashboard; hệ thống sẽ không tự thử lại cho đến khi khởi động lại listener."
    )
    if bot is not None:
        await bot.send_message(
            chat_id=get_setting("TELEGRAM_CHAT_ID"),
            text=message,
        )
        return
    async with Bot(get_setting("TELEGRAM_BOT_TOKEN")) as standalone_bot:
        await standalone_bot.send_message(
            chat_id=get_setting("TELEGRAM_CHAT_ID"),
            text=message,
        )


async def _cancel_policy_blocked_item(
    item: dict[str, Any], bot: Bot | None
) -> None:
    """Drop an item Flow rejected for policy reasons and tell the user via Telegram."""
    item_id = item["id"]
    title = item.get("idea", {}).get("title", item_id)
    acknowledge_processed(item_id)
    message = (
        f"🚫 Google Flow từ chối kịch bản \"{title}\" ({item_id}): "
        "\"This prompt might violate our policies about generating harmful content.\"\n"
        "Đã hủy lượt này và xóa khỏi hàng đợi. Hãy sửa nội dung (tránh từ ngữ bạo lực/quá khích) "
        "hoặc dùng /choice để tạo kịch bản mới."
    )
    try:
        if bot is not None:
            await bot.send_message(
                chat_id=get_setting("TELEGRAM_CHAT_ID"), text=message
            )
        else:
            async with Bot(get_setting("TELEGRAM_BOT_TOKEN")) as standalone_bot:
                await standalone_bot.send_message(
                    chat_id=get_setting("TELEGRAM_CHAT_ID"), text=message
                )
    except Exception:
        LOGGER.exception(
            "Could not notify Telegram about policy-blocked item %s.", item_id
        )


async def process_queue(
    bot: Bot | None = None,
    publish: bool = True,
    skip_failed_ids: set[str] | None = None,
) -> None:
    with FileLock(str(PROCESS_LOCK_FILE), timeout=0):
        items = [
            item
            for item in read_approved_queue()
            if skip_failed_ids is None or item["id"] not in skip_failed_ids
        ]
        if not items:
            LOGGER.info("Approved queue is empty.")
            return
        failures: list[str] = []
        if bot is not None:
            for item in items:
                try:
                    await _process_one(item, bot, publish=publish)
                except FlowAuthError as auth_error:
                    LOGGER.error(
                        "⛔ Lỗi xác thực Google Flow khi xử lý kịch bản %s. Dừng ngay hàng đợi (giữ nguyên các kịch bản đã duyệt) — hãy chạy save_flow_auth.py để đăng nhập lại.",
                        item["id"],
                    )
                    try:
                        await send_auth_error_notification(bot)
                    except Exception:
                        LOGGER.exception("Could not send Flow auth error notification to Telegram.")
                    raise auth_error
                except FlowPolicyViolationError:
                    LOGGER.warning(
                        "Flow từ chối prompt của item %s (vi phạm chính sách). Hủy lượt này.",
                        item["id"],
                    )
                    await _cancel_policy_blocked_item(item, bot)
                    continue
                except Exception as error:
                    LOGGER.exception("Failed to process approved item %s.", item["id"])
                    if skip_failed_ids is not None:
                        LOGGER.error(
                            "Item %s will not be retried automatically until the listener restarts.",
                            item["id"],
                        )
                        try:
                            await _notify_processing_failure(item["id"], bot)
                        except Exception:
                            LOGGER.exception(
                                "Could not notify Telegram about failed item %s.",
                                item["id"],
                            )
                    failures.append(f"{item['id']}: {error}")
                    if skip_failed_ids is not None:
                        skip_failed_ids.add(item["id"])
        else:
            async with Bot(get_setting("TELEGRAM_BOT_TOKEN")) as standalone_bot:
                for item in items:
                    try:
                        await _process_one(item, standalone_bot, publish=publish)
                    except FlowAuthError as auth_error:
                        LOGGER.error(
                            "⛔ Lỗi xác thực Google Flow khi xử lý kịch bản %s. Dừng ngay hàng đợi (giữ nguyên các kịch bản đã duyệt) — hãy chạy save_flow_auth.py để đăng nhập lại.",
                            item["id"],
                        )
                        try:
                            await send_auth_error_notification(standalone_bot)
                        except Exception:
                            LOGGER.exception("Could not send Flow auth error notification to Telegram.")
                        raise auth_error
                    except FlowPolicyViolationError:
                        LOGGER.warning(
                            "Flow từ chối prompt của item %s (vi phạm chính sách). Hủy lượt này.",
                            item["id"],
                        )
                        await _cancel_policy_blocked_item(item, standalone_bot)
                        continue
                    except Exception as error:
                        LOGGER.exception(
                            "Failed to process approved item %s.", item["id"]
                        )
                        if skip_failed_ids is not None:
                            LOGGER.error(
                                "Item %s will not be retried automatically until the listener restarts.",
                                item["id"],
                            )
                            try:
                                await _notify_processing_failure(
                                    item["id"], standalone_bot
                                )
                            except Exception:
                                LOGGER.exception(
                                    "Could not notify Telegram about failed item %s.",
                                    item["id"],
                                )
                        failures.append(f"{item['id']}: {error}")
                        if skip_failed_ids is not None:
                            skip_failed_ids.add(item["id"])
        if failures:
            raise RuntimeError(
                f"{len(failures)} approved item(s) failed: {'; '.join(failures)}"
            )


async def _run_telegram_listener(
    generate_on_start: bool,
    process_on_start: bool,
    interval_minutes: int | None,
    idea_prompt: str | None = None,
    scene_count: int = 3,
    duration_seconds: int = 10,
    process_approved: bool = False,
    publish_approved: bool = True,
    resend_saved_on_start: bool = False,
    schedule_mode: str = "interval",
    schedule_datetime: str | None = None,
    schedule_time: str | None = None,
    ideas_count: int = 10,
    poll_updates: bool = True,
) -> None:
    await run_telegram_listener(
        generate_on_start=generate_on_start,
        process_on_start=process_on_start,
        interval_minutes=interval_minutes,
        generate_and_send_fn=generate_and_send,
        process_queue_fn=process_queue,
        idea_prompt=idea_prompt,
        scene_count=scene_count,
        duration_seconds=duration_seconds,
        process_approved=process_approved,
        publish_approved=publish_approved,
        resend_saved_on_start=resend_saved_on_start,
        schedule_mode=schedule_mode,
        schedule_datetime=schedule_datetime,
        schedule_time=schedule_time,
        ideas_count=ideas_count,
        poll_updates=poll_updates,
    )


def _write_workflow_status(
    running: bool,
    completed_nodes: list[str],
    current_node: str | None = None,
    error: str | None = None,
    node_order: list[dict[str, str]] | None = None,
    auth_error: bool = False,
) -> None:
    status = {
        "running": running,
        "current_node": current_node,
        "completed_nodes": completed_nodes,
        "error": error,
        "auth_error": auth_error,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if node_order is not None:
        status["node_order"] = node_order
    write_json_atomic(WORKFLOW_STATUS_FILE, status)


async def _run_workflow(
    nodes: list[dict[str, Any]],
    idea_prompt: str | None,
    scene_count: int,
    duration_seconds: int,
    interval_minutes: int = 360,
    schedule_mode: str = "interval",
    schedule_datetime: str | None = None,
    schedule_time: str | None = None,
    ideas_count: int = 10,
    external_telegram_listener: bool = False,
) -> None:
    completed_nodes: list[str] = []
    current_node: str | None = None
    ran_generate_ideas = False
    try:
        for idx, node in enumerate(nodes):
            current_node = node["id"]
            node_type = node["type"]
            next_types = [n["type"] for n in nodes[idx + 1 :]]
            _write_workflow_status(True, completed_nodes, current_node, node_order=nodes)
            LOGGER.info("Running workflow node %s (%s).", current_node, node_type)

            if node_type == "generate-ideas":
                await generate_and_send(
                    prompt=idea_prompt,
                    scene_count=scene_count,
                    duration_seconds=duration_seconds,
                    ideas_count=ideas_count,
                )
                ran_generate_ideas = True
            elif node_type == "process-queue":
                if ran_generate_ideas and not read_approved_queue():
                    drafts = read_json(DRAFTS_FILE, [])
                    if isinstance(drafts, list):
                        target_draft = next(
                            (
                                d
                                for d in reversed(drafts)
                                if isinstance(d, dict)
                                and d.get("id")
                                and d.get("state", "pending") == "pending"
                            ),
                            None,
                        ) or (drafts[-1] if drafts and isinstance(drafts[-1], dict) else None)
                        if target_draft and target_draft.get("id"):
                            auto_id = str(target_draft["id"])
                            approve_draft(auto_id, allow_requeue=True)
                            LOGGER.info(
                                "⚡ Chế độ Auto Flow (Tạo ý tưởng -> Sản xuất & đăng): Tự động Approve kịch bản '%s' (%s) vào hàng đợi để tạo video & đăng MXH!",
                                target_draft.get("idea", {}).get("title", auto_id),
                                auto_id,
                            )
                await process_queue(publish=True)
            elif node_type in {"approval-video", "approval-publish", "telegram-listener"}:
                if external_telegram_listener and node_type == "telegram-listener":
                    LOGGER.info(
                        "Telegram listener is managed by the dashboard; no duplicate listener was started."
                    )
                    completed_nodes.append(current_node)
                    current_node = None
                    _write_workflow_status(True, completed_nodes, node_order=nodes)
                    break
                all_remaining = [n["type"] for n in nodes[idx:]]
                should_produce = any(
                    t in {"approval-video", "approval-publish", "process-queue"}
                    for t in all_remaining
                )
                should_publish = any(
                    t in {"approval-publish", "process-queue"}
                    for t in all_remaining
                )
                auto_node = next((n for n in nodes if n.get("type") == "automation"), None)
                if should_publish:
                    LOGGER.info(
                        "🚀 Flow có nối tới bước Đăng MXH: Khi bạn bấm Approve (trên Web hoặc Telegram), hệ thống sẽ tự tạo 3 video trên Google Flow, ghép lại VÀ tự động đăng lên TikTok, Facebook Reels, YouTube Shorts!"
                    )
                if auto_node:
                    auto_mode = auto_node.get("schedule_mode") or schedule_mode or "interval"
                    auto_datetime = auto_node.get("schedule_datetime") or schedule_datetime
                    auto_time = auto_node.get("schedule_time") or schedule_time
                    auto_interval = auto_node.get("interval_minutes") or interval_minutes or 360
                    if auto_mode == "datetime" and auto_datetime:
                        LOGGER.info("📅 Node Tự động hóa: Đã lập lịch chạy Flow vào ngày giờ: %s", auto_datetime)
                    elif auto_mode == "daily" and auto_time:
                        LOGGER.info("⏰ Node Tự động hóa: Đã lập lịch chạy Flow cố định hàng ngày vào lúc: %s", auto_time)
                    else:
                        LOGGER.info(
                            "⏱️ Node Tự động hóa: Đã lập lịch lặp lại sau mỗi %d phút (%.1f giờ)!",
                            auto_interval,
                            auto_interval / 60,
                        )
                else:
                    auto_mode = schedule_mode or "interval"
                    auto_datetime = schedule_datetime
                    auto_time = schedule_time
                    auto_interval = interval_minutes or 360

                await _run_telegram_listener(
                    False,
                    False,
                    auto_interval if (auto_node and auto_mode == "interval") else None,
                    process_approved=should_produce,
                    publish_approved=should_publish,
                    idea_prompt=idea_prompt,
                    scene_count=scene_count,
                    duration_seconds=duration_seconds,
                    schedule_mode=auto_mode if auto_node else "interval",
                    schedule_datetime=auto_datetime if auto_node else None,
                    schedule_time=auto_time if auto_node else None,
                    ideas_count=ideas_count,
                    poll_updates=not external_telegram_listener,
                )
                completed_nodes.append(current_node)
                current_node = None
                _write_workflow_status(True, completed_nodes, node_order=nodes)
                break
            elif node_type == "automation":
                all_types = [n["type"] for n in nodes]
                should_produce = any(
                    t in {"approval-video", "approval-publish", "process-queue"}
                    for t in all_types
                )
                should_publish = any(
                    t in {"approval-publish", "process-queue"}
                    for t in all_types
                )
                auto_node = next((n for n in nodes if n.get("type") == "automation"), node)
                auto_mode = auto_node.get("schedule_mode") or schedule_mode or "interval"
                auto_datetime = auto_node.get("schedule_datetime") or schedule_datetime
                auto_time = auto_node.get("schedule_time") or schedule_time
                auto_interval = auto_node.get("interval_minutes") or interval_minutes or 360

                if auto_mode == "datetime" and auto_datetime:
                    LOGGER.info("📅 Khởi chạy Node Tự động hóa theo lịch ngày giờ: %s", auto_datetime)
                elif auto_mode == "daily" and auto_time:
                    LOGGER.info("⏰ Khởi chạy Node Tự động hóa theo lịch hàng ngày lúc: %s", auto_time)
                else:
                    LOGGER.info(
                        "⏱️ Khởi chạy Node Tự động hóa theo lịch chu kỳ: mỗi %d phút (%.1f giờ)!",
                        auto_interval,
                        auto_interval / 60,
                    )

                gen_on_start = (not ran_generate_ideas) if auto_mode == "interval" else False
                await _run_telegram_listener(
                    generate_on_start=gen_on_start,
                    process_on_start=True,
                    interval_minutes=auto_interval if auto_mode == "interval" else None,
                    process_approved=should_produce,
                    publish_approved=should_publish,
                    idea_prompt=idea_prompt,
                    scene_count=scene_count,
                    duration_seconds=duration_seconds,
                    schedule_mode=auto_mode,
                    schedule_datetime=auto_datetime,
                    schedule_time=auto_time,
                    poll_updates=not external_telegram_listener,
                )
                completed_nodes.append(current_node)
                current_node = None
                _write_workflow_status(True, completed_nodes, node_order=nodes)
                break
            else:
                raise ValueError(f"Unknown workflow node type: {node_type}")

            completed_nodes.append(current_node)
            current_node = None
            _write_workflow_status(True, completed_nodes, node_order=nodes)

        _write_workflow_status(False, completed_nodes, node_order=nodes)
    except Exception as error:
        _write_workflow_status(
            False,
            completed_nodes,
            current_node,
            str(error),
            node_order=nodes,
            auth_error=isinstance(error, FlowAuthError),
        )
        raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate, approve, produce, and distribute short videos."
    )
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument(
        "--generate-ideas",
        action="store_true",
        help="Generate ideas, send them to Telegram, and keep listening for approvals.",
    )
    actions.add_argument(
        "--resend-drafts",
        action="store_true",
        help="Re-send saved drafts from storage/drafts.json to Telegram and listen for approvals.",
    )
    actions.add_argument(
        "--process-queue",
        action="store_true",
        help="Produce approved videos, publish to configured platforms, and report.",
    )
    actions.add_argument(
        "--produce-telegram",
        action="store_true",
        help="Produce approved videos from queue_approved.json and send them to Telegram without social publishing.",
    )
    actions.add_argument(
        "--auto",
        action="store_true",
        help="Start Telegram approval polling and scheduled generation/queue processing.",
    )
    actions.add_argument(
        "--telegram-poll",
        action="store_true",
        help="Run the Telegram approval callback listener without scheduling jobs.",
    )
    actions.add_argument(
        "--run-flow",
        action="store_true",
        help="Run a validated workflow node list in sequence.",
    )
    actions.add_argument(
        "--approval-video",
        action="store_true",
        help="Listen for approvals, create videos, and send them to Telegram.",
    )
    actions.add_argument(
        "--approval-publish",
        action="store_true",
        help="Listen for approvals, create videos, and automatically publish to TikTok, Facebook Reels, and YouTube Shorts.",
    )
    actions.add_argument(
        "--save-flow-auth",
        action="store_true",
        help="Open Google Chrome to sign in to Google Flow and save auth.json.",
    )
    parser.add_argument("--flow-nodes-json", default="[]")
    parser.add_argument(
        "--external-telegram-listener",
        action="store_true",
        help="Use a Telegram listener managed by another process instead of polling here.",
    )
    parser.add_argument(
        "--schedule-mode",
        choices=["interval", "datetime", "daily"],
        default="interval",
        help="Scheduling mode for automation node (interval, datetime, or daily).",
    )
    parser.add_argument(
        "--schedule-datetime",
        default="",
        help="Target datetime for 'datetime' mode in ISO format (e.g. 2026-11-02T18:30).",
    )
    parser.add_argument(
        "--schedule-time",
        default="",
        help="Target daily time for 'daily' mode (e.g. 19:00).",
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=360,
        help="Idea generation interval used by --auto or interval mode (default: 360).",
    )
    parser.add_argument(
        "--idea-prompt",
        default="",
        help="Custom story brief used for this idea-generation run.",
    )
    parser.add_argument(
        "--scene-count",
        type=int,
        default=3,
        help="Number of scenes per generated idea (1-20).",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=10,
        help="Requested total video duration in seconds (1-180).",
    )
    parser.add_argument(
        "--ideas-count",
        type=int,
        default=10,
        help="Number of ideas to generate per batch (1-25, default: 10).",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> None:
    ensure_storage()
    if not 1 <= args.ideas_count <= 25:
        raise ValueError("--ideas-count must be between 1 and 25.")
    if args.run_flow:
        if not 1 <= args.scene_count <= 20:
            raise ValueError("--scene-count must be between 1 and 20.")
        if not 1 <= args.duration_seconds <= 180:
            raise ValueError("--duration-seconds must be between 1 and 180.")
        nodes = json.loads(args.flow_nodes_json)
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("A workflow must contain at least one node.")
        if any(
            not isinstance(node, dict)
            or not isinstance(node.get("id"), str)
            or not isinstance(node.get("type"), str)
            or node.get("type") not in {
                "generate-ideas",
                "process-queue",
                "approval-video",
                "approval-publish",
                "telegram-listener",
                "automation",
            }
            for node in nodes
        ):
            raise ValueError("Workflow contains an invalid node.")
        allowed_after_listener = {
            "approval-video",
            "approval-publish",
            "process-queue",
            "automation",
        }
        for index, node in enumerate(nodes):
            if node["type"] in WORKFLOW_TERMINAL_NODES and index != len(nodes) - 1:
                trailing = [n["type"] for n in nodes[index + 1 :]]
                if any(t not in allowed_after_listener for t in trailing):
                    raise ValueError("Long-running listener nodes must be last in the flow.")
        await _run_workflow(
            nodes,
            args.idea_prompt or None,
            args.scene_count,
            args.duration_seconds,
            interval_minutes=args.interval_minutes,
            schedule_mode=args.schedule_mode,
            schedule_datetime=args.schedule_datetime or None,
            schedule_time=args.schedule_time or None,
            ideas_count=args.ideas_count,
            external_telegram_listener=args.external_telegram_listener,
        )
    elif args.generate_ideas:
        if not 1 <= args.scene_count <= 20:
            raise ValueError("--scene-count must be between 1 and 20.")
        if not 1 <= args.duration_seconds <= 180:
            raise ValueError("--duration-seconds must be between 1 and 180.")
        if args.external_telegram_listener:
            await generate_and_send(
                prompt=args.idea_prompt or None,
                scene_count=args.scene_count,
                duration_seconds=args.duration_seconds,
                ideas_count=args.ideas_count,
            )
        else:
            await _run_telegram_listener(
                True,
                False,
                None,
                idea_prompt=args.idea_prompt or None,
                scene_count=args.scene_count,
                duration_seconds=args.duration_seconds,
                ideas_count=args.ideas_count,
            )
    elif args.resend_drafts:
        if args.external_telegram_listener:
            await send_existing_drafts()
        else:
            await _run_telegram_listener(
                False,
                False,
                None,
                process_approved=True,
                publish_approved=False,
                resend_saved_on_start=True,
            )
    elif args.approval_video:
        await _run_telegram_listener(
            False,
            False,
            None,
            process_approved=True,
            publish_approved=False,
            poll_updates=not args.external_telegram_listener,
        )
    elif args.approval_publish:
        await _run_telegram_listener(
            False,
            False,
            None,
            process_approved=True,
            publish_approved=True,
            poll_updates=not args.external_telegram_listener,
        )
    elif args.produce_telegram:
        await process_queue(publish=False)
    elif args.process_queue:
        await process_queue()
    elif args.save_flow_auth:
        from scripts.save_flow_auth import save_flow_session

        await asyncio.to_thread(save_flow_session, True)
    elif args.auto:
        if args.schedule_mode == "interval" and args.interval_minutes < 1:
            raise ValueError("--interval-minutes must be at least 1.")
        await _run_telegram_listener(
            generate_on_start=True if args.schedule_mode == "interval" else False,
            process_on_start=True,
            interval_minutes=args.interval_minutes if args.schedule_mode == "interval" else None,
            schedule_mode=args.schedule_mode,
            schedule_datetime=args.schedule_datetime or None,
            schedule_time=args.schedule_time or None,
            poll_updates=not args.external_telegram_listener,
        )
    else:
        await _run_telegram_listener(False, False, None)


def main() -> None:
    logging.basicConfig(
        level=get_setting("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        LOGGER.info("Stopped.")
    except FlowAuthError as auth_error:
        _write_workflow_status(
            False,
            [],
            error=str(auth_error),
            auth_error=True,
        )
        LOGGER.exception("Flow authentication failed.")
        raise
    except Exception:
        LOGGER.exception("Command failed.")
        raise


if __name__ == "__main__":
    main()
