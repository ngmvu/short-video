from __future__ import annotations

import unittest

from core.tele.approval_bot import _format_idea


class ApprovalMessageTests(unittest.TestCase):
    def test_format_idea_escapes_parentheses_in_scene_label(self) -> None:
        idea = {
            "title": "Bất ngờ (ở chợ)",
            "character_1": {
                "ingredient": "@khi_tho",
                "image_prompt": "Thỏ (áo đỏ)",
            },
            "character_2": {
                "ingredient": "@khach",
                "image_prompt": "Người bạn",
            },
            "scenes": [
                {
                    "duration": "0-10s",
                    "prompt": "Cả hai (bất ngờ) bật cười.",
                    "type": "story",
                }
            ],
        }

        message = _format_idea(idea, 1)

        self.assertIn(r"Scene 1 \(0\-10s\)", message)
        self.assertIn(r"Bất ngờ \(ở chợ\)", message)

    def test_format_idea_preserves_script_and_shows_approved_badge(self) -> None:
        idea = {
            "title": "Cao Thủ Cờ Bàn Vỉa Hè",
            "character_1": {"ingredient": "@khi", "image_prompt": "Khỉ"},
            "character_2": {"ingredient": "@nguoi", "image_prompt": "Người"},
            "scenes": [
                {
                    "duration": "3s",
                    "prompt": "@khi stares at chess board.",
                    "type": "intro",
                }
            ],
        }

        message = _format_idea(
            idea,
            1,
            status_label="✅ ĐÃ APPROVE — Đang trong hàng đợi tạo video",
        )

        self.assertIn("Cao Thủ Cờ Bàn Vỉa Hè", message)
        self.assertIn("@khi stares at chess board", message)
        self.assertIn("ĐÃ APPROVE", message)

    def test_format_idea_omits_part_breakdowns_and_shows_clean_content(self) -> None:
        idea = {
            "title": "Vợ Giao Việc Nhà",
            "twist_highlight": "Tuyên bố nóc nhà nhưng nghe tiếng vợ quát là quỳ rạp.",
            "setting": "Phòng khách",
            "viral_quote": "Đàn ông tụi tao ở nhà là nóc nhà!",
            "scenes": [
                {
                    "duration": "auto",
                    "first_frame": "@nguoi + @Voice_Nguoi",
                    "dialogue": "Đàn ông tụi tao ở nhà là nóc nhà, vợ bảo một cấm cãi hai!",
                    "voice": "@Voice_Nguoi",
                    "prompt": "Medium two-shot. Speaker: @nguoi with @Voice_Nguoi...",
                },
                {
                    "duration": "auto",
                    "first_frame": "@khi + @Voice_Khi",
                    "dialogue": "Ghê vậy sao? Gọi tên vợ xem nào!",
                    "voice": "@Voice_Khi",
                    "prompt": "Medium two-shot. Speaker: @khi with @Voice_Khi...",
                },
            ],
            "overlay_text": "Khi đàn ông khoe nóc nhà...",
            "caption": "Caption dài dòng #hashtag",
            "engagement_prompt": "Bác nào sợ vợ giơ tay?",
        }

        message = _format_idea(idea, 1)

        # Ensure "PHẦN 1", "PHẦN 2", etc. are omitted for cleaner reading
        self.assertNotIn("PHẦN 1", message)
        self.assertNotIn("PHẦN 2", message)
        self.assertNotIn("PHẦN 3", message)
        self.assertNotIn("PHẦN 4", message)
        self.assertNotIn("PHẦN 5", message)
        self.assertNotIn("GOOGLE FLOW PROMPTS", message)

        # Ensure essential content is present
        self.assertIn("Vợ Giao Việc Nhà", message)
        self.assertIn("Cốt truyện", message)
        self.assertIn("Scene 1", message)
        self.assertIn("Scene 2", message)
        self.assertIn("Đàn ông tụi tao ở nhà là nóc nhà", message)
        self.assertIn("Ghê vậy sao", message)

    def test_script_parser_extracts_scenes_and_dialogues(self) -> None:
        from core.script_parser import looks_like_script, parse_script

        script_text = (
            "Tạo 3 video theo 3 Scene sau:\n"
            "🔥 KỊCH BẢN: Thầy Bói Xem Quẻ Bách Phát Bách Trúng\n"
            'Scene 1 — Input / First Frame: @nguoi + @Voice_Nguoi: Vertical 9:16. Speaker: @nguoi with @Voice_Nguoi: lip-syncing briskly: "Lá bài này báo hiệu bước lên mây xanh!" — finishes speaking completely.\n'
            'Scene 2 — Input / First Frame: @khi + @Voice_Khi: Vertical 9:16. Speaker: @khi with @Voice_Khi: lip-syncing briskly: "Thầy cầm ngược bài nãy giờ kìa!" — finishes speaking completely.\n'
            'Scene 3 — Input / First Frame: @nguoi + @Voice_Nguoi: Vertical 9:16. Speaker: @nguoi with @Voice_Nguoi: lip-syncing briskly: "Cầm ngược để giải phong thủy đảo chiều!" — finishes speaking.'
        )

        self.assertTrue(looks_like_script(script_text))
        idea = parse_script(
            script_text,
            characters=[("@khi", "@Voice_Khi"), ("@nguoi", "@Voice_Nguoi")],
        )

        self.assertEqual(idea["title"], "Thầy Bói Xem Quẻ Bách Phát Bách Trúng")
        self.assertEqual(len(idea["scenes"]), 3)
        self.assertEqual(
            idea["scenes"][0]["dialogue"],
            "Lá bài này báo hiệu bước lên mây xanh!",
        )
        self.assertEqual(idea["scenes"][0]["voice"], "@Voice_Nguoi")
        self.assertIn("@nguoi", idea["scenes"][0]["first_frame"])
        self.assertEqual(
            idea["scenes"][1]["dialogue"], "Thầy cầm ngược bài nãy giờ kìa!"
        )
        self.assertEqual(idea["scenes"][1]["voice"], "@Voice_Khi")
        self.assertEqual(
            idea["scenes"][2]["dialogue"],
            "Cầm ngược để giải phong thủy đảo chiều!",
        )

    def test_script_parser_rejects_missing_scene_headers(self) -> None:
        from core.script_parser import ScriptParseError, looks_like_script, parse_script

        self.assertFalse(looks_like_script("Chỉ là một câu nói bâng quơ"))
        with self.assertRaises(ScriptParseError):
            parse_script("Chỉ là một câu nói bâng quơ không có scene")


def _run_coroutine(coro: Any) -> Any:
    while True:
        try:
            coro.send(None)
        except StopIteration as exc:
            return exc.value


class TelegramInteractiveBotTests(unittest.TestCase):
    def test_choice_custom_situation_requests_user_input(self) -> None:
        from unittest.mock import AsyncMock, Mock, patch

        from core.tele.approval_bot import (
            _get_session,
            _reset_session,
            handle_approval_callback,
        )

        _reset_session("123456")
        query = AsyncMock()
        query.data = "menu:custom_situation"
        message = AsyncMock()
        message.chat_id = "123456"
        query.message = message
        update = Mock(callback_query=query)

        with patch("core.tele.approval_bot.get_setting", return_value="123456"):
            _run_coroutine(handle_approval_callback(update, Mock()))

        session = _get_session("123456")
        self.assertEqual(session["state"], "waiting_situation")
        self.assertEqual(session["brief_type"], "situation")
        self.assertIn("bán 1 con", message.reply_text.await_args.args[0])

    def test_situation_input_is_kept_as_the_core_brief(self) -> None:
        from unittest.mock import AsyncMock, Mock, patch

        from core.tele.approval_bot import _get_session, handle_text_message

        session = _get_session("123456")
        session.update(state="waiting_situation", brief_type="situation")
        message = AsyncMock()
        message.chat_id = "123456"
        message.text = "Khỉ bán gà, một con biết giá, hỏi hai con thì không biết."
        update = Mock(message=message)

        with patch("core.tele.approval_bot.get_setting", return_value="123456"):
            _run_coroutine(handle_text_message(update, Mock()))

        self.assertEqual(session["theme"], message.text)
        self.assertEqual(session["state"], "waiting_count")
        self.assertEqual(session["brief_type"], "situation")
        self.assertIn("giữ mấu chốt", message.reply_text.await_args.args[0])
        callbacks = [
            button.callback_data
            for row in message.reply_text.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            for button in row
        ]
        self.assertIn("gen_count:3", callbacks)

    def test_situation_mode_sends_editing_instructions_to_gemini(self) -> None:
        from unittest.mock import AsyncMock, Mock, patch

        from core.tele.approval_bot import handle_approval_callback

        query = AsyncMock()
        query.data = "gen_count:3"
        message = AsyncMock()
        message.chat_id = "123456"
        query.message = message
        update = Mock(callback_query=query)
        context = Mock(bot=object())
        session = {
            "theme": "Khỉ bán gà, một con biết giá, hỏi hai con thì không biết.",
            "brief_type": "situation",
        }
        generated: list[tuple[object, str, str, int]] = []

        async def capture_generation(
            passed_bot: object, chat_id: str, prompt: str, count: int
        ) -> None:
            generated.append((passed_bot, chat_id, prompt, count))

        with (
            patch("core.tele.approval_bot.get_setting", return_value="123456"),
            patch("core.tele.approval_bot._get_session", return_value=session),
            patch("core.tele.approval_bot._reset_session"),
            patch(
                "core.tele.approval_bot._run_background_ideas_generation",
                side_effect=capture_generation,
            ),
            patch(
                "core.tele.approval_bot.asyncio.create_task",
                side_effect=_run_coroutine,
            ),
        ):
            _run_coroutine(handle_approval_callback(update, context))

        self.assertEqual(len(generated), 1)
        _, chat_id, prompt, count = generated[0]
        self.assertEqual(chat_id, "123456")
        self.assertEqual(count, 3)
        self.assertIn("TÌNH HUỐNG GỐC CẦN GEMINI CHỈNH SỬA VÀ PHÁT TRIỂN", prompt)
        self.assertIn("Giữ nguyên mấu chốt gây cười", prompt)
        self.assertIn(session["theme"], prompt)

    def test_promote_queue_item_moves_selected_item_to_front(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from core.storage import promote_approved_queue_item

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_file = root / "queue.json"
            queue_file.write_text(
                json.dumps([{"id": "first"}, {"id": "selected"}]),
                encoding="utf-8",
            )
            with (
                patch("core.storage.STORAGE_DIR", root),
                patch("core.storage.APPROVED_QUEUE_FILE", queue_file),
                patch("core.storage.DRAFTS_FILE", root / "drafts.json"),
                patch("core.storage.QUEUE_LOCK_FILE", root / "queue.lock"),
            ):
                position = promote_approved_queue_item("selected")
                missing_position = promote_approved_queue_item("missing")

            queue = json.loads(queue_file.read_text(encoding="utf-8"))

        self.assertEqual(position, 1)
        self.assertEqual(missing_position, 0)
        self.assertEqual([item["id"] for item in queue], ["selected", "first"])

    def test_queue_status_offers_each_item_as_run_first(self) -> None:
        from unittest.mock import AsyncMock, patch

        from core.tele.approval_bot import _send_queue_status, _queue_run_cache

        message = AsyncMock()
        queue = [
            {"id": "item-1", "idea": {"title": "Câu chuyện một"}},
            {"id": "item-2", "idea": {"title": "Câu chuyện hai"}},
        ]
        with (
            patch("core.tele.approval_bot.read_json", return_value=queue),
            patch("core.tele.approval_bot._queue_run_cache", {}),
        ):
            _run_coroutine(_send_queue_status(message))

        args, kwargs = message.reply_text.await_args
        self.assertIn("đưa một mục lên đầu hàng đợi", args[0])
        buttons = [
            button
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(len(buttons), 2)
        self.assertTrue(all(button.callback_data.startswith("queue_run:") for button in buttons))
        self.assertTrue(any("Câu chuyện một" in button.text for button in buttons))
        self.assertTrue(any("Câu chuyện hai" in button.text for button in buttons))

    def test_queue_run_callback_promotes_selected_item_and_starts_worker(self) -> None:
        from unittest.mock import AsyncMock, Mock, patch
        import tempfile
        from pathlib import Path

        from core.tele.approval_bot import _queue_run_cache, handle_approval_callback

        token = "selection-token"
        _queue_run_cache[token] = "item-2"
        query = AsyncMock()
        query.data = f"queue_run:{token}"
        message = AsyncMock()
        message.chat_id = "123456"
        query.message = message
        update = Mock(callback_query=query)
        context = Mock()
        queued_items = [
            {"id": "item-2", "publish": False},
            {"id": "item-1", "publish": False},
        ]

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("core.tele.approval_bot.get_setting", return_value="123456"),
            patch(
                "core.tele.approval_bot.PROCESS_LOCK_FILE",
                Path(temporary) / "process.lock",
            ),
            patch(
                "core.tele.approval_bot.promote_approved_queue_item",
                return_value=1,
            ) as promote,
            patch(
                "core.tele.approval_bot.read_json",
                return_value=queued_items,
            ),
            patch(
                "core.tele.approval_bot._send_queue_status",
                new_callable=AsyncMock,
            ) as send_status,
            patch(
                "core.tele.approval_bot._auto_start_video_worker_if_needed"
            ) as start_worker,
        ):
            _run_coroutine(handle_approval_callback(update, context))

        promote.assert_called_once_with("item-2")
        send_status.assert_awaited_once_with(message)
        start_worker.assert_called_once_with(publish=False)
        query.answer.assert_awaited_once()
        self.assertNotIn(token, _queue_run_cache)

    def test_register_bot_commands_replaces_telegram_command_menu(self) -> None:
        from unittest.mock import AsyncMock, patch

        from core.tele.approval_bot import TELEGRAM_BOT_COMMANDS, register_bot_commands

        bot = AsyncMock()
        with patch("core.tele.approval_bot.get_setting", return_value="123456"):
            _run_coroutine(register_bot_commands(bot))

        self.assertEqual(bot.set_my_commands.await_count, 2)
        self.assertEqual(
            bot.set_my_commands.await_args_list[0].args,
            (TELEGRAM_BOT_COMMANDS,),
        )
        self.assertEqual(
            str(bot.set_my_commands.await_args_list[1].kwargs["scope"].chat_id),
            "123456",
        )
        self.assertEqual(
            [command.command for command in TELEGRAM_BOT_COMMANDS],
            ["choice", "new", "direct", "hang_doi"],
        )

    def test_command_registration_includes_new_and_choice_aliases(self) -> None:
        from unittest.mock import Mock

        from core.tele.approval_bot import (
            handle_choice_command,
            handle_direct_command,
            handle_script_command,
            register_approval_handler,
        )

        application = Mock()
        register_approval_handler(application)

        handlers = [
            call.args[0]
            for call in application.add_handler.call_args_list
        ]
        command_handlers = {
            command: handler.callback
            for handler in handlers
            if hasattr(handler, "commands")
            for command in handler.commands
        }
        self.assertIs(command_handlers["new"], handle_script_command)
        self.assertIs(command_handlers["direct"], handle_direct_command)
        self.assertIs(command_handlers["choice"], handle_choice_command)

    def test_choice_command_shows_many_monkey_and_uncle_topic_options(self) -> None:
        from unittest.mock import AsyncMock, Mock, patch

        from core.tele.approval_bot import handle_choice_command

        message = AsyncMock()
        message.chat_id = "123456"
        update = Mock()
        update.message = message

        with (
            patch("core.tele.approval_bot.get_setting", return_value="123456"),
            patch("core.tele.approval_bot.asyncio.to_thread", new=AsyncMock()) as to_thread_mock,
        ):
            _run_coroutine(handle_choice_command(update, Mock()))

        to_thread_mock.assert_not_awaited()
        message.reply_text.assert_awaited_once()
        self.assertIn("khỉ chủ tiệm sửa xe và ông chú", message.reply_text.await_args.args[0])
        _, kwargs = message.reply_text.await_args
        buttons = [
            button
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertGreaterEqual(len(buttons), 12)
        self.assertTrue(
            all(
                button.callback_data.startswith("pick_topic:")
                for button in buttons[:12]
            )
        )
        labels = [button.text for button in buttons]
        self.assertIn("📱 Hóng phốt, dính link bẫy", labels)
        self.assertIn("🧩 Đưa tình huống để Gemini phát triển", labels)
        self.assertTrue(any("Hỏi xoáy đáp xoay" in label for label in labels))
        self.assertTrue(any("cà khịa" in label.lower() for label in labels))
        self.assertIn("🎲 Gemini nghĩ thêm 5 chủ đề", labels)
        self.assertEqual(buttons[-1].callback_data, "menu:cancel")

    def test_script_command_prompts_for_a_script(self) -> None:
        from unittest.mock import AsyncMock, Mock, patch

        from core.tele.approval_bot import _get_session, handle_script_command

        message = AsyncMock()
        message.chat_id = "123456"
        update = Mock()
        update.message = message

        with patch("core.tele.approval_bot.get_setting", return_value="123456"):
            _run_coroutine(handle_script_command(update, Mock()))

        self.assertEqual(_get_session("123456")["state"], "waiting_script")
        message.reply_text.assert_awaited_once()

    def test_handle_menu_command_sends_menu(self) -> None:
        from unittest.mock import AsyncMock, Mock, patch

        from core.tele.approval_bot import handle_menu_command

        message = AsyncMock()
        message.chat_id = "123456"
        update = Mock()
        update.message = message
        context = Mock()

        with patch("core.tele.approval_bot.get_setting", return_value="123456"):
            _run_coroutine(handle_menu_command(update, context))

        message.reply_text.assert_awaited_once()
        args, kwargs = message.reply_text.await_args
        self.assertIn("BẢNG ĐIỀU KHIỂN", args[0])
        self.assertIn("3, 5 hoặc 10", args[0])
        keyboard = kwargs.get("reply_markup")
        self.assertIsNotNone(keyboard)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(
            [button.callback_data for button in buttons],
            [
                "menu:prompt_theme",
                "menu:suggest_topics",
                "menu:paste_script",
                "menu:direct_prompt",
                "menu:view_queue",
            ],
        )
        self.assertNotIn("Đăng nhập Flow", args[0])

    def test_handle_text_message_pasting_script_creates_preview(self) -> None:
        from unittest.mock import AsyncMock, Mock, patch

        from core.tele.approval_bot import handle_text_message

        message = AsyncMock()
        message.chat_id = "123456"
        message.text = (
            "🔥 KỊCH BẢN: Test Trực Tiếp\n"
            'Scene 1 — Input / First Frame: @nguoi + @Voice_Nguoi: Vertical 9:16. Speaker: @nguoi with @Voice_Nguoi: "Thoại 1" — finishes speaking.\n'
            'Scene 2 — Input / First Frame: @khi + @Voice_Khi: Vertical 9:16. Speaker: @khi with @Voice_Khi: "Thoại 2" — finishes speaking.'
        )
        update = Mock()
        update.message = message
        context = Mock()

        with (
            patch("core.tele.approval_bot.get_setting", return_value="123456"),
            patch("core.tele.approval_bot.append_pending_draft") as append_mock,
        ):
            _run_coroutine(handle_text_message(update, context))

        append_mock.assert_called_once()
        message.reply_text.assert_awaited_once()
        args, kwargs = message.reply_text.await_args
        self.assertIn("Test Trực Tiếp", args[0])
        self.assertIsNotNone(kwargs.get("reply_markup"))

    def test_direct_command_accepts_flow_prompt_without_gemini(self) -> None:
        from unittest.mock import AsyncMock, Mock, patch

        from core.tele.approval_bot import (
            _get_session,
            handle_direct_command,
            handle_text_message,
        )

        message = AsyncMock()
        message.chat_id = "123456"
        message.text = "/direct"
        update = Mock()
        update.message = message
        prompt = (
            "**Scene 1 (7 giây — thiết lập không gian)**\n"
            "> `@khi @nguoi Medium two-shot, vertical 9:16.`\n"
            "**Scene 2 (9 giây — đối thoại)**\n"
            '> `@khi @nguoi lip sync: "Làm gì ngồi thở dài?"`\n'
            "**Scene 3 (10 giây — cú chốt)**\n"
            '> `@khi raises a wrench: "Đất trôi theo dòng đời!"`'
        )

        with (
            patch("core.tele.approval_bot.get_setting", return_value="123456"),
            patch("core.tele.approval_bot.append_pending_draft") as append_mock,
        ):
            _run_coroutine(handle_direct_command(update, Mock()))
            self.assertEqual(_get_session("123456")["state"], "waiting_direct")
            message.text = prompt
            _run_coroutine(handle_text_message(update, Mock()))

        append_mock.assert_called_once()
        idea = append_mock.call_args.args[1]
        self.assertEqual(append_mock.call_args.kwargs["source"], "telegram_direct")
        self.assertEqual(
            [scene["duration"] for scene in idea["scenes"]],
            ["7s", "9s", "10s"],
        )
        self.assertIn("@khi @nguoi Medium two-shot", idea["scenes"][0]["prompt"])
        keyboard = message.reply_text.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertFalse(any(value.startswith("refine_script:") for value in callbacks))

    def test_handle_approval_callback_prod_tele_enqueues_without_publish(
        self,
    ) -> None:
        from unittest.mock import AsyncMock, Mock, patch

        from core.tele.approval_bot import handle_approval_callback

        query = AsyncMock()
        query.data = "prod_tele:test-draft-id"
        msg = AsyncMock()
        msg.chat_id = "123456"
        query.message = msg
        update = Mock()
        update.callback_query = query
        context = Mock()

        with (
            patch("core.tele.approval_bot.get_setting", return_value="123456"),
            patch("core.tele.approval_bot._auto_start_video_worker_if_needed") as auto_start_mock,
            patch(
                "core.tele.approval_bot.enqueue_draft_with_options",
                return_value=1,
            ) as enqueue_mock,
            patch(
                "core.tele.approval_bot._find_draft_and_index",
                return_value=(
                    {
                        "id": "test-draft-id",
                        "idea": {"title": "Test", "scenes": []},
                    },
                    1,
                ),
            ),
        ):
            _run_coroutine(handle_approval_callback(update, context))

        enqueue_mock.assert_called_once_with(
            "test-draft-id", publish=False, notify_progress=True
        )
        auto_start_mock.assert_called_once_with(publish=False)
        query.edit_message_text.assert_awaited_once()

    def test_handle_approval_callback_gen_count_uses_context_bot(self) -> None:
        from unittest.mock import AsyncMock, Mock, patch

        from telegram import CallbackQuery

        from core.tele.approval_bot import handle_approval_callback

        query = AsyncMock(spec=CallbackQuery)
        query.data = "gen_count:5"
        message = AsyncMock()
        message.chat_id = "123456"
        query.message = message
        update = Mock(callback_query=query)
        bot = object()
        context = Mock(bot=bot)
        generated: list[tuple[object, str, str, int]] = []

        async def capture_generation(
            passed_bot: object, chat_id: str, theme: str, count: int
        ) -> None:
            generated.append((passed_bot, chat_id, theme, count))

        with (
            patch("core.tele.approval_bot.get_setting", return_value="123456"),
            patch(
                "core.tele.approval_bot._get_session",
                return_value={"theme": "Chủ đề thử nghiệm"},
            ),
            patch("core.tele.approval_bot._reset_session"),
            patch(
                "core.tele.approval_bot._run_background_ideas_generation",
                side_effect=capture_generation,
            ),
            patch(
                "core.tele.approval_bot.asyncio.create_task",
                side_effect=_run_coroutine,
            ),
        ):
            _run_coroutine(handle_approval_callback(update, context))

        self.assertEqual(
            generated,
            [(bot, "123456", "Chủ đề thử nghiệm", 5)],
        )

    def test_send_trending_topic_suggestions_includes_refresh_button(self) -> None:
        from unittest.mock import AsyncMock, patch

        from core.tele.approval_bot import _send_trending_topic_suggestions

        status_msg = AsyncMock()
        message = AsyncMock()
        message.reply_text = AsyncMock(return_value=status_msg)

        sample_topics = [
            "Chủ đề 1: Bắt chữ nghĩa đen ở tiệm",
            "Chủ đề 2: Chữa lợn lành thành lợn què",
            "Chủ đề 3: Ông chú sĩ diện bị bóc mẽ",
            "Chủ đề 4: Bảng giá sửa xe lươn lẹo",
            "Chủ đề 5: Đú trend công nghệ nửa mùa",
        ]
        async def fake_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        with (
            patch(
                "core.tele.approval_bot.suggest_trending_topics",
                return_value=sample_topics,
            ),
            patch(
                "core.tele.approval_bot.asyncio.to_thread",
                side_effect=fake_to_thread,
            ),
        ):
            _run_coroutine(_send_trending_topic_suggestions(message))

        status_msg.edit_text.assert_awaited_once()
        markup = status_msg.edit_text.call_args.kwargs["reply_markup"]
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 7)
        self.assertEqual(buttons[-2].text, "🔄 Đổi 5 chủ đề khác")
        self.assertEqual(buttons[-2].callback_data, "menu:suggest_topics")
        self.assertEqual(buttons[-1].callback_data, "menu:cancel")


if __name__ == "__main__":
    unittest.main()
