from __future__ import annotations

import unittest

from core.flow_runner import _scene_duration_seconds
from core.script_parser import looks_like_direct_prompt, parse_direct_prompt


class DirectPromptParserTests(unittest.TestCase):
    def test_parses_inline_first_frame_prompt_for_each_scene(self) -> None:
        prompt = (
            "🔥 KỊCH BẢN: Prompt trực tiếp từ Telegram\n"
            'Scene 1 — Input / First Frame: @khi + @nguoi: @khi @nguoi Medium two-shot in front of a rustic motorbike repair shop, vertical 9:16: on the right, @nguoi leans forward asking: "Này khỉ, cái miếng đất hôm trước mày đòi bán theo tạ có giấy tờ gì không, là sổ đỏ hay sổ hồng?" ; on the left, @khi sits on a stool; authentic daytime lighting.\n'
            '\u200bScene 2 — Input / First Frame: @khi + @nguoi: @khi @nguoi Medium two-shot: "Sổ đỏ sổ hồng xưa như trái đất rồi! Đất tính theo tạ thì giấy tờ ở đây chơi SỔ KÝ!" ; "Ủa trên đời này tao chỉ nghe sổ đỏ sổ hồng, làm quái gì có cái gọi là sổ ký?"\n'
            '\u200bScene 3 — Input / First Frame: @khi + @nguoi: @khi @nguoi Medium two-shot: "Cân đất bao nhiêu ký thì in ra cuốn sổ nặng bấy nhiêu ký! Chú mua một tạ tui vác nguyên cuốn sổ nặng một tạ giao tận cửa nhà, cân ký tính tiền sòng phẳng! Đúng là khỉ!"'
        )

        self.assertTrue(looks_like_direct_prompt(prompt))
        idea = parse_direct_prompt(prompt)

        self.assertEqual(idea["source"], "telegram_direct")
        self.assertEqual(len(idea["scenes"]), 3)
        self.assertEqual(
            idea["scenes"][0]["first_frame"],
            "@khi + @nguoi",
        )
        self.assertTrue(
            idea["scenes"][0]["prompt"].startswith("@khi @nguoi Medium two-shot")
        )
        self.assertTrue(
            idea["scenes"][2]["prompt"].startswith("@khi @nguoi Medium two-shot")
        )
        for scene in idea["scenes"]:
            self.assertNotIn("Input / First Frame", scene["prompt"])
            self.assertNotIn("@khi + @nguoi:", scene["prompt"])
        self.assertIn("đúng là khỉ", idea["scenes"][2]["dialogue"].lower())
        self.assertEqual(
            [_scene_duration_seconds(scene) for scene in idea["scenes"]],
            [10, 18, 15],
        )

    def test_parses_unnumbered_input_first_frame_scenes_from_inline_code(self) -> None:
        prompt = (
            "[11/4/2026 8:36 PM] Nguyen Minh Vu: Scene — Input / First Frame: "
            "`@khi @nguoi Medium two-shot with comical confusion, vertical 9:16: "
            'Vietnamese language audio only, on the left, @khi reacts: "Màn hình tối thui rồi chú ơi!" '
            'on the right, @nguoi laughs: "Cho chừa cái tật tò mò!"` \n'
            "Scene — Input / First Frame: "
            "`@nguoi @khi Medium two-shot, vertical 9:16: "
            'on the right, @nguoi asks: "Cứu được không khỉ?" '
            'on the left, @khi shakes his head.`'
        )

        self.assertTrue(looks_like_direct_prompt(prompt))
        idea = parse_direct_prompt(prompt)

        self.assertEqual(idea["source"], "telegram_direct")
        self.assertEqual(len(idea["scenes"]), 2)
        self.assertEqual(
            [scene["scene_title"] for scene in idea["scenes"]],
            ["Scene 1", "Scene 2"],
        )
        self.assertEqual(idea["scenes"][0]["first_frame"], "@khi + @nguoi")
        self.assertEqual(idea["scenes"][1]["first_frame"], "@nguoi + @khi")
        self.assertTrue(
            idea["scenes"][0]["prompt"].startswith("@khi @nguoi Medium two-shot")
        )
        self.assertIn("Cho chừa cái tật tò mò!", idea["scenes"][0]["prompt"])


if __name__ == "__main__":
    unittest.main()
