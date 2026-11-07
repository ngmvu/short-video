from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from filelock import FileLock, Timeout
from telegram import Bot
from telegram.ext import Application

from core.config import get_setting
from core.flow_runner import FlowAuthError
from core.storage import TELEGRAM_POLL_LOCK_FILE
from core.tele.approval_bot import (
    register_approval_handler,
    register_bot_commands,
    send_existing_drafts,
)

LOGGER = logging.getLogger("short_video_distributor")
GenerateAndSend = Callable[..., Awaitable[None]]
ProcessQueue = Callable[..., Awaitable[None]]


async def run_telegram_listener(
    generate_on_start: bool,
    process_on_start: bool,
    interval_minutes: int | None,
    generate_and_send_fn: GenerateAndSend,
    process_queue_fn: ProcessQueue,
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
    if not poll_updates:
        await _run_telegram_listener_locked(
            generate_on_start,
            process_on_start,
            interval_minutes,
            generate_and_send_fn,
            process_queue_fn,
            idea_prompt,
            scene_count,
            duration_seconds,
            process_approved,
            publish_approved,
            resend_saved_on_start,
            schedule_mode,
            schedule_datetime,
            schedule_time,
            ideas_count,
            poll_updates=False,
        )
        return
    try:
        with FileLock(str(TELEGRAM_POLL_LOCK_FILE), timeout=0):
            await _run_telegram_listener_locked(
                generate_on_start,
                process_on_start,
                interval_minutes,
                generate_and_send_fn,
                process_queue_fn,
                idea_prompt,
                scene_count,
                duration_seconds,
                process_approved,
                publish_approved,
                resend_saved_on_start,
                schedule_mode,
                schedule_datetime,
                schedule_time,
                ideas_count,
                poll_updates=True,
            )
    except Timeout as error:
        raise RuntimeError(
            "Another process is already polling Telegram for this bot. "
            "Stop the other workflow before starting a Telegram listener."
        ) from error


async def _run_telegram_listener_locked(
    generate_on_start: bool,
    process_on_start: bool,
    interval_minutes: int | None,
    generate_and_send_fn: GenerateAndSend,
    process_queue_fn: ProcessQueue,
    idea_prompt: str | None,
    scene_count: int,
    duration_seconds: int,
    process_approved: bool,
    publish_approved: bool,
    resend_saved_on_start: bool = False,
    schedule_mode: str = "interval",
    schedule_datetime: str | None = None,
    schedule_time: str | None = None,
    ideas_count: int = 10,
    poll_updates: bool = True,
) -> None:
    application = (
        Application.builder()
        .token(get_setting("TELEGRAM_BOT_TOKEN"))
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(60.0)
        .pool_timeout(30.0)
        .get_updates_read_timeout(30.0)
        .build()
    )
    register_approval_handler(application)
    await application.initialize()
    await register_bot_commands(application.bot)
    await application.start()
    if poll_updates:
        if application.updater is None:
            raise RuntimeError("Telegram polling updater is unavailable.")
        await application.updater.start_polling()
    scheduler: AsyncIOScheduler | None = None
    skipped_after_failure: set[str] = set()
    stop_event = asyncio.Event()
    fatal_auth_error: list[FlowAuthError] = []
    try:
        if resend_saved_on_start:
            count = await send_existing_drafts(application.bot)
            LOGGER.info("Re-sent %s saved draft idea(s) to Telegram.", count)
        if generate_on_start:
            await generate_and_send_fn(
                application.bot,
                prompt=idea_prompt,
                scene_count=scene_count,
                duration_seconds=duration_seconds,
                ideas_count=ideas_count,
            )
        if process_on_start:
            await process_queue_fn(application.bot, publish=publish_approved)

        has_schedule = (
            (schedule_mode == "interval" and interval_minutes is not None)
            or (
                schedule_mode == "datetime"
                and bool(schedule_datetime and schedule_datetime.strip())
            )
            or (schedule_mode == "daily" and bool(schedule_time and schedule_time.strip()))
            or interval_minutes is not None
        )
        if has_schedule or process_approved:
            scheduler = AsyncIOScheduler()
            if schedule_mode == "datetime" and schedule_datetime and schedule_datetime.strip():
                try:
                    raw_dt = schedule_datetime.strip()
                    run_dt = datetime.fromisoformat(raw_dt)
                    if run_dt.tzinfo is None:
                        run_dt = run_dt.astimezone()
                    scheduler.add_job(
                        _scheduled_generate,
                        "date",
                        run_date=run_dt,
                        args=[
                            application.bot,
                            idea_prompt,
                            scene_count,
                            duration_seconds,
                            ideas_count,
                            generate_and_send_fn,
                        ],
                        id="generate_ideas_date",
                        max_instances=1,
                        coalesce=True,
                    )
                    LOGGER.info(
                        "📅 Đã đặt lịch chạy Flow vào ngày giờ cụ thể: %s (theo giờ địa phương)",
                        run_dt.strftime("%d/%m/%Y %H:%M"),
                    )
                except Exception as error:
                    LOGGER.error(
                        "Không thể lên lịch theo ngày giờ '%s': %s",
                        schedule_datetime,
                        error,
                    )
            elif schedule_mode == "daily" and schedule_time and schedule_time.strip():
                try:
                    parts = schedule_time.strip().split(":")
                    hour = int(parts[0])
                    minute = int(parts[1]) if len(parts) > 1 else 0
                    scheduler.add_job(
                        _scheduled_generate,
                        "cron",
                        hour=hour,
                        minute=minute,
                        args=[
                            application.bot,
                            idea_prompt,
                            scene_count,
                            duration_seconds,
                            ideas_count,
                            generate_and_send_fn,
                        ],
                        id="generate_ideas_daily",
                        max_instances=1,
                        coalesce=True,
                    )
                    LOGGER.info(
                        "⏰ Đã đặt lịch chạy Flow cố định hàng ngày vào lúc: %02d:%02d",
                        hour,
                        minute,
                    )
                except Exception as error:
                    LOGGER.error(
                        "Không thể lên lịch hàng ngày vào lúc '%s': %s",
                        schedule_time,
                        error,
                    )
            elif interval_minutes is not None:
                scheduler.add_job(
                    _scheduled_generate,
                    "interval",
                    minutes=interval_minutes,
                    args=[
                        application.bot,
                        idea_prompt,
                        scene_count,
                        duration_seconds,
                        ideas_count,
                        generate_and_send_fn,
                    ],
                    id="generate_ideas_interval",
                    max_instances=1,
                    coalesce=True,
                )
                LOGGER.info(
                    "⏱️ Đã đặt lịch chạy Flow lặp lại định kỳ mỗi %d phút (%.1f giờ)",
                    interval_minutes,
                    interval_minutes / 60,
                )

            if process_approved or has_schedule:
                scheduler.add_job(
                    _scheduled_process,
                    "interval",
                    seconds=5 if process_approved else 60,
                    args=[
                        application.bot,
                        publish_approved,
                        skipped_after_failure,
                        stop_event,
                        fatal_auth_error,
                        process_queue_fn,
                    ],
                    id="process_approved_queue",
                    max_instances=1,
                    coalesce=True,
                )
            scheduler.start()
        if not poll_updates and not has_schedule and not process_approved:
            raise ValueError(
                "A listener without Telegram polling requires a scheduler or queue processor."
            )
        if poll_updates and not generate_on_start and not process_on_start and not has_schedule:
            if process_approved:
                LOGGER.info(
                    "Listening for Telegram approvals; approved videos will be sent back to Telegram."
                )
            else:
                LOGGER.info("Telegram approval listener is running.")
        await stop_event.wait()
        if fatal_auth_error:
            raise fatal_auth_error[0]
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if poll_updates and application.updater is not None:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def _scheduled_generate(
    bot: Bot | None,
    prompt: str | None,
    scene_count: int,
    duration_seconds: int,
    ideas_count: int,
    generate_and_send_fn: GenerateAndSend,
) -> None:
    try:
        await generate_and_send_fn(
            bot=bot,
            prompt=prompt,
            scene_count=scene_count,
            duration_seconds=duration_seconds,
            ideas_count=ideas_count,
        )
    except Exception:
        LOGGER.exception("Scheduled idea generation failed.")


async def _scheduled_process(
    bot: Bot | None,
    publish: bool,
    skip_failed_ids: set[str],
    stop_event: asyncio.Event,
    fatal_auth_error: list[FlowAuthError],
    process_queue_fn: ProcessQueue,
) -> None:
    try:
        await process_queue_fn(
            bot,
            publish=publish,
            skip_failed_ids=skip_failed_ids,
        )
    except FlowAuthError as auth_error:
        LOGGER.error(
            "⛔ Dừng tiến trình listener/workflow do lỗi xác thực Google Flow. Hãy chạy save_flow_auth.py để đăng nhập lại."
        )
        fatal_auth_error.append(auth_error)
        stop_event.set()
    except Exception:
        LOGGER.exception("Scheduled queue processing failed.")
