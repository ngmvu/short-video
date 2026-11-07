from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from core.flow_runner import (
    FlowDurationSelectionError,
    _combined_video_prompt,
    _configure_video_settings,
    _duration_selector,
    _dedupe_clip_files,
    _dedupe_indexed_video_tiles,
    _agent_continuation_prompt,
    _generation_mode_for_idea,
    _new_result_index,
    _add_character_assets_to_prompt,
    _idea_character_tags,
    _scene_character_tags,
    _scene_duration_seconds,
    _scene_video_prompt,
    _use_agent_tab_for_idea,
    read_approved_queue,
)


class FlowPromptTests(unittest.TestCase):
    def test_combines_all_characters_and_three_ordered_scenes(self) -> None:
        idea = {
            "character_1": {
                "ingredient": "@khi_tho",
                "image_prompt": "A friendly rabbit mechanic",
            },
            "character_2": {
                "ingredient": "@khach",
                "image_prompt": "A surprised customer",
            },
            "scenes": [
                {"duration": "3s", "prompt": "The rabbit opens a garage."},
                {
                    "duration": "3-4s",
                    "prompt": "The customer asks a question.",
                    "dialogue": "Xe tôi sao rồi?",
                    "voice": "@Voice_Khach",
                },
                {
                    "duration": "5-7s",
                    "prompt": "The rabbit gives a funny answer.",
                    "dialogue": "Nó cần nghỉ phép!",
                    "voice": "@Voice_Khi",
                },
            ],
        }

        prompt = _combined_video_prompt(idea)

        self.assertIn("@khi_tho", prompt)
        self.assertIn("@khach", prompt)
        self.assertNotIn("A friendly rabbit mechanic", prompt)
        self.assertNotIn("A surprised customer", prompt)
        self.assertIn("Beat 1", prompt)
        self.assertLess(prompt.index("Beat 1"), prompt.index("Beat 2"))
        self.assertLess(prompt.index("Beat 2"), prompt.index("Beat 3"))
        self.assertIn("Xe tôi sao rồi?", prompt)
        self.assertIn("Nó cần nghỉ phép!", prompt)
        self.assertIn("The other character stays silent", prompt)
        self.assertIn("do not make the listener freeze", prompt)

    def test_scene_prompt_names_silent_listener_and_requires_reactions(self) -> None:
        scene = {
            "first_frame": "@human + @monkey + @Voice_Human",
            "prompt": (
                "Speaker: @human faces the camera. "
                "Listener: @monkey stays silent."
            ),
            "dialogue": "Xe tôi đâu?",
            "voice": "@Voice_Human",
        }

        prompt = _scene_video_prompt({}, scene)

        self.assertIn("@human is the only speaker", prompt)
        self.assertIn("@monkey stays silent with a relaxed closed mouth", prompt)
        self.assertIn("actively reacts throughout", prompt)
        self.assertIn("never freeze or hold a static pose", prompt)

    def test_prompt_and_duration_selector_use_requested_settings(self) -> None:
        idea = {
            "character_1": {"ingredient": "@rabbit", "image_prompt": "Rabbit"},
            "character_2": {"ingredient": "@human", "image_prompt": "Human"},
            "duration_seconds": 15,
            "user_prompt": "A cooking contest",
            "scenes": [
                {"duration": "0-5s", "prompt": "The contest starts."},
                {"duration": "5-15s", "prompt": "The rabbit wins."},
            ],
        }

        prompt = _combined_video_prompt(idea)

        self.assertIn("15 seconds", prompt)
        self.assertIn("these 2 beats", prompt)
        self.assertIn("A cooking contest", prompt)
        self.assertEqual(
            _duration_selector('button[role="radio"]:has-text("10s")', 15),
            'button[role="radio"]:has-text("15s")',
        )
        self.assertEqual(
            _duration_selector('button[role="radio"]:has-text("{seconds}s")', 8),
            'button[role="radio"]:has-text("8s")',
        )
        with self.assertRaisesRegex(ValueError, "FLOW_DURATION_SELECTOR"):
            _duration_selector("button[role=radio]", 15)

    def test_auto_scene_duration_includes_every_quoted_dialogue_line(self) -> None:
        scene = {
            "duration": "auto",
            "prompt": (
                'First speaker: "Một hai ba bốn năm sáu bảy tám chín mười." '
                'Second speaker: "Mười một mười hai mười ba mười bốn mười lăm mười sáu mười bảy mười tám mười chín hai mươi."'
            ),
            "dialogue": "Một hai ba bốn năm sáu bảy tám chín mười.",
        }

        self.assertEqual(_scene_duration_seconds(scene), 14)

    def test_explicit_scene_duration_is_extended_when_dialogue_needs_more_time(self) -> None:
        scene = {
            "duration": "5s",
            "dialogue": "Một hai ba bốn năm sáu bảy tám chín mười.",
        }

        self.assertEqual(_scene_duration_seconds(scene), 6)

    def test_direct_telegram_prompts_follow_configured_generation_mode(self) -> None:
        self.assertEqual(
            _generation_mode_for_idea(
                {"source": "telegram_direct"},
                "agent",
            ),
            "agent",
        )
        self.assertEqual(
            _generation_mode_for_idea(
                {"source": "telegram_script"},
                "agent",
            ),
            "agent",
        )
        self.assertEqual(
            _generation_mode_for_idea(
                {"source": "telegram_direct"},
                "direct",
            ),
            "direct",
        )
        self.assertTrue(_use_agent_tab_for_idea({"source": "telegram_direct"}, "true"))
        self.assertFalse(_use_agent_tab_for_idea({"source": "telegram_direct"}, "false"))
        self.assertTrue(_use_agent_tab_for_idea({"source": "telegram_script"}, "true"))

    def test_extracts_character_tags_without_voice_tags(self) -> None:
        self.assertEqual(
            _scene_character_tags(
                {"first_frame": "@khi + @nguoi + @Voice_Khi"}
            ),
            ["@khi", "@nguoi"],
        )

    def test_collects_unique_character_assets_for_agent_submission(self) -> None:
        idea = {
            "character_1": {"ingredient": "@khi"},
            "character_2": {"ingredient": "@nguoi"},
            "scenes": [
                {"first_frame": "@nguoi + @khi + @Voice_Nguoi"},
                {"first_frame": "@khi + @nguoi + @Voice_Khi"},
            ],
        }

        self.assertEqual(_idea_character_tags(idea), ["@khi", "@nguoi"])

    def test_agent_continuation_prompt_includes_only_unfinished_scenes(self) -> None:
        idea = {
            "scenes": [
                {
                    "first_frame": "@nguoi + @khi + @Voice_Nguoi",
                    "prompt": "The uncle asks the monkey to dance.",
                    "dialogue": "Bật nhạc lên nào!",
                    "voice": "@Voice_Nguoi",
                },
                {
                    "first_frame": "@khi + @nguoi + @Voice_Khi",
                    "prompt": "The monkey demonstrates the dance.",
                    "dialogue": "Chú nhìn cháu làm mẫu đây!",
                    "voice": "@Voice_Khi",
                },
                {
                    "first_frame": "@nguoi + @khi + @Voice_Nguoi",
                    "prompt": "The uncle reacts to an ant in his clothes.",
                    "dialogue": "Cứu chú với!",
                    "voice": "@Voice_Nguoi",
                },
            ],
        }

        continuation = _agent_continuation_prompt(idea, completed_count=1)

        self.assertIn("Do not stop to ask the user", continuation)
        self.assertNotIn("Scene 1", continuation)
        self.assertIn("Scene 2 — Input / First Frame: @khi + @nguoi + @Voice_Khi", continuation)
        self.assertIn("Scene 3 — Input / First Frame: @nguoi + @khi + @Voice_Nguoi", continuation)
        self.assertNotIn("Bật nhạc lên nào!", continuation)
        self.assertIn("Cứu chú với!", continuation)

    def test_adds_character_assets_after_prompt_in_asset_picker_order(self) -> None:
        events: list[str] = []

        class Locator:
            def __init__(
                self,
                page: "Page",
                role: str,
                name: str | re.Pattern[str],
                index: int | None = None,
            ) -> None:
                self.page = page
                self.role = role
                self.name = name
                self.index = index

            async def click(self) -> None:
                if self.name == "Add ingredients to the prompt box":
                    events.append("open-picker")
                elif self.role == "tab":
                    events.append("characters-tab")
                elif self.role == "option":
                    pattern = self.name.pattern if isinstance(self.name, re.Pattern) else self.name
                    match = re.search(r"(@[\w.]+)", pattern)
                    tag = match.group(0) if match else ""
                    if tag not in {"@khi", "@nguoi"}:
                        raise AssertionError(f"Unexpected character option: {self.name}")
                    events.append(f"select:{tag}")
                    self.page.current_tag = tag
                    if tag == "@nguoi":
                        self.page.ingredient_count += 1
                elif self.name == "Add to prompt":
                    events.append("add-to-prompt")
                    self.page.ingredient_count += 1
                    self.page.current_tag = ""

            async def count(self) -> int:
                if self.name == "Add to prompt":
                    return int(self.page.current_tag == "@khi")
                if self.name == "Ingredient":
                    return self.page.ingredient_count
                return 1

            async def is_visible(self) -> bool:
                return await self.count() > 0

            async def wait_for(self, state: str, timeout: int) -> None:
                if self.role == "button" and self.name == "Ingredient":
                    if self.index is None or self.index >= self.page.ingredient_count:
                        raise AssertionError("Character ingredient was not added.")

            def nth(self, index: int) -> "Locator":
                return Locator(self.page, self.role, self.name, index)

        class Page:
            ingredient_count = 0
            current_tag = ""

            def get_by_role(
                self,
                role: str,
                name: str | re.Pattern[str],
                exact: bool = False,
            ) -> Locator:
                return Locator(self, role, name)

        _run_coroutine(_add_character_assets_to_prompt(Page(), ["@khi", "@nguoi"]))

        self.assertEqual(
            events,
            [
                "open-picker",
                "characters-tab",
                "select:@khi",
                "add-to-prompt",
                "open-picker",
                "characters-tab",
                "select:@nguoi",
            ],
        )


def _run_coroutine(coro):
    while True:
        try:
            coro.send(None)
        except StopIteration as exc:
            return exc.value


class FlowSettingsTests(unittest.TestCase):
    def test_video_settings_select_options_and_fail_if_duration_is_unavailable(self) -> None:
        class Locator:
            def __init__(self, checked: str | None = None) -> None:
                self.checked = checked
                self.click_count = 0
                self.raise_timeout = False

            async def count(self) -> int:
                return 1

            async def get_attribute(self, name: str) -> str | None:
                return self.checked

            async def wait_for(self, state: str, timeout: int) -> None:
                if self.raise_timeout:
                    raise PlaywrightTimeoutError("not found")
                self.wait_state = state

            async def click(self) -> None:
                self.click_count += 1
                self.checked = "true"

        class Keyboard:
            async def press(self, key: str) -> None:
                self.last_key = key

        class Page:
            def __init__(self) -> None:
                self.locators = {
                    "agent": Locator("true"),
                    "settings": Locator(),
                    "video": Locator("true"),
                    "aspect": Locator("true"),
                    "duration": Locator("false"),
                    "count": Locator("false"),
                }
                self.keyboard = Keyboard()

            def locator(self, selector: str) -> Locator:
                return self.locators[selector]

        page = Page()
        selectors = {
            "agent_toggle": "agent",
            "settings": "settings",
            "video_mode": "video",
            "aspect_ratio": "aspect",
            "duration": "duration",
            "output_count": "count",
        }

        _run_coroutine(_configure_video_settings(page, selectors))

        self.assertEqual(page.locators["agent"].click_count, 1)
        self.assertEqual(page.locators["settings"].click_count, 1)
        self.assertEqual(page.locators["video"].click_count, 0)
        self.assertEqual(page.locators["aspect"].click_count, 0)
        self.assertEqual(page.locators["duration"].click_count, 1)
        self.assertEqual(page.locators["count"].click_count, 1)
        self.assertEqual(page.keyboard.last_key, "Escape")

        page.locators["duration"].raise_timeout = True
        with self.assertRaisesRegex(
            FlowDurationSelectionError,
            "dừng trước khi gửi",
        ):
            _run_coroutine(_configure_video_settings(page, selectors))


class FlowResultTests(unittest.TestCase):
    def test_identifies_new_video_by_label_delta(self) -> None:
        self.assertEqual(
            _new_result_index(
                ["old-a", "old-b"],
                ["generated-idea", "old-a", "old-b"],
            ),
            0,
        )
        self.assertIsNone(_new_result_index(["old-a"], ["old-a"]))

    def test_deduplicates_result_tiles_with_the_same_video_source(self) -> None:
        tiles = [
            (0, {"src": "https://flow.test/video-a.mp4"}),
            (1, {"src": "https://flow.test/video-a.mp4"}),
            (2, {"src": "https://flow.test/video-b.mp4"}),
        ]

        self.assertEqual(
            _dedupe_indexed_video_tiles(tiles),
            [tiles[0], tiles[2]],
        )

    def test_removes_duplicate_downloaded_clip_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "scene1.mp4"
            duplicate = Path(temporary) / "scene2.mp4"
            distinct = Path(temporary) / "scene3.mp4"
            first.write_bytes(b"same video bytes")
            duplicate.write_bytes(b"same video bytes")
            distinct.write_bytes(b"different video bytes")

            clips = _dedupe_clip_files([first, duplicate, distinct])

            self.assertEqual(clips, [first, distinct])
            self.assertTrue(first.is_file())
            self.assertFalse(duplicate.exists())
            self.assertTrue(distinct.is_file())


class ApprovedQueueTests(unittest.TestCase):
    def test_reads_approved_queue_from_storage_file(self) -> None:
        item = {"id": "approved-1", "idea": {"title": "Test"}}
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            queue_file = storage / "queue_approved.json"
            queue_file.write_text(json.dumps([item]), encoding="utf-8")
            with (
                patch("core.flow_runner.APPROVED_QUEUE_FILE", queue_file),
                patch("core.flow_runner.QUEUE_LOCK_FILE", storage / "queue.lock"),
            ):
                self.assertEqual(read_approved_queue(), [item])


if __name__ == "__main__":
    unittest.main()
