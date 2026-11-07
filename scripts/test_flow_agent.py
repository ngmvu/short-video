from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import sys

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.flow_runner import DOWNLOADS_DIR, _safe_filename, generate_video_clips, merge_clips
from core.gemini.idea_generator import format_flow_agent_prompt, generate_ideas

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("test_flow_agent")


async def run_test(call_gemini: bool = False) -> None:
    os.environ["FLOW_HEADLESS"] = "false"
    os.environ["FLOW_GENERATION_MODE"] = "agent"
    os.environ["FLOW_USE_AGENT_TAB"] = "true"

    idea = None
    if call_gemini:
        LOGGER.info("Đang gọi Gemini AI sinh kịch bản mới chuẩn 5 phần...")
        ideas = generate_ideas(
            prompt="Ăn phở bò bỏ hành & thách thức bản lĩnh giang hồ của ông chú với khỉ",
            scene_count=3,
            duration_seconds=20,
        )
        if ideas:
            idea = ideas[0]

    if idea is None:
        queue_path = ROOT_DIR / "storage" / "queue_approved.json"
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        idea = queue[0]["idea"]

    agent_prompt = format_flow_agent_prompt(idea)
    print("\n" + "=" * 72)
    print("PROMPT SẼ DÁN VÀO TAB AGENT GOOGLE FLOW:")
    print("=" * 72)
    print(agent_prompt)
    print("=" * 72 + "\n")

    item_id = "test-agent-5part"
    clips = await generate_video_clips(item_id, idea)
    print("\n" + "=" * 72)
    print(f"KẾT QUẢ TẢI VIDEO ({len(clips)} video):")
    for idx, clip in enumerate(clips, start=1):
        size_kb = clip.stat().st_size / 1024 if clip.exists() else 0
        print(f"  [{idx}] {clip} ({size_kb:.1f} KB)")
    if len(clips) > 1:
        merged = merge_clips(
            clips,
            DOWNLOADS_DIR / (_safe_filename(idea["title"], item_id) + "-full.mp4"),
        )
        print(f"  [FULL MERGED] {merged}")
    print("=" * 72 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test Google Flow Agent tab with headless=False and 5-part script."
    )
    parser.add_argument(
        "--gemini",
        action="store_true",
        help="Call Gemini API to generate a fresh 5-part script before opening Chrome.",
    )
    args = parser.parse_args()
    asyncio.run(run_test(call_gemini=args.gemini))


if __name__ == "__main__":
    main()
