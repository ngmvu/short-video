from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

import web_app


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env_file = self.root / ".env"
        self.env_file.write_text(
            'TELEGRAM_BOT_TOKEN="hidden-test-token"\n'
            "TELEGRAM_CHAT_ID=12345\n"
            "GEMINI_MODEL=gemini-test-model\n"
            "FLOW_PROJECT_URL=https://example.test/flow\n",
            encoding="utf-8",
        )
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.storage = self.root / "storage"
        self.storage.mkdir()
        (self.root / "auth.json").write_text("{}", encoding="utf-8")
        for key in web_app.FLOW_SELECTOR_KEYS:
            if key == "FLOW_SETTINGS_SELECTOR":
                selector = 'button[aria-label="Settings trigger"]'
            elif key == "FLOW_VIDEO_MODE_SELECTOR":
                selector = 'button[role="radio"]:has-text("Video")'
            elif key == "FLOW_ASPECT_RATIO_SELECTOR":
                selector = 'button[role="radio"]:has-text("9:16")'
            elif key == "FLOW_DURATION_SELECTOR":
                selector = 'button[role="radio"]:has-text("10s")'
            elif key == "FLOW_OUTPUT_COUNT_SELECTOR":
                selector = 'button[role="radio"]:has-text("x1")'
            elif key == "FLOW_PROMPT_SELECTOR":
                selector = (
                    '[contenteditable="true"]'
                    '[aria-label="What do you want to create?"]'
                )
            elif key == "FLOW_SUBMIT_SELECTOR":
                selector = 'button[aria-label="Start generation"]'
            elif key == "FLOW_RESULT_SELECTOR":
                selector = 'flow-grid-tile-container[role="group"]'
            elif key == "FLOW_DOWNLOAD_MENU_SELECTOR":
                selector = 'button[aria-label="More options"]'
            else:
                selector = '[role="menuitem"]:has-text("720p Original size")'
            self.env_file.write_text(
                self.env_file.read_text(encoding="utf-8")
                + f"{key}={json.dumps(selector)}\n",
                encoding="utf-8",
            )
        (self.storage / "queue_approved.json").write_text(
            json.dumps(
                [
                    {
                        "id": "test-item",
                        "idea": {"title": "Approved idea"},
                        "approved_at": "2026-10-30",
                    }
                ]
            ),
            encoding="utf-8",
        )
        self.patches = [
            patch.object(web_app, "ENV_FILE", self.env_file),
            patch.object(web_app, "DOWNLOADS_DIR", self.downloads),
            patch.object(web_app, "STORAGE_DIR", self.storage),
            patch.object(web_app, "ROOT_DIR", self.root),
            patch.object(web_app, "LOG_FILE", self.storage / "dashboard.log"),
            patch.object(web_app, "LOG_BACKUP_FILE", self.storage / "dashboard.log.1"),
            patch.object(web_app, "FLOW_STATUS_FILE", self.storage / "workflow_status.json"),
        ]
        for item in self.patches:
            item.start()
        web_app.app.config.update(TESTING=True, SECRET_KEY="dashboard-test-secret")
        self.client = web_app.app.test_client()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def csrf_token(self) -> str:
        response = self.client.get("/")
        match = re.search(
            rb'name="csrf_token" value="([^"]+)"', response.data
        )
        self.assertIsNotNone(match)
        return match.group(1).decode("ascii")

    def test_dashboard_main_starts_and_stops_its_telegram_listener(self) -> None:
        events: list[str] = []
        with (
            patch.object(
                web_app, "_start_telegram_listener",
                side_effect=lambda: events.append("listener-start"),
            ),
            patch.object(
                web_app, "_stop_telegram_listener",
                side_effect=lambda: events.append("listener-stop"),
            ),
            patch.object(
                web_app.app,
                "run",
                side_effect=lambda **kwargs: events.append("dashboard-run"),
            ),
            patch.dict("os.environ", {"DASHBOARD_PORT": "5010"}),
        ):
            web_app.main()

        self.assertEqual(
            events,
            ["listener-start", "dashboard-run", "listener-stop"],
        )

    def test_dashboard_starts_listener_as_managed_poll_process(self) -> None:
        class FakeProcess:
            pid = 4321
            returncode = None

            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / ".env"
            env_file.write_text(
                "TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_CHAT_ID=12345\n",
                encoding="utf-8",
            )
            storage = root / "storage"
            log_file = storage / "dashboard.log"
            with (
                patch.object(web_app, "_telegram_listener_process", None),
                patch.object(web_app, "ENV_FILE", env_file),
                patch.object(web_app, "ROOT_DIR", root),
                patch.object(web_app, "STORAGE_DIR", storage),
                patch.object(web_app, "LOG_FILE", log_file),
                patch.object(
                    web_app, "TELEGRAM_POLL_LOCK_FILE",
                    storage / "telegram_poll.lock",
                ),
                patch.object(web_app.subprocess, "Popen", return_value=FakeProcess()) as popen,
                patch.object(web_app.time, "sleep"),
            ):
                self.assertTrue(web_app._start_telegram_listener())

        command = popen.call_args.args[0]
        self.assertEqual(command[-2:], [str(root / "main.py"), "--telegram-poll"])
        self.assertEqual(popen.call_args.kwargs["env"]["TELEGRAM_CHAT_ID"], "12345")

    def test_dashboard_workflow_uses_the_dashboard_listener(self) -> None:
        class FakeProcess:
            pid = 4322

            def poll(self):
                return None

        with (
            patch.object(web_app, "_managed_process", None),
            patch.object(web_app, "_cleanup_orphan_workers"),
            patch.object(web_app.subprocess, "Popen", return_value=FakeProcess()) as popen,
        ):
            started, _ = web_app._launch_process(
                "run-flow",
                ["--flow-nodes-json", "[]"],
            )

        self.assertTrue(started)
        self.assertIn("--external-telegram-listener", popen.call_args.args[0])

    def test_dashboard_hides_secrets_and_lists_video_and_queue(self) -> None:
        (self.downloads / "rendered.mp4").write_bytes(b"mp4")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"hidden-test-token", response.data)
        self.assertIn(b"Workflow editor", response.data)
        self.assertIn(b'data-node-type="process-queue"', response.data)
        self.assertIn(b'name="prompt"', response.data)
        self.assertIn(b'name="scene_count"', response.data)
        self.assertIn(b'name="duration_seconds"', response.data)
        self.assertNotIn("Log gần đây".encode(), response.data)
        self.assertIn("Quản lý log".encode(), response.data)
        self.assertIn("Xóa log đã lưu".encode(), response.data)
        self.assertIn(b"rendered", response.data)
        self.assertIn(b"Approved idea", response.data)
        self.assertIn(b'data-dashboard-nav="settings-google-flow"', response.data)
        self.assertIn(b'data-dashboard-page="settings-google-flow"', response.data)

    def test_dashboard_loads_separate_static_assets(self) -> None:
        css = self.client.get("/static/dashboard.css")
        javascript = self.client.get("/static/dashboard.js")

        self.assertEqual(css.status_code, 200)
        self.assertEqual(javascript.status_code, 200)
        self.assertIn(b".sidebar", css.data)
        self.assertIn(b"activateDashboardPage", javascript.data)
        self.assertNotIn(b"{{ url_for", javascript.data)
        css.close()
        javascript.close()

    def test_save_config_keeps_blank_secret_and_updates_normal_field(self) -> None:
        response = self.client.post(
            "/config",
            data={
                "csrf_token": self.csrf_token(),
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_CHAT_ID": "67890",
                "IDEA_STYLE": "Đổi vai và hiểu lầm",
            },
        )
        self.assertEqual(response.status_code, 302)
        saved = dotenv_values(self.env_file)
        self.assertEqual(saved["TELEGRAM_BOT_TOKEN"], "hidden-test-token")
        self.assertEqual(saved["TELEGRAM_CHAT_ID"], "67890")
        self.assertEqual(saved["IDEA_STYLE"], "Đổi vai và hiểu lầm")
        self.assertEqual(saved["GEMINI_MODEL"], "gemini-test-model")

    def test_partial_config_save_keeps_other_settings_and_returns_to_sidebar_page(self) -> None:
        response = self.client.post(
            "/config",
            data={
                "csrf_token": self.csrf_token(),
                "settings_page": "settings-telegram",
                "TELEGRAM_CHAT_ID": "99999",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/#settings-telegram")
        saved = dotenv_values(self.env_file)
        self.assertEqual(saved["TELEGRAM_CHAT_ID"], "99999")
        self.assertEqual(saved["GEMINI_MODEL"], "gemini-test-model")
        self.assertEqual(saved["FLOW_PROJECT_URL"], "https://example.test/flow")

    def test_post_without_csrf_is_rejected(self) -> None:
        response = self.client.post("/stop")
        self.assertEqual(response.status_code, 400)

    def test_clear_logs_removes_current_and_rotated_log_files(self) -> None:
        web_app.LOG_FILE.write_text("current log", encoding="utf-8")
        web_app.LOG_BACKUP_FILE.write_text("rotated log", encoding="utf-8")

        response = self.client.post(
            "/logs/clear",
            data={"csrf_token": self.csrf_token()},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(web_app.LOG_FILE.exists())
        self.assertFalse(web_app.LOG_BACKUP_FILE.exists())

    def test_clear_logs_is_rejected_while_workflow_is_running(self) -> None:
        web_app.LOG_FILE.write_text("keep this log", encoding="utf-8")
        running_process = type("RunningProcess", (), {"poll": lambda self: None})()
        csrf_token = self.csrf_token()

        with patch.object(web_app, "_managed_process", running_process):
            response = self.client.post(
                "/logs/clear",
                data={"csrf_token": csrf_token},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(web_app.LOG_FILE.read_text(encoding="utf-8"), "keep this log")

    def test_dashboard_rejects_non_local_client(self) -> None:
        response = self.client.get("/", environ_base={"REMOTE_ADDR": "192.168.1.50"})
        self.assertEqual(response.status_code, 403)

    def test_video_route_rejects_path_traversal(self) -> None:
        response = self.client.get("/videos/..%2F.env")
        self.assertEqual(response.status_code, 404)

    def test_flow_status_reports_verified_selectors_and_auth(self) -> None:
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        flow = response.get_json()["flow"]
        self.assertTrue(flow["configured"])
        self.assertTrue(flow["auth_exists"])
        self.assertEqual(flow["missing_selectors"], [])

    def test_legacy_finalize_selector_is_not_reported_as_missing(self) -> None:
        with patch.object(
            web_app,
            "FLOW_SELECTOR_KEYS",
            (*web_app.FLOW_SELECTOR_KEYS, "FLOW_FINALIZE_SELECTOR"),
        ):
            response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        flow = response.get_json()["flow"]
        self.assertNotIn("FLOW_FINALIZE_SELECTOR", flow["missing_selectors"])
        self.assertTrue(flow["configured"])

    def test_running_workflow_node_starts_its_matching_mode(self) -> None:
        with (
            patch.object(
                web_app, "_launch_process", return_value=(True, "Started")
            ) as launch,
            patch.object(
                web_app, "_process_status", return_value={"running": True}
            ),
        ):
            response = self.client.post(
                "/api/workflow/nodes/process-queue/run",
                data={"csrf_token": self.csrf_token()},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["message"], "Started")
        launch.assert_called_once_with("process-queue")

    def test_running_workflow_node_automation_supports_datetime_mode(self) -> None:
        with (
            patch.object(
                web_app, "_launch_process", return_value=(True, "Started")
            ) as launch,
            patch.object(
                web_app, "_process_status", return_value={"running": True}
            ),
        ):
            response = self.client.post(
                "/api/workflow/nodes/automation/run",
                data={
                    "csrf_token": self.csrf_token(),
                    "schedule_mode": "datetime",
                    "schedule_datetime": "2026-11-02T18:30",
                },
            )

        self.assertEqual(response.status_code, 202)
        launch.assert_called_once_with(
            "auto",
            extra_args=["--schedule-mode", "datetime", "--schedule-datetime", "2026-11-02T18:30"],
        )

    def test_running_workflow_node_automation_supports_daily_mode(self) -> None:
        with (
            patch.object(
                web_app, "_launch_process", return_value=(True, "Started")
            ) as launch,
            patch.object(
                web_app, "_process_status", return_value={"running": True}
            ),
        ):
            response = self.client.post(
                "/api/workflow/nodes/automation/run",
                data={
                    "csrf_token": self.csrf_token(),
                    "schedule_mode": "daily",
                    "schedule_time": "19:45",
                },
            )

        self.assertEqual(response.status_code, 202)
        launch.assert_called_once_with(
            "auto",
            extra_args=["--schedule-mode", "daily", "--schedule-time", "19:45"],
        )

    def test_run_flow_orders_nodes_by_connections(self) -> None:
        nodes = [
            {"id": "approval", "type": "approval-video"},
            {"id": "ideas", "type": "generate-ideas"},
        ]
        edges = [{"from": "ideas", "to": "approval"}]
        with (
            patch.object(
                web_app, "_launch_process", return_value=(True, "Started")
            ) as launch,
            patch.object(
                web_app, "_process_status", return_value={"running": True}
            ),
        ):
            response = self.client.post(
                "/api/workflow/run",
                data={
                    "csrf_token": self.csrf_token(),
                    "nodes": json.dumps(nodes),
                    "edges": json.dumps(edges),
                    "prompt": "A story",
                    "scene_count": "4",
                    "duration_seconds": "20",
                },
            )

        self.assertEqual(response.status_code, 202)
        launch_args = launch.call_args.args
        self.assertEqual(launch_args[0], "run-flow")
        serialized_nodes = launch_args[1][launch_args[1].index("--flow-nodes-json") + 1]
        self.assertEqual(
            json.loads(serialized_nodes),
            [
                {"id": "ideas", "type": "generate-ideas"},
                {"id": "approval", "type": "approval-video"},
            ],
        )
        self.assertTrue(web_app.FLOW_STATUS_FILE.is_file())

    def test_run_flow_passes_schedule_datetime_from_automation_node(self) -> None:
        nodes = [
            {"id": "ideas", "type": "generate-ideas"},
            {"id": "approval", "type": "approval-video"},
            {
                "id": "auto",
                "type": "automation",
                "schedule_mode": "datetime",
                "schedule_datetime": "2026-11-05T14:30",
            },
        ]
        edges = [
            {"from": "ideas", "to": "approval"},
            {"from": "approval", "to": "auto"},
        ]
        with (
            patch.object(
                web_app, "_launch_process", return_value=(True, "Started")
            ) as launch,
            patch.object(
                web_app, "_process_status", return_value={"running": True}
            ),
        ):
            response = self.client.post(
                "/api/workflow/run",
                data={
                    "csrf_token": self.csrf_token(),
                    "nodes": json.dumps(nodes),
                    "edges": json.dumps(edges),
                },
            )

        self.assertEqual(response.status_code, 202)
        launch_args = launch.call_args.args
        self.assertEqual(launch_args[0], "run-flow")
        args_list = launch_args[1]
        self.assertIn("--schedule-mode", args_list)
        self.assertEqual(args_list[args_list.index("--schedule-mode") + 1], "datetime")
        self.assertIn("--schedule-datetime", args_list)
        self.assertEqual(args_list[args_list.index("--schedule-datetime") + 1], "2026-11-05T14:30")

    def test_run_flow_rejects_cycles_and_non_terminal_listener(self) -> None:
        csrf_token = self.csrf_token()
        cycle = self.client.post(
            "/api/workflow/run",
            data={
                "csrf_token": csrf_token,
                "nodes": json.dumps(
                    [
                        {"id": "ideas", "type": "generate-ideas"},
                        {"id": "approval", "type": "approval-video"},
                    ]
                ),
                "edges": json.dumps(
                    [
                        {"from": "ideas", "to": "approval"},
                        {"from": "approval", "to": "ideas"},
                    ]
                ),
            },
        )
        blocked = self.client.post(
            "/api/workflow/run",
            data={
                "csrf_token": csrf_token,
                "nodes": json.dumps(
                    [
                        {"id": "approval", "type": "approval-video"},
                        {"id": "ideas", "type": "generate-ideas"},
                    ]
                ),
                "edges": json.dumps([{"from": "approval", "to": "ideas"}]),
            },
        )

        self.assertEqual(cycle.status_code, 400)
        self.assertIn("vòng", cycle.get_json()["error"])
        self.assertEqual(blocked.status_code, 400)

    def test_running_workflow_node_reports_launch_conflict(self) -> None:
        with patch.object(
            web_app, "_launch_process", return_value=(False, "Already running")
        ):
            response = self.client.post(
                "/api/workflow/nodes/automation/run",
                data={"csrf_token": self.csrf_token()},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "Already running")

    def test_custom_idea_request_passes_prompt_and_options_to_process(self) -> None:
        with (
            patch.object(
                web_app, "_launch_process", return_value=(True, "Started")
            ) as launch,
            patch.object(
                web_app, "_process_status", return_value={"running": True}
            ),
        ):
            response = self.client.post(
                "/api/ideas/run",
                data={
                    "csrf_token": self.csrf_token(),
                    "prompt": "Thỏ thi nấu ăn",
                    "scene_count": "5",
                    "duration_seconds": "20",
                },
            )

        self.assertEqual(response.status_code, 202)
        launch.assert_called_once_with(
            "generate-ideas",
            [
                "--idea-prompt",
                "Thỏ thi nấu ăn",
                "--scene-count",
                "5",
                "--duration-seconds",
                "20",
                "--ideas-count",
                "10",
            ],
        )

    def test_custom_idea_request_rejects_empty_prompt_and_out_of_range(self) -> None:
        csrf_token = self.csrf_token()
        empty_prompt = self.client.post(
            "/api/ideas/run",
            data={"csrf_token": csrf_token, "prompt": " ", "scene_count": "3"},
        )
        too_many_scenes = self.client.post(
            "/api/ideas/run",
            data={
                "csrf_token": csrf_token,
                "prompt": "A story",
                "scene_count": "21",
                "duration_seconds": "10",
            },
        )

        self.assertEqual(empty_prompt.status_code, 400)
        self.assertEqual(too_many_scenes.status_code, 400)

    def test_workflow_node_rejects_unknown_action(self) -> None:
        response = self.client.post(
            "/api/workflow/nodes/not-a-node/run",
            data={"csrf_token": self.csrf_token()},
        )

        self.assertEqual(response.status_code, 404)

    def test_log_redaction_hides_secret_and_access_token(self) -> None:
        redacted = web_app._redact_logs(
            "hidden-test-token access_token=another-secret "
            "https://example.test?access_token=query-secret"
        )
        self.assertNotIn("hidden-test-token", redacted)
        self.assertNotIn("another-secret", redacted)
        self.assertNotIn("query-secret", redacted)

    def test_workflow_page_renders_live_log_and_saved_drafts(self) -> None:
        (self.storage / "drafts.json").write_text(
            json.dumps(
                [
                    {
                        "id": "draft-123",
                        "idea": {"title": "Saved idea without video"},
                        "state": "pending",
                        "created_at": "2026-10-31T08:11:42+00:00",
                    }
                ]
            ),
            encoding="utf-8",
        )
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="workflow-log-output"', response.data)
        self.assertIn(b"Saved idea without video", response.data)
        self.assertIn(b"js-draft-produce-telegram", response.data)

    def test_saved_draft_can_be_queued_and_produced_for_telegram(self) -> None:
        import core.storage as storage_mod

        drafts_file = self.storage / "drafts.json"
        queue_file = self.storage / "queue_approved.json"
        drafts_file.write_text(
            json.dumps(
                [
                    {
                        "id": "draft-abc",
                        "idea": {"title": "Reusable idea"},
                        "state": "approved",
                        "created_at": "2026-10-31T08:11:42+00:00",
                    }
                ]
            ),
            encoding="utf-8",
        )
        queue_file.write_text("[]", encoding="utf-8")
        csrf_token = self.csrf_token()

        with (
            patch.object(storage_mod, "DRAFTS_FILE", drafts_file),
            patch.object(storage_mod, "APPROVED_QUEUE_FILE", queue_file),
            patch.object(
                web_app, "_launch_process", return_value=(True, "Started")
            ) as launch,
            patch.object(
                web_app, "_process_status", return_value={"running": True}
            ),
        ):
            response = self.client.post(
                "/api/drafts/draft-abc/produce-telegram",
                data={"csrf_token": csrf_token},
            )

        self.assertEqual(response.status_code, 202)
        launch.assert_called_once_with("produce-telegram")
        queued = json.loads(queue_file.read_text(encoding="utf-8"))
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["id"], "draft-abc")

    def test_import_video_uploads_file_to_downloads(self) -> None:
        csrf_token = self.csrf_token()
        response = self.client.post(
            "/api/videos/import",
            data={
                "csrf_token": csrf_token,
                "video_file": (io.BytesIO(b"fake-mp4-video-content"), "clip_suaxe.mp4"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["video"]["name"], "clip_suaxe.mp4")
        self.assertTrue((self.downloads / "clip_suaxe.mp4").is_file())

    def test_publish_video_multiplatform_uses_per_platform_captions_and_hashtags(
        self,
    ) -> None:
        import core.social_uploader as uploader_mod

        video_file = self.downloads / "ready_clip.mp4"
        video_file.write_bytes(b"mp4-binary-data")
        csrf_token = self.csrf_token()

        with (
            patch.object(
                uploader_mod,
                "_tiktok_upload",
                return_value={"success": True, "id": "tt-123", "url": None},
            ) as mock_tt,
            patch.object(
                uploader_mod,
                "_facebook_upload",
                return_value={
                    "success": True,
                    "id": "fb-456",
                    "url": "https://facebook.com/reel/456",
                },
            ) as mock_fb,
            patch.object(
                uploader_mod,
                "_youtube_upload",
                return_value={
                    "success": True,
                    "id": "yt-789",
                    "url": "https://www.youtube.com/shorts/yt-789",
                },
            ) as mock_yt,
        ):
            response = self.client.post(
                "/api/videos/publish",
                data={
                    "csrf_token": csrf_token,
                    "video_name": "ready_clip.mp4",
                    "general_title": "Thắng Kêu Tra Dầu Ăn",
                    "tiktok_enabled": "1",
                    "tiktok_title": "Hook TikTok cực gắt",
                    "tiktok_caption": "Caption riêng cho TikTok",
                    "tiktok_hashtags": "#xuhuong #tiktokvn",
                    "facebook_enabled": "1",
                    "facebook_title": "Tiêu đề Reels FB",
                    "facebook_caption": "Caption riêng cho Fanpage Facebook",
                    "facebook_hashtags": "#reelsvn #suaxe",
                    "youtube_enabled": "1",
                    "youtube_title": "Thắng Kêu Tra Dầu Ăn Shorts",
                    "youtube_caption": "Mô tả riêng cho YouTube Shorts",
                    "youtube_hashtags": "#Shorts #haihuoc",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        report = payload["report"]
        self.assertTrue(report["statuses"]["tiktok"]["success"])
        self.assertTrue(report["statuses"]["facebook"]["success"])
        self.assertTrue(report["statuses"]["youtube"]["success"])

        # Verify TikTok received its own title, caption, and hashtags
        tt_args = mock_tt.call_args.args
        self.assertEqual(tt_args[1], "Hook TikTok cực gắt")
        self.assertIn("Caption riêng cho TikTok", tt_args[2])
        self.assertIn("#xuhuong #tiktokvn", tt_args[2])

        # Verify Facebook received its own title, caption, and hashtags
        fb_args = mock_fb.call_args.args
        self.assertEqual(fb_args[1], "Tiêu đề Reels FB")
        self.assertIn("Caption riêng cho Fanpage Facebook", fb_args[2])
        self.assertIn("#reelsvn #suaxe", fb_args[2])

        # Verify YouTube received its own title (with hashtags attached), caption, and tags
        yt_args = mock_yt.call_args.args
        self.assertEqual(yt_args[1], "Thắng Kêu Tra Dầu Ăn Shorts #Shorts #haihuoc")
        self.assertEqual(yt_args[2], "Mô tả riêng cho YouTube Shorts")
        self.assertEqual(yt_args[3], ["Shorts", "haihuoc"])

        # Verify report was saved to publish_reports.json
        saved_reports = json.loads(
            (self.storage / "publish_reports.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(saved_reports), 1)
        self.assertEqual(
            saved_reports[0]["platform_configs"]["tiktok"]["hashtags"],
            ["#xuhuong", "#tiktokvn"],
        )

    def test_publish_video_skips_unchecked_platforms(self) -> None:
        import core.social_uploader as uploader_mod

        csrf_token = self.csrf_token()
        with (
            patch.object(
                uploader_mod,
                "_tiktok_upload",
                return_value={"success": True, "id": "tt-only"},
            ) as mock_tt,
            patch.object(uploader_mod, "_facebook_upload") as mock_fb,
            patch.object(uploader_mod, "_youtube_upload") as mock_yt,
        ):
            response = self.client.post(
                "/api/videos/publish",
                data={
                    "csrf_token": csrf_token,
                    "video_file": (io.BytesIO(b"uploaded-mp4"), "upload_direct.mp4"),
                    "tiktok_enabled": "1",
                    "tiktok_caption": "Chỉ đăng TikTok thôi",
                    "tiktok_hashtags": "tiktokonly, viral",
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        statuses = response.get_json()["report"]["statuses"]
        self.assertTrue(statuses["tiktok"]["success"])
        self.assertTrue(statuses["facebook"]["skipped"])
        self.assertTrue(statuses["youtube"]["skipped"])
        mock_tt.assert_called_once()
        mock_fb.assert_not_called()
        mock_yt.assert_not_called()

    def test_import_video_auto_generates_gemini_captions_and_saves_metadata(
        self,
    ) -> None:
        csrf_token = self.csrf_token()
        fake_ai_posts = {
            "general_title": "Thắng Kêu Tự Tra Dầu Ăn",
            "tiktok": {
                "title": "TikTok AI Title",
                "caption": "TikTok AI Caption",
                "hashtags": "#xuhuong #tiktokvn",
            },
            "facebook": {
                "title": "Facebook AI Title",
                "caption": "Facebook AI Caption",
                "hashtags": "#reelsvn #facebookreels",
            },
            "youtube": {
                "title": "YouTube AI Title #Shorts",
                "caption": "YouTube AI Description",
                "hashtags": "#Shorts #youtubeshorts",
            },
        }
        with patch.object(
            web_app,
            "generate_social_posts_for_video",
            return_value=fake_ai_posts,
        ) as mock_gen:
            response = self.client.post(
                "/api/videos/import",
                data={
                    "csrf_token": csrf_token,
                    "video_file": (
                        io.BytesIO(b"fake-mp4-video-content"),
                        "thang_keu.mp4",
                    ),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertTrue(payload["ai_generated"])
        self.assertEqual(
            payload["social_posts"]["tiktok"]["caption"], "TikTok AI Caption"
        )
        self.assertEqual(
            payload["social_posts"]["facebook"]["caption"], "Facebook AI Caption"
        )
        self.assertEqual(
            payload["social_posts"]["youtube"]["caption"],
            "YouTube AI Description",
        )
        mock_gen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
