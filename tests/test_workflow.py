from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from filelock import FileLock

import main
from core.tele import listener as telegram_listener


def _run_coroutine(coro):
    while True:
        try:
            coro.send(None)
        except StopIteration as exc:
            return exc.value


class ApprovalVideoTests(unittest.TestCase):
    def test_only_one_process_can_start_telegram_polling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "telegram_poll.lock"
            with (
                patch.object(telegram_listener, "TELEGRAM_POLL_LOCK_FILE", lock_path),
                patch.object(telegram_listener, "Application") as application,
                FileLock(str(lock_path), timeout=0),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "already polling Telegram"
                ):
                    _run_coroutine(main._run_telegram_listener(False, False, None))

        application.builder.assert_not_called()

    def test_failed_approved_item_is_not_retried_by_polling_listener(self) -> None:
        item = {"id": "draft-1", "idea": {"title": "Approved", "scenes": []}}
        bot = Mock()
        failed_ids: set[str] = set()
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(main, "PROCESS_LOCK_FILE", Path(temporary) / "lock"),
                patch("main.read_approved_queue", return_value=[item]),
                patch(
                    "main._process_one",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("video could not be uploaded"),
                ) as process,
                patch(
                    "main._notify_processing_failure", new_callable=AsyncMock
                ) as notify,
            ):
                with self.assertRaisesRegex(RuntimeError, "1 approved item"):
                    _run_coroutine(
                        main.process_queue(
                            bot,
                            publish=False,
                            skip_failed_ids=failed_ids,
                        )
                    )
                _run_coroutine(
                    main.process_queue(
                        bot,
                        publish=False,
                        skip_failed_ids=failed_ids,
                    )
                )

        self.assertEqual(failed_ids, {"draft-1"})
        process.assert_awaited_once()
        notify.assert_awaited_once_with("draft-1", bot)

    def test_approved_video_is_sent_to_telegram_without_social_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "approved.mp4"
            video.write_bytes(b"mp4")
            bot = Mock()
            idea = {"title": "Approved", "scenes": []}
            with (
                patch("main.generate_video", new_callable=AsyncMock, return_value=video),
                patch("main.send_video_to_telegram", new_callable=AsyncMock) as send,
                patch("main.acknowledge_processed") as acknowledge,
                patch("main.publish_to_social") as publish,
                patch("asyncio.to_thread", side_effect=lambda func, *args, **kwargs: func(*args, **kwargs)),
            ):
                _run_coroutine(
                    main._process_one(
                        {"id": "draft-1", "idea": idea},
                        bot,
                        publish=False,
                    )
                )

        send.assert_awaited_once_with(str(video), "Approved", bot)
        acknowledge.assert_called_once_with("draft-1")
        publish.assert_not_called()

    def test_run_workflow_executes_connected_nodes_in_order(self) -> None:
        order: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            status_file = Path(temporary) / "workflow_status.json"
            generate = AsyncMock(side_effect=lambda **kwargs: order.append("generate"))
            listener = AsyncMock(
                side_effect=lambda *args, **kwargs: order.append("approval-video")
            )
            with (
                patch.object(main, "WORKFLOW_STATUS_FILE", status_file),
                patch.object(main, "generate_and_send", generate),
                patch.object(main, "_run_telegram_listener", listener),
            ):
                _run_coroutine(
                    main._run_workflow(
                        [
                            {"id": "ideas", "type": "generate-ideas"},
                            {"id": "approval", "type": "approval-video"},
                        ],
                        "A story",
                        4,
                        20,
                    )
                )
            status = json.loads(status_file.read_text(encoding="utf-8"))

        self.assertEqual(order, ["generate", "approval-video"])
        self.assertFalse(status["running"])
        self.assertEqual(status["completed_nodes"], ["ideas", "approval"])
        generate.assert_awaited_once_with(
            prompt="A story",
            scene_count=4,
            duration_seconds=20,
            ideas_count=10,
        )
        listener.assert_awaited_once_with(
            False,
            False,
            None,
            process_approved=True,
            publish_approved=False,
            idea_prompt="A story",
            scene_count=4,
            duration_seconds=20,
            schedule_mode="interval",
            schedule_datetime=None,
            schedule_time=None,
            ideas_count=10,
            poll_updates=True,
        )

    def test_run_workflow_uses_dashboard_managed_listener(self) -> None:
        listener = AsyncMock()
        with tempfile.TemporaryDirectory() as temporary:
            status_file = Path(temporary) / "workflow_status.json"
            with (
                patch.object(main, "WORKFLOW_STATUS_FILE", status_file),
                patch.object(main, "_run_telegram_listener", listener),
            ):
                _run_coroutine(
                    main._run_workflow(
                        [{"id": "approval", "type": "approval-video"}],
                        None,
                        3,
                        10,
                        external_telegram_listener=True,
                    )
                )

        listener.assert_awaited_once()
        self.assertFalse(listener.await_args.kwargs["poll_updates"])


if __name__ == "__main__":
    unittest.main()
