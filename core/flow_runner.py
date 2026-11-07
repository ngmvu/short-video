from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

from filelock import FileLock
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from core.config import DOWNLOADS_DIR, STORAGE_DIR, get_setting, setting_path
from core.gemini.idea_generator import format_five_part_script, format_flow_agent_prompt
from core.storage import (
    APPROVED_QUEUE_FILE,
    PROCESS_LOCK_FILE,
    QUEUE_LOCK_FILE,
    ensure_storage,
    read_json,
    remove_approved_item,
)

LOGGER = logging.getLogger("short_video_distributor")


class FlowAuthError(RuntimeError):
    """Raised when the Google Flow session is missing, expired, or logged out."""


class FlowDurationSelectionError(RuntimeError):
    """Raised when Flow cannot select the requested clip duration."""


class FlowPolicyViolationError(RuntimeError):
    """Raised when Flow rejects a prompt as potentially violating its content policies."""


_POLICY_BLOCK_TEXTS = (
    "might violate our policies",
    "failed video generation",
)
_policy_block_baseline = 0


async def _count_policy_block_messages(page: Any) -> int:
    total = 0
    for text in _POLICY_BLOCK_TEXTS:
        try:
            total += await page.get_by_text(text, exact=False).count()
        except Exception:
            continue
    return total



def _safe_filename(title: str, item_id: str) -> str:
    normalized = re.sub(r"[^\w-]+", "-", title, flags=re.UNICODE).strip("-")
    safe_id = re.sub(r"[^A-Za-z0-9-]", "", item_id)[:40] or "video"
    return f"{normalized[:60] or 'short-video'}-{safe_id}"


def _new_result_index(
    previous_labels: list[str | None], result_labels: list[str | None]
) -> int | None:
    previous_counts = Counter(
        label for label in previous_labels if isinstance(label, str)
    )
    result_counts = Counter(label for label in result_labels if isinstance(label, str))
    new_labels = result_counts - previous_counts
    return next(
        (
            index
            for index, label in enumerate(result_labels)
            if isinstance(label, str) and new_labels[label] > 0
        ),
        None,
    )


def _combined_video_prompt(idea: dict[str, Any]) -> str:
    duration_seconds = idea.get("duration_seconds", 10)
    if not isinstance(duration_seconds, int) or not 1 <= duration_seconds <= 180:
        raise ValueError("Idea duration_seconds must be an integer from 1 to 180.")
    character_tags = [
        str(idea.get(key, {}).get("ingredient", "")).strip()
        for key in ("character_1", "character_2")
        if isinstance(idea.get(key), dict)
    ]
    character_tags = [tag for tag in character_tags if tag]
    characters = ", ".join(character_tags)
    scenes: list[str] = []
    for index, scene in enumerate(idea["scenes"], start=1):
        beat = f"Beat {index}: {scene['prompt']}"
        dialogue = scene.get("dialogue")
        if dialogue and dialogue not in scene["prompt"]:
            beat += f' Vietnamese dialogue ({scene.get("voice", "natural voice")}): "{dialogue}".'
        scenes.append(beat)
    user_prompt = idea.get("user_prompt")
    creative_direction = (
        f"Follow this user-provided creative brief:\n{user_prompt}\n"
        if isinstance(user_prompt, str) and user_prompt.strip()
        else ""
    )
    return (
        f"Create one coherent vertical 9:16 Vietnamese comedy short lasting "
        f"approximately {duration_seconds} seconds. "
        f"Use the saved Flow character assets {characters} exactly as configured "
        "in the project (do not redesign or describe them).\n"
        "PERFORMANCE: In each beat exactly one named speaker says the supplied dialogue and lip-syncs. "
        "The other character stays silent with a relaxed closed mouth and no lip-sync, but remains "
        "visibly engaged throughout: follow the speaker with eyes/head, nod or shake their head, "
        "change expression, shift posture, or make a small fitting gesture. Reactions evolve with "
        "the dialogue; do not make the listener freeze or hold a static pose. Keep both characters visible.\n"
        f"{creative_direction}"
        f"Show these {len(scenes)} beats in order as one continuous short video, with quick "
        "transitions and no on-screen text:\n"
        + "\n".join(scenes)
    )


def _duration_selector(selector: str, duration_seconds: int) -> str:
    if "{seconds}" in selector:
        return selector.replace("{seconds}", str(duration_seconds))
    if duration_seconds == 10:
        return selector
    updated, count = re.subn(
        r"(?<!\d)10s(?!\w)",
        f"{duration_seconds}s",
        selector,
        count=1,
        flags=re.IGNORECASE,
    )
    if count == 0:
        raise ValueError(
            "To select a custom duration, configure FLOW_DURATION_SELECTOR "
            "with a {seconds} placeholder or a 10s option to replace."
        )
    return updated


async def _select_setting(page: Any, selector: str) -> None:
    locator = page.locator(selector)
    await locator.wait_for(state="visible", timeout=15_000)
    if await locator.get_attribute("aria-checked") != "true":
        await locator.click()


async def _configure_video_settings(page: Any, selectors: dict[str, str]) -> None:
    agent_toggle = page.locator(selectors["agent_toggle"])
    if await agent_toggle.count() and await agent_toggle.get_attribute(
        "aria-pressed"
    ) == "true":
        await agent_toggle.click()
    settings_button = page.locator(selectors["settings"])
    await settings_button.wait_for(state="visible", timeout=10_000)
    await settings_button.click()
    for setting in ("video_mode", "aspect_ratio", "duration", "output_count"):
        try:
            await _select_setting(page, selectors[setting])
        except PlaywrightTimeoutError as exc:
            if setting == "duration":
                raise FlowDurationSelectionError(
                    "Không tìm thấy lựa chọn thời lượng yêu cầu trên Flow; "
                    "dừng trước khi gửi để tránh thoại bị cắt."
                ) from exc
            raise
    await page.keyboard.press("Escape")


async def _ensure_agent_tab_open(page: Any, selectors: dict[str, str]) -> None:
    """Ensure the Google Flow Agent tab / side-panel is open and active."""
    await asyncio.sleep(2.0)
    agent_panel_marker = page.locator(
        'button[aria-label="Start new session"]:visible, '
        'button[aria-label="Open session history"]:visible, '
        'button[aria-label="Agent instructions"]:visible'
    )
    agent_toggle = page.locator(
        f'{selectors["agent_toggle"]}, '
        'button[aria-pressed]:has-text("Agent"):visible, '
        '[role="tab"]:has-text("Agent"):visible, '
        'button:has-text("Agent"):visible'
    ).first

    try:
        panel_open = await agent_panel_marker.count() > 0
        pressed = (
            await agent_toggle.get_attribute("aria-pressed")
            if await agent_toggle.count()
            else None
        )
        if not panel_open and pressed != "true":
            if await agent_toggle.count() and await agent_toggle.is_visible():
                LOGGER.info("Đang bấm chọn tab Agent trên Google Flow...")
                await agent_toggle.click(timeout=8_000)
                await asyncio.sleep(2.0)
        else:
            LOGGER.info("Tab Agent trên Google Flow đã được mở sẵn.")
    except Exception as exc:
        LOGGER.warning("Không chuyển tự động sang tab Agent được (%s); tiếp tục với khung nhập hiện tại.", exc)

    try:
        new_session_btn = page.locator('button[aria-label="Start new session"]:visible').first
        if await new_session_btn.count() and await new_session_btn.is_visible():
            await new_session_btn.click(timeout=4_000)
            await asyncio.sleep(1.0)
    except Exception:
        pass


async def _paste_into_flow_prompt_and_submit(
    page: Any,
    selectors: dict[str, str],
    prompt_text: str,
    character_tags: list[str] | None = None,
) -> None:
    """Fill the Flow prompt, attach requested characters, and submit."""
    if character_tags:
        clear_prompt = page.get_by_role("button", name="Clear prompt", exact=True)
        if await clear_prompt.count() and await clear_prompt.is_visible():
            await clear_prompt.click()

    candidates = [
        page.locator(selectors["prompt"]).locator("visible=true"),
        page.locator('[contenteditable="true"]:visible'),
        page.locator('textarea:visible'),
    ]
    target_input = None
    for loc in candidates:
        try:
            count = await loc.count()
            if count > 0:
                target_input = loc.last
                break
        except Exception:
            continue

    if target_input is None:
        target_input = page.locator(selectors["prompt"]).first

    await target_input.click(timeout=10_000)
    await asyncio.sleep(0.3)
    try:
        await target_input.fill(prompt_text)
    except Exception:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.insert_text(prompt_text)

    LOGGER.info("Đã dán prompt vào ô nhập Google Flow (đợi 2s trước khi gửi)...")
    await asyncio.sleep(2.0)

    if character_tags:
        await _add_character_assets_to_prompt(page, character_tags)

    submit_candidates = [
        page.locator(selectors["submit"]).locator("visible=true"),
        page.locator(
            'button[aria-label="Start generation"]:visible, '
            'button[aria-label*="Send" i]:visible, '
            'button[aria-label*="Submit" i]:visible, '
            'button[type="submit"]:visible'
        ),
    ]
    submitted = False
    for sub_loc in submit_candidates:
        try:
            if await sub_loc.count() > 0:
                btn = sub_loc.last
                if await btn.is_enabled():
                    await btn.click(timeout=8_000)
                    submitted = True
                    break
        except Exception:
            continue

    global _policy_block_baseline
    _policy_block_baseline = await _count_policy_block_messages(page)
    if not submitted:
        await page.keyboard.press("Enter")
    LOGGER.info("Đã gửi prompt cho Google Flow.")


async def _add_character_assets_to_prompt(page: Any, character_tags: list[str]) -> None:
    """Attach matching saved character assets after the prompt text is in the composer."""
    tags = list(dict.fromkeys(tag.strip() for tag in character_tags if tag.strip()))
    ingredients = page.get_by_role("button", name="Ingredient", exact=True)
    ingredient_count = await ingredients.count()

    for tag in tags:
        await page.get_by_role(
            "button",
            name="Add ingredients to the prompt box",
            exact=True,
        ).click()
        await page.get_by_role("tab", name="Characters", exact=True).click()
        asset = page.get_by_role(
            "option",
            name=re.compile(rf"^{re.escape(tag)}(?:\s+Character)?$"),
        )
        await asset.wait_for(state="visible", timeout=10_000)
        await asset.click()

        add_to_prompt = page.get_by_role(
            "button",
            name="Add to prompt",
            exact=True,
        )
        if await add_to_prompt.count() and await add_to_prompt.is_visible():
            await add_to_prompt.click()

        await ingredients.nth(ingredient_count).wait_for(
            state="visible",
            timeout=10_000,
        )
        ingredient_count += 1
        LOGGER.info("Đã gắn Character asset %s vào prompt.", tag)


async def _wait_for_initial_grid_settle(page: Any, results: Any) -> None:
    """Wait for existing project tiles to finish loading before snapshotting."""
    await asyncio.sleep(2.5)
    last_count = await results.count()
    for _ in range(4):
        await asyncio.sleep(1.2)
        current_count = await results.count()
        if current_count == last_count:
            break
        last_count = current_count


async def _mark_existing_tiles(results: Any) -> None:
    await results.evaluate_all(
        """elements => elements.forEach(el => el.setAttribute('data-svd-existing', '1'))"""
    )


async def _inspect_tiles(results: Any) -> list[dict[str, Any]]:
    return await results.evaluate_all(
        """elements => elements.map(el => {
            const label = el.getAttribute('aria-label');
            const isExisting = el.getAttribute('data-svd-existing') === '1';
            const hasVideoTile = Boolean(el.querySelector('flow-video-tile'));
            const hasMoreOptions = Boolean(el.querySelector('button[aria-label="More options"]'));
            const video = el.querySelector('video');
            const src = video
                ? (video.currentSrc || video.getAttribute('src') || (video.querySelector('source') && video.querySelector('source').getAttribute('src')) || '')
                : '';
            const hasProgress = Boolean(
                el.querySelector('[role="progressbar"], mat-progress-spinner, mat-spinner, [aria-busy="true"]')
            );
            const text = (el.innerText || '').trim();
            const percentMatch = text.match(/\\b(\\d{1,3})%\\b/);
            const percent = percentMatch ? percentMatch[1] + '%' : null;
            const hasVideo = Boolean(video && src && src !== 'about:blank');
            const isReady = Boolean(
                hasVideo
                && !hasProgress
                && !percent
            );
            return {
                label,
                isExisting,
                src,
                hasVideoTile,
                hasMoreOptions,
                hasVideoElement: Boolean(video),
                hasVideo,
                isReady,
                hasProgress,
                percent,
            };
        })"""
    )


def _dedupe_indexed_video_tiles(
    tiles: list[tuple[int, dict[str, Any]]],
) -> list[tuple[int, dict[str, Any]]]:
    """Keep the first tile for each source URL while preserving original indices."""
    seen_sources: set[str] = set()
    unique_tiles: list[tuple[int, dict[str, Any]]] = []
    for index, tile in tiles:
        source = str(tile.get("src") or "").strip()
        if source and source in seen_sources:
            LOGGER.warning(
                "Bỏ qua tile video trùng nguồn Google Flow tại ô #%d.",
                index,
            )
            continue
        if source:
            seen_sources.add(source)
        unique_tiles.append((index, tile))
    return unique_tiles


def _dedupe_clip_files(clips: list[Path]) -> list[Path]:
    """Keep one copy of each downloaded clip and remove duplicate files from this run."""
    seen_paths: set[Path] = set()
    seen_hashes: dict[str, Path] = {}
    unique_clips: list[Path] = []

    for clip in clips:
        resolved_clip = clip.resolve()
        if resolved_clip in seen_paths:
            continue
        seen_paths.add(resolved_clip)

        digest = hashlib.sha256()
        with clip.open("rb") as video_file:
            for chunk in iter(lambda: video_file.read(1024 * 1024), b""):
                digest.update(chunk)
        fingerprint = digest.hexdigest()
        original = seen_hashes.get(fingerprint)
        if original is None:
            seen_hashes[fingerprint] = clip
            unique_clips.append(clip)
            continue

        LOGGER.warning(
            "Phát hiện clip tải trùng (%s và %s); bỏ file trùng.",
            original.name,
            clip.name,
        )
        try:
            clip.unlink()
        except OSError:
            LOGGER.exception("Không xóa được file clip trùng: %s", clip)

    return unique_clips


async def _wait_for_new_video_tile_ready(
    page: Any,
    results: Any,
    previous_labels: list[str | None],
    previous_srcs: set[str],
    timeout_ms: int,
    prompt_text: str = "",
    min_render_seconds: int = 40,
    approve_agent_actions: bool = False,
) -> int:
    start_time = time.monotonic()
    deadline = start_time + (timeout_ms / 1000.0)
    last_log_time = 0.0
    prompt_head = (prompt_text or "").strip()[:40]
    all_grid_tiles = page.locator('flow-grid-tile-container[role="group"]')

    while time.monotonic() < deadline:
        if await _count_policy_block_messages(page) > _policy_block_baseline:
            raise FlowPolicyViolationError(
                "Google Flow từ chối prompt: 'This prompt might violate our policies "
                "about generating harmful content.'"
            )
        try:
            approve_selector = '[aria-label="Approve"][aria-checked="false"]:visible'
            if approve_agent_actions:
                approve_selector += ', button:has-text("Approve"):visible'
            approve_btn = page.locator(
                approve_selector
            )
            if await approve_btn.count():
                LOGGER.info("Phát hiện nút Approve của Flow Agent, đang tự động bấm Approve...")
                await approve_btn.first.click(timeout=3_000)
                await asyncio.sleep(2.0)
                # Reset start_time because Flow only begins rendering after Approve is clicked
                start_time = time.monotonic()
                deadline = start_time + (timeout_ms / 1000.0)
        except Exception:
            pass

        tiles = await _inspect_tiles(results)
        result_labels = [t.get("label") for t in tiles]

        # In Google Flow, newly submitted videos are placed at the very top (index 0).
        # We search from index 0 for a tile that is NOT pre-existing and whose src is not in previous_srcs.
        candidate_idx: int | None = None
        for idx, tile in enumerate(tiles):
            tile_src = str(tile.get("src") or "")
            if not tile.get("isExisting") and (not tile_src or tile_src not in previous_srcs):
                candidate_idx = idx
                break

        # If no tile without isExisting was found yet (e.g. grid hasn't inserted the new container yet),
        # candidate is index 0 (the top-left slot where the new item will appear).
        # We NEVER fall back to index > 0 because older indices are older videos!
        if candidate_idx is None:
            candidate_idx = 0

        elapsed = int(time.monotonic() - start_time)
        if candidate_idx is not None and 0 <= candidate_idx < len(tiles):
            candidate = tiles[candidate_idx]
            cand_src = str(candidate.get("src") or "")
            is_definitely_old = bool(
                (candidate.get("isExisting") and previous_srcs)
                or (cand_src and cand_src in previous_srcs)
            )

            if is_definitely_old:
                looks_ready = False
            else:
                if not candidate.get("hasVideo") and elapsed >= min_render_seconds:
                    try:
                        await results.nth(candidate_idx).hover(timeout=2_000)
                        await asyncio.sleep(1.0)
                        refreshed = await _inspect_tiles(results)
                        if candidate_idx < len(refreshed):
                            candidate = refreshed[candidate_idx]
                    except Exception:
                        pass
                label_text = str(candidate.get("label") or "").strip()
                label_is_prompt = bool(
                    label_text
                    and (
                        len(label_text) > 160
                        or (prompt_head and label_text.startswith(prompt_head))
                    )
                )
                looks_ready = bool(
                    candidate.get("hasVideo")
                    or (
                        candidate.get("hasVideoElement")
                        and not label_is_prompt
                        and elapsed >= 45
                    )
                )
                if (
                    looks_ready
                    and elapsed >= min_render_seconds
                    and not candidate.get("hasProgress")
                    and not candidate.get("percent")
                ):
                    LOGGER.info(
                        "Google Flow đã tạo xong video '%s' tại ô #%d (sau %ds). Đợi 4s để ổn định trước khi mở tải xuống...",
                        candidate.get("label") or "",
                        candidate_idx,
                        elapsed,
                    )
                    await asyncio.sleep(4.0)
                    return candidate_idx

            if time.monotonic() - last_log_time >= 10.0:
                if is_definitely_old:
                    LOGGER.info(
                        "Đang đợi Google Flow tạo ô video mới... (đã chờ %ds · ô #%d vẫn là video cũ)",
                        elapsed,
                        candidate_idx,
                    )
                else:
                    progress_note = (
                        f" · tiến độ {candidate.get('percent')}"
                        if candidate.get("percent")
                        else " · đang xử lý video"
                    )
                    LOGGER.info(
                        "Đang đợi Google Flow render xong video... (đã chờ %ds%s)",
                        elapsed,
                        progress_note,
                    )
                last_log_time = time.monotonic()
        else:
            if time.monotonic() - last_log_time >= 10.0:
                generating_text = ""
                try:
                    if await all_grid_tiles.count() > len(previous_labels):
                        first_text = await all_grid_tiles.first.inner_text(timeout=2_000)
                        match = re.search(r"\b(\d{1,3})%\b", first_text or "")
                        if match:
                            generating_text = f" · tiến độ {match.group(1)}%"
                except Exception:
                    pass
                LOGGER.info(
                    "Đang đợi Google Flow tạo video mới... (đã chờ %ds%s)",
                    elapsed,
                    generating_text,
                )
                last_log_time = time.monotonic()

        await asyncio.sleep(3.0)

    raise RuntimeError(
        f"Quá thời gian chờ Google Flow render xong video ({timeout_ms // 1000}s)."
    )


async def _download_video_url_to_path(
    page: Any, video_src: str, destination: Path
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    response = await page.context.request.get(video_src, timeout=120_000)
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status} when downloading video URL.")
    body = await response.body()
    if len(body) < 10_000:
        raise RuntimeError(f"Downloaded video payload too small ({len(body)} bytes).")
    destination.write_bytes(body)
    return destination


def _match_prompt_to_scene_number(
    rendered_prompt: str,
    scenes: list[dict[str, Any]],
    used_scene_numbers: set[int],
) -> int | None:
    """Match the prompt text shown in <flow-expandable-prompt> to 1-based Scene number (1..N)."""
    cleaned_rendered = (rendered_prompt or "").strip().lower()
    if not cleaned_rendered or not scenes:
        return None

    best_scene_num: int | None = None
    best_score = -1.0

    rendered_quotes = [
        q.strip()
        for q in re.findall(r'["“”]([^"“”]{6,})["“”]', cleaned_rendered)
        if q.strip()
    ]
    rendered_words = {
        w for w in re.findall(r"\w{4,}", cleaned_rendered) if w not in {"medium", "shot", "vertical", "with", "sync"}
    }

    for idx, scene in enumerate(scenes, start=1):
        if idx in used_scene_numbers:
            continue
        scene_prompt = str(scene.get("prompt") or "").strip().lower()
        scene_dialogue = str(scene.get("dialogue") or "").strip().lower()
        score = 0.0

        if scene_dialogue and len(scene_dialogue) >= 8 and scene_dialogue in cleaned_rendered:
            score += 1000.0

        scene_quotes = [
            q.strip()
            for q in re.findall(r'["“”]([^"“”]{6,})["“”]', scene_prompt)
            if q.strip()
        ]
        for sq in scene_quotes:
            if sq in cleaned_rendered:
                score += 1000.0
            else:
                for rq in rendered_quotes:
                    sq_words = set(re.findall(r"\w{3,}", sq))
                    rq_words = set(re.findall(r"\w{3,}", rq))
                    if sq_words and rq_words:
                        score += 200.0 * (len(sq_words & rq_words) / max(1, len(sq_words)))

        scene_words = {
            w for w in re.findall(r"\w{4,}", scene_prompt) if w not in {"medium", "shot", "vertical", "with", "sync"}
        }
        if scene_words and rendered_words:
            overlap = len(scene_words & rendered_words)
            score += float(overlap * 10)

        if score > best_score:
            best_score = score
            best_scene_num = idx

    return best_scene_num if best_score > 0 else None


async def _read_scene_builder_prompt_text(page: Any) -> str:
    """Read the prompt text of the currently active clip in <flow-scene-builder>."""
    selectors_to_try = [
        "flow-expandable-prompt .prompt-text .text-part",
        "flow-expandable-prompt .prompt-text",
        "flow-editor-history-step-video .prompt-text",
    ]
    for sel in selectors_to_try:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                txt = (await loc.inner_text(timeout=3_000)).strip()
                if txt:
                    return txt
        except Exception:
            continue
    return ""


async def _trigger_scene_builder_download(
    page: Any,
    selectors: dict[str, str],
    destination: Path,
    download_timeout_ms: int,
) -> Path | None:
    """Click the Download media button in <flow-scene-builder> header (or More options menu) and save the file."""
    download_media_pattern = re.compile(r"^(Download media|Download|Tải)", re.IGNORECASE)
    quality_pattern = re.compile(r"720p|Original|1080p|MP4", re.IGNORECASE)

    # 1. Direct 'Download media' icon button in <flow-scene-builder> top-right header
    header_download_btn = page.locator('button[aria-label="Download media"]:visible').first
    if await header_download_btn.count() > 0 and await header_download_btn.is_visible():
        try:
            async with page.expect_download(
                timeout=min(download_timeout_ms, 60_000)
            ) as download_info:
                await header_download_btn.click(timeout=6_000)
                await asyncio.sleep(0.8)
                quality_item = page.locator(selectors["download"]).first
                fallback_quality = page.get_by_role("menuitem", name=quality_pattern).first
                any_menuitem = page.locator('[role="menuitem"]:visible').first
                try:
                    if await quality_item.count() and await quality_item.is_visible():
                        await quality_item.click(timeout=4_000)
                    elif await fallback_quality.count() and await fallback_quality.is_visible():
                        await fallback_quality.click(timeout=4_000)
                    elif await any_menuitem.count() and await any_menuitem.is_visible():
                        await any_menuitem.click(timeout=4_000)
                except Exception:
                    pass
            download = await download_info.value
            await download.save_as(destination)
            LOGGER.info("Đã tải video qua nút 'Download media' trên Scene Builder về: %s", destination)
            return destination
        except Exception as exc:
            LOGGER.warning("Tải qua nút header 'Download media' chưa hoàn tất (%s)...", exc)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    # 2. Fallback: More options menu -> Download media
    more_btn = page.locator(selectors["download_menu"]).locator("visible=true").first
    if await more_btn.count() > 0 and await more_btn.is_visible():
        try:
            await more_btn.click(timeout=5_000)
            await asyncio.sleep(0.8)
            download_media_item = page.get_by_role("menuitem", name=download_media_pattern).first
            if await download_media_item.count() > 0 and await download_media_item.is_visible():
                async with page.expect_download(
                    timeout=min(download_timeout_ms, 60_000)
                ) as download_info:
                    await download_media_item.click(timeout=5_000)
                    await asyncio.sleep(0.8)
                    quality_item = page.locator(selectors["download"]).first
                    fallback_quality = page.get_by_role("menuitem", name=quality_pattern).first
                    try:
                        if await quality_item.count() and await quality_item.is_visible():
                            await quality_item.click(timeout=4_000)
                        elif await fallback_quality.count() and await fallback_quality.is_visible():
                            await fallback_quality.click(timeout=4_000)
                    except Exception:
                        pass
                download = await download_info.value
                await download.save_as(destination)
                LOGGER.info("Đã tải video qua menu More options về: %s", destination)
                return destination
        except Exception:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
    return None


async def _open_tile_and_download(
    page: Any,
    results: Any,
    tile_index: int,
    selectors: dict[str, str],
    item_id: str,
    idea: dict[str, Any],
    render_timeout_ms: int,
    filename: str | None = None,
    expected_prompt: str = "",
    expected_dialogue: str = "",
) -> Path:
    await results.nth(tile_index).click()
    await page.wait_for_url("**/edit/**", timeout=30_000)

    if not filename:
        filename = _safe_filename(idea["title"], item_id) + ".mp4"
    destination = DOWNLOADS_DIR / filename
    destination.parent.mkdir(parents=True, exist_ok=True)

    edit_deadline = time.monotonic() + max(240.0, render_timeout_ms / 1000.0)
    download_timeout_ms = int(get_setting("FLOW_DOWNLOAD_TIMEOUT_MS", "900000"))

    while time.monotonic() < edit_deadline:
        try:
            await page.locator(
                'canvas.video-canvas, button[aria-label="Download media"], video'
            ).first.wait_for(state="visible", timeout=10_000)
        except Exception:
            LOGGER.info("Đang đợi giao diện chi tiết Flow (Scene Builder) tải hoàn tất...")
            await asyncio.sleep(3.0)
            continue

        await asyncio.sleep(1.5)

        # Inspect prompt text on Scene Builder to ensure it is not an old clip
        rendered_prompt = await _read_scene_builder_prompt_text(page)
        if rendered_prompt:
            LOGGER.info("Prompt hiển thị trên Scene Builder: '%s'", rendered_prompt[:75])
            cleaned_rendered = rendered_prompt.strip().lower()
            cleaned_dialogue = (expected_dialogue or "").strip().lower()
            if cleaned_dialogue and len(cleaned_dialogue) >= 8:
                rendered_quotes = re.findall(r'["“”]([^"“”]{6,})["“”]', cleaned_rendered)
                if rendered_quotes:
                    first_quote = rendered_quotes[0].strip().lower()
                    if first_quote not in cleaned_dialogue and cleaned_dialogue not in first_quote:
                        LOGGER.warning(
                            "⚠️ CẢNH BÁO: Clip trên Scene Builder có thoại '%s' khác với thoại kịch bản hiện tại ('%s')!",
                            rendered_quotes[0],
                            expected_dialogue[:50],
                        )

        saved = await _trigger_scene_builder_download(
            page, selectors, destination, download_timeout_ms
        )
        if saved is not None:
            return saved

        video_src = ""
        try:
            video_src = await page.evaluate(
                """() => {
                    const v = document.querySelector('video');
                    if (!v) return '';
                    return v.currentSrc || v.getAttribute('src') || (v.querySelector('source') && v.querySelector('source').getAttribute('src')) || '';
                }"""
            )
        except Exception:
            pass
        if isinstance(video_src, str) and video_src.startswith("https://"):
            return await _download_video_url_to_path(page, video_src, destination)

        await asyncio.sleep(4.0)

    raise RuntimeError(
        "Không tải được video từ trang chi tiết Google Flow sau khi đợi hoàn tất."
    )


def _scene_duration_seconds(scene: dict[str, Any], fallback: int = 8) -> int:
    """Parse '4s', '3-4s', '0-5.3s' into a whole number of seconds for one clip."""
    raw = str(scene.get("duration", "") or "")
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", raw)]
    if not numbers:
        seconds = fallback
    elif len(numbers) >= 2 and "-" in raw and numbers[0] < numbers[1] and numbers[0] == 0:
        seconds = numbers[1] - numbers[0]
    else:
        seconds = max(numbers)

    prompt = str(scene.get("prompt", "") or "")
    dialogue_lines = [
        match.group(1).strip()
        for match in re.finditer(r'["“”]([^"“”]{2,})["“”]', prompt)
    ]
    dialogue = str(scene.get("dialogue", "") or "").strip()
    if dialogue and not dialogue_lines:
        dialogue_lines = [dialogue]
    if dialogue_lines:
        syllable_count = sum(len(line.split()) for line in dialogue_lines)
        estimated_seconds = math.ceil(
            syllable_count / 3 + 2 + max(0, len(dialogue_lines) - 1) * 1.5
        )
        seconds = max(seconds, estimated_seconds)
    return max(1, min(int(math.ceil(seconds)), 60))


def _generation_mode_for_idea(idea: dict[str, Any], configured_mode: str) -> str:
    """Use the configured Flow generation mode for all prompt sources."""
    return configured_mode


def _use_agent_tab_for_idea(idea: dict[str, Any], configured_value: str) -> bool:
    return configured_value.strip().lower() not in {"0", "false", "no"}


def _scene_video_prompt(idea: dict[str, Any], scene: dict[str, Any]) -> str:
    prompt = str(scene.get("prompt", "")).strip()
    prompt = re.sub(
        r"^(?:Vertical\s+9:16\s+framing\.?\s*|Handheld\s+camera\s+style\.?\s*|Climax\.?\s*)+",
        "",
        prompt,
        flags=re.IGNORECASE,
    ).strip()
    first_frame = str(scene.get("first_frame", "") or "").strip()
    primary_tag = first_frame.split("+")[0].strip() if first_frame else ""
    if primary_tag and primary_tag not in prompt:
        prompt = f"{primary_tag} {prompt}"
    character_tags = _scene_character_tags(scene)
    speaker_match = re.search(r"\bSpeaker\s*:\s*(@[\w.]+)", prompt, re.IGNORECASE)
    speaker_tag = (
        speaker_match.group(1)
        if speaker_match and speaker_match.group(1) in character_tags
        else primary_tag if primary_tag in character_tags else ""
    )
    listener_tag = next(
        (tag for tag in character_tags if tag != speaker_tag),
        "",
    )
    dialogue = str(scene.get("dialogue") or "").strip()
    voice = str(scene.get("voice") or "").strip()
    if dialogue and dialogue not in prompt:
        voice_suffix = f" {voice}" if voice else ""
        prompt = f'{prompt.rstrip(".")} with lip sync: "{dialogue}"{voice_suffix}.'
    elif voice and voice not in prompt:
        prompt = f"{prompt} {voice}"
    speaker_instruction = (
        f"{speaker_tag} is the only speaker; keep their face clearly visible and "
        "lip-sync only their dialogue. "
        if speaker_tag
        else "Exactly one character speaks and lip-syncs the supplied dialogue. "
    )
    listener_instruction = (
        f"{listener_tag} stays silent with a relaxed closed mouth and no lip-sync, "
        if listener_tag
        else "The non-speaking character stays silent with a relaxed closed mouth and no lip-sync, "
    )
    prompt = (
        f"{prompt.rstrip('.')} {speaker_instruction}{listener_instruction}"
        "but actively reacts throughout with eye/head movement, changing expression, "
        "a nod or small natural gesture; never freeze or hold a static pose."
    )
    return prompt


def _scene_character_tags(scene: dict[str, Any]) -> list[str]:
    first_frame = str(scene.get("first_frame", "") or "")
    return list(
        dict.fromkeys(
            tag
            for tag in re.findall(r"@[\w.]+", first_frame)
            if not tag.lower().startswith("@voice")
        )
    )


def _idea_character_tags(idea: dict[str, Any]) -> list[str]:
    """Collect saved Character assets referenced by the idea, excluding voice assets."""
    tags: list[str] = []
    for key in ("character_1", "character_2"):
        character = idea.get(key)
        if isinstance(character, dict):
            tag = str(character.get("ingredient") or "").strip()
            if tag and not tag.lower().startswith("@voice"):
                tags.append(tag)
    for scene in idea.get("scenes", []):
        if isinstance(scene, dict):
            tags.extend(_scene_character_tags(scene))
    return list(dict.fromkeys(tags))


def _agent_continuation_prompt(
    idea: dict[str, Any], completed_count: int
) -> str:
    """Ask the Flow Agent to render only scenes it has not completed yet."""
    scenes = [scene for scene in idea.get("scenes", []) if isinstance(scene, dict)]
    remaining_scenes = scenes[max(0, completed_count):]
    lines = [
        "Continue the existing video request. Do not stop to ask the user whether "
        "to continue and do not regenerate completed scenes.",
        "Generate each remaining scene below as a separate video, in order, and "
        "continue until all listed scenes have been generated.",
    ]
    for index, scene in enumerate(remaining_scenes, start=completed_count + 1):
        first_frame = str(scene.get("first_frame") or "").strip()
        prompt = str(scene.get("prompt") or "").strip()
        dialogue = str(scene.get("dialogue") or "").strip()
        voice = str(scene.get("voice") or "").strip()
        scene_line = f"Scene {index}"
        if first_frame:
            scene_line += f" — Input / First Frame: {first_frame}"
        if prompt:
            scene_line += f": {prompt}"
        if dialogue and dialogue not in prompt:
            scene_line += f' Dialogue ({voice or "Vietnamese"}): "{dialogue}"'
        lines.append(scene_line)
    return "\n".join(lines)


def merge_clips(clips: list[Path], destination: Path) -> Path | None:
    """Concatenate clips with ffmpeg; returns None when ffmpeg is unavailable or fails."""
    if len(clips) < 2:
        return clips[0] if clips else None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        LOGGER.warning("Không tìm thấy ffmpeg nên không ghép được %d clip.", len(clips))
        return None
    list_file = destination.with_suffix(".txt")
    list_file.write_text(
        "".join(f"file '{clip.resolve().as_posix()}'\n" for clip in clips),
        encoding="utf-8",
    )
    attempts = [
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(destination)],
        [
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", str(destination),
        ],
    ]
    try:
        for command in attempts:
            result = subprocess.run(command, capture_output=True, timeout=600)
            if result.returncode == 0 and destination.is_file():
                LOGGER.info("Đã ghép %d clip thành video hoàn chỉnh: %s", len(clips), destination)
                return destination
        LOGGER.warning(
            "ffmpeg ghép clip thất bại: %s",
            result.stderr.decode("utf-8", errors="replace")[-500:],
        )
        return None
    except Exception:
        LOGGER.exception("Không ghép được các clip bằng ffmpeg.")
        return None
    finally:
        list_file.unlink(missing_ok=True)


def _flow_selectors(duration_seconds: int) -> dict[str, str]:
    return {
        "agent_toggle": get_setting("FLOW_AGENT_TOGGLE_SELECTOR"),
        "settings": get_setting("FLOW_SETTINGS_SELECTOR"),
        "video_mode": get_setting("FLOW_VIDEO_MODE_SELECTOR"),
        "aspect_ratio": get_setting("FLOW_ASPECT_RATIO_SELECTOR"),
        "duration": _duration_selector(
            get_setting("FLOW_DURATION_SELECTOR"),
            duration_seconds,
        ),
        "output_count": get_setting("FLOW_OUTPUT_COUNT_SELECTOR"),
        "prompt": get_setting("FLOW_PROMPT_SELECTOR"),
        "submit": get_setting("FLOW_SUBMIT_SELECTOR"),
        "result": get_setting("FLOW_RESULT_SELECTOR"),
        "download_menu": get_setting("FLOW_DOWNLOAD_MENU_SELECTOR"),
        "download": get_setting("FLOW_DOWNLOAD_SELECTOR"),
    }


async def _open_flow_project_and_verify_session(
    page: Any,
    context: Any,
    auth_file: Path,
    selectors: dict[str, str],
    headless: bool = False,
) -> None:
    project_url = get_setting("FLOW_PROJECT_URL")
    await page.goto(
        project_url,
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    await asyncio.sleep(3.0)
    current_url = (page.url or "").lower()
    if "accounts.google.com" in current_url or current_url.rstrip("/").endswith("/about"):
        if headless:
            raise FlowAuthError(
                "Phiên đăng nhập Google Flow đã hết hạn hoặc bị chuyển về trang đăng nhập. "
                "Hãy chạy scripts/save_flow_auth.py để đăng nhập lại."
            )
        LOGGER.warning(
            "⚠️ Cửa sổ Chrome đang ở trang đăng nhập / giới thiệu Google Flow (%s). "
            "Hãy đăng nhập hoặc bấm vào Project ngay trên cửa sổ Chrome đang mở (đang chờ tối đa 3 phút)...",
            page.url,
        )
        login_deadline = time.monotonic() + 180.0
        while time.monotonic() < login_deadline:
            cur = (page.url or "").lower()
            if (
                "/project/" in cur
                and "accounts.google.com" not in cur
                and not cur.rstrip("/").endswith("/about")
            ):
                break
            await asyncio.sleep(2.0)
        else:
            raise FlowAuthError(
                "Phiên đăng nhập Google Flow đã hết hạn và chưa hoàn tất đăng nhập trên trình duyệt."
            )

    try:
        await page.locator(
            f'{selectors["prompt"]}, [contenteditable="true"], textarea'
        ).first.wait_for(state="visible", timeout=60_000)
    except PlaywrightTimeoutError as error:
        raise FlowAuthError(
            "The saved Flow session did not open the project editor. "
            "Run scripts/save_flow_auth.py, sign in in its Chromium window, "
            "open the configured project, and save the session again."
        ) from error

    try:
        await context.storage_state(path=str(auth_file))
    except Exception:
        pass


async def _generate_one_clip(
    page: Any,
    context: Any,
    auth_file: Path,
    selectors: dict[str, str],
    video_prompt: str,
    label: str,
    item_id: str,
    idea: dict[str, Any],
    filename: str,
    render_timeout_ms: int,
    use_agent_tab: bool = True,
    headless: bool = False,
    character_tags: list[str] | None = None,
    expected_dialogue: str = "",
) -> Path:
    """Open the project, select the Agent tab (when enabled), paste the prompt, wait until it finishes, then download it."""
    await _open_flow_project_and_verify_session(
        page, context, auth_file, selectors, headless=headless
    )

    if use_agent_tab:
        await _ensure_agent_tab_open(page, selectors)
    else:
        try:
            await _configure_video_settings(page, selectors)
        except FlowDurationSelectionError:
            raise
        except Exception as exc:
            LOGGER.warning(
                "Không mở được bảng Settings trigger (%s); giữ nguyên cấu hình Video/9:16 hiện tại của Project và tiếp tục gửi prompt...",
                exc,
            )
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    results = page.locator(selectors["result"])
    await _wait_for_initial_grid_settle(page, results)
    await _mark_existing_tiles(results)
    initial_tiles = await _inspect_tiles(results)
    previous_labels = [t.get("label") for t in initial_tiles]
    previous_srcs = {str(t.get("src")) for t in initial_tiles if t.get("src")}
    LOGGER.info(
        "Đã mở Project Flow (hiện có %d video sẵn có). Đang dán kịch bản vào %s để tạo %s...",
        len(previous_labels),
        "tab Agent" if use_agent_tab else "khung Prompt",
        label,
    )
    await _paste_into_flow_prompt_and_submit(
        page,
        selectors,
        video_prompt,
        character_tags=character_tags,
    )

    new_result_index = await _wait_for_new_video_tile_ready(
        page,
        results,
        previous_labels,
        previous_srcs,
        timeout_ms=render_timeout_ms,
        prompt_text=video_prompt,
        approve_agent_actions=use_agent_tab,
    )
    return await _open_tile_and_download(
        page,
        results,
        new_result_index,
        selectors,
        item_id,
        idea,
        render_timeout_ms=render_timeout_ms,
        filename=filename,
        expected_prompt=video_prompt,
        expected_dialogue=expected_dialogue,
    )


async def _generate_agent_multi_scene_clips(
    page: Any,
    context: Any,
    auth_file: Path,
    selectors: dict[str, str],
    script_text: str,
    item_id: str,
    idea: dict[str, Any],
    base_name: str,
    expected_count: int,
    render_timeout_ms: int,
    headless: bool = False,
) -> list[Path]:
    """Open Flow project, select Agent tab, paste full 5-part script, wait for expected_count videos, and download all of them."""
    await _open_flow_project_and_verify_session(
        page, context, auth_file, selectors, headless=headless
    )

    await _ensure_agent_tab_open(page, selectors)

    results = page.locator(selectors["result"])
    await _wait_for_initial_grid_settle(page, results)
    await _mark_existing_tiles(results)
    initial_tiles = await _inspect_tiles(results)
    initial_tile_count = len(initial_tiles)
    previous_label_counts = Counter(t.get("label") for t in initial_tiles)
    previous_srcs = {str(t.get("src")) for t in initial_tiles if t.get("src")}
    LOGGER.info(
        "Đã mở Project Flow (hiện có %d video sẵn có). Đang dán kịch bản 5 phần vào tab Agent (kỳ vọng tải %d video)...",
        initial_tile_count,
        expected_count,
    )

    await _paste_into_flow_prompt_and_submit(
        page,
        selectors,
        script_text,
        character_tags=_idea_character_tags(idea),
    )

    start_time = time.monotonic()
    last_agent_action_time: float | None = None
    deadline = start_time + (render_timeout_ms / 1000.0)
    last_log_time = 0.0
    ready_new_tiles: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        # Auto-click Approve in Agent sidebar if Agent proposes an action plan
        try:
            approve_btn = page.locator(
                '[aria-label="Approve"][aria-checked="false"]:visible, '
                'button:has-text("Approve"):visible'
            )
            if await approve_btn.count() > 0:
                btn = approve_btn.first
                if await btn.is_visible() and await btn.is_enabled():
                    LOGGER.info("Phát hiện nút Approve của Flow Agent, đang tự động bấm Approve...")
                    await btn.click(timeout=4_000)
                    last_agent_action_time = time.monotonic()
                    await asyncio.sleep(2.5)
        except Exception:
            pass

        tiles = await _inspect_tiles(results)
        elapsed = int(time.monotonic() - start_time)
        since_agent_action = (
            int(time.monotonic() - last_agent_action_time)
            if last_agent_action_time is not None
            else elapsed
        )

        # Determine genuinely new tiles at the top of the grid
        added_count = max(0, len(tiles) - initial_tile_count)
        remaining_old = Counter(previous_label_counts)
        new_indices: list[int] = []
        for idx, tile in enumerate(tiles):
            lbl = tile.get("label")
            if idx < added_count:
                new_indices.append(idx)
            elif remaining_old[lbl] > 0:
                remaining_old[lbl] -= 1
            else:
                new_indices.append(idx)

        if since_agent_action >= 20 and new_indices:
            for idx in new_indices[: expected_count + 2]:
                tile = tiles[idx]
                if not tile.get("hasVideo") and not tile.get("hasProgress") and not tile.get("percent"):
                    try:
                        await results.nth(idx).hover(timeout=1_500)
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass
            tiles = await _inspect_tiles(results)

        ready_candidates: list[tuple[int, dict[str, Any]]] = []
        in_progress_count = 0
        for idx in new_indices:
            if idx >= len(tiles):
                continue
            tile = tiles[idx]
            if tile.get("hasProgress") or tile.get("percent"):
                in_progress_count += 1
            elif tile.get("hasVideo") and since_agent_action >= 20:
                ready_candidates.append((idx, tile))
        ready_candidates = _dedupe_indexed_video_tiles(ready_candidates)

        if time.monotonic() - last_log_time >= 8.0:
            LOGGER.info(
                "Tab Agent đang xử lý: %d/%d video mới đã sẵn sàng (tổng ô mới: %d, đang render: %d, đã chờ %ds)...",
                len(ready_candidates),
                expected_count,
                len(new_indices),
                in_progress_count,
                elapsed,
            )
            last_log_time = time.monotonic()

        if len(ready_candidates) >= expected_count and in_progress_count == 0:
            LOGGER.info(
                "Đã nhận đủ %d/%d video mới từ Google Flow Agent (sau %ds)! Chuẩn bị tải xuống...",
                len(ready_candidates),
                expected_count,
                elapsed,
            )
            await asyncio.sleep(3.0)
            ready_new_tiles = [
                {"index": idx, "src": str(t.get("src") or ""), "label": str(t.get("label") or "")}
                for idx, t in ready_candidates[:expected_count]
            ]
            break

        # If the Agent stops after a single scene, explicitly ask it to render the rest.
        if (
            len(ready_candidates) >= 1
            and in_progress_count == 0
            and since_agent_action >= 90
        ):
            completed_count = len(ready_candidates)
            if completed_count < expected_count:
                continuation_prompt = _agent_continuation_prompt(
                    idea, completed_count
                )
                LOGGER.info(
                    "Flow Agent đã hoàn tất %d/%d cảnh nhưng chưa tiếp tục. "
                    "Đang yêu cầu tạo các cảnh còn lại...",
                    completed_count,
                    expected_count,
                )
                await _paste_into_flow_prompt_and_submit(
                    page,
                    selectors,
                    continuation_prompt,
                )
                last_agent_action_time = time.monotonic()
                await asyncio.sleep(2.5)
                continue

        await asyncio.sleep(3.0)

    if not ready_new_tiles:
        raise RuntimeError(
            f"Quá thời gian chờ Google Flow Agent render xong video ({render_timeout_ms // 1000}s)."
        )

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    scenes = [s for s in idea.get("scenes", []) if isinstance(s, dict)]
    download_timeout_ms = int(get_setting("FLOW_DOWNLOAD_TIMEOUT_MS", "900000"))

    # Click the first new tile to enter <flow-scene-builder> (/edit/**) where we can read
    # <flow-expandable-prompt> for each thumbnail in <flow-navigation-rail> and download in exact Scene order.
    entered_scene_builder = False
    try:
        await results.nth(int(ready_new_tiles[0]["index"])).click(timeout=10_000)
        await page.wait_for_url("**/edit/**", timeout=30_000)
        await page.locator(
            'canvas.video-canvas, button[aria-label="Download media"], flow-navigation-rail-thumbnail'
        ).first.wait_for(state="visible", timeout=20_000)
        await asyncio.sleep(1.5)
        entered_scene_builder = True
    except Exception as exc:
        LOGGER.warning("Không mở được giao diện Scene Builder (%s); sẽ tải trực tiếp từ danh sách...", exc)

    used_scene_numbers: set[int] = set()
    scene_to_path: dict[int, Path] = {}

    if entered_scene_builder:
        rail_buttons = page.locator("flow-navigation-rail-thumbnail button.thumbnail-button")
        rail_count = await rail_buttons.count()
        valid_rail_indices: list[int] = []
        for r_idx in range(min(rail_count, len(ready_new_tiles) + 4)):
            try:
                lbl = (await rail_buttons.nth(r_idx).get_attribute("aria-label") or "").strip()
            except Exception:
                lbl = ""
            if lbl.lower().startswith("untitled scene"):
                continue
            valid_rail_indices.append(r_idx)
            if len(valid_rail_indices) >= len(ready_new_tiles):
                break

        for pos, r_idx in enumerate(valid_rail_indices):
            btn = rail_buttons.nth(r_idx)
            try:
                is_active = (await btn.get_attribute("aria-current")) == "true" or "active" in (
                    await btn.get_attribute("class") or ""
                )
                if not is_active:
                    await btn.click(timeout=6_000)
                    await asyncio.sleep(1.8)
            except Exception:
                pass

            thumb_label = ""
            try:
                thumb_label = (await btn.get_attribute("aria-label") or "").strip()
            except Exception:
                pass

            rendered_prompt = await _read_scene_builder_prompt_text(page)
            matched_scene = _match_prompt_to_scene_number(
                rendered_prompt, scenes, used_scene_numbers
            )
            if matched_scene is None:
                # Default to natural 1..N order if prompt text was not matched
                for candidate_num in range(1, len(ready_new_tiles) + 1):
                    if candidate_num not in used_scene_numbers:
                        matched_scene = candidate_num
                        break
            assert matched_scene is not None
            used_scene_numbers.add(matched_scene)

            clip_filename = (
                f"{base_name}-scene{matched_scene}.mp4"
                if len(ready_new_tiles) > 1
                else f"{base_name}.mp4"
            )
            dest_path = DOWNLOADS_DIR / clip_filename
            LOGGER.info(
                "🔍 Thumbnail #%d ('%s') khớp với Scene %d (prompt: '%s...'). Đang tải về %s...",
                pos + 1,
                thumb_label or f"Clip {pos + 1}",
                matched_scene,
                (rendered_prompt[:65] if rendered_prompt else "N/A"),
                dest_path.name,
            )

            saved_path = await _trigger_scene_builder_download(
                page, selectors, dest_path, download_timeout_ms
            )
            if saved_path is None and pos < len(ready_new_tiles):
                fallback_src = ready_new_tiles[pos].get("src", "")
                if fallback_src.startswith("https://"):
                    saved_path = await _download_video_url_to_path(page, fallback_src, dest_path)
                    LOGGER.info("✓ Đã tải Scene %d qua URL nguồn về: %s", matched_scene, dest_path.name)

            if saved_path is not None:
                scene_to_path[matched_scene] = saved_path

    # Fallback for any scene not downloaded via Scene Builder
    for pos, tile_info in enumerate(ready_new_tiles, start=1):
        scene_num = pos if pos not in scene_to_path else None
        if scene_num is None:
            for c_num in range(1, len(ready_new_tiles) + 1):
                if c_num not in scene_to_path:
                    scene_num = c_num
                    break
        if scene_num is None:
            continue

        clip_filename = (
            f"{base_name}-scene{scene_num}.mp4"
            if len(ready_new_tiles) > 1
            else f"{base_name}.mp4"
        )
        dest_path = DOWNLOADS_DIR / clip_filename
        src_url = tile_info.get("src", "")
        if src_url.startswith("https://"):
            try:
                await _download_video_url_to_path(page, src_url, dest_path)
                LOGGER.info("✓ Đã tải xong Scene %d: %s", scene_num, dest_path.name)
                scene_to_path[scene_num] = dest_path
            except Exception as exc:
                LOGGER.warning("Không tải được URL trực tiếp cho Scene %d: %s", scene_num, exc)

    ordered_clips = [scene_to_path[k] for k in sorted(scene_to_path.keys())]
    if not ordered_clips:
        raise RuntimeError("Không tải được video nào từ Google Flow.")
    LOGGER.info(
        "✓ Đã tải đủ %d video theo đúng thứ tự Scene (%s).",
        len(ordered_clips),
        ", ".join(p.name for p in ordered_clips),
    )
    return ordered_clips


async def generate_video_clips(item_id: str, idea: dict[str, Any]) -> list[Path]:
    """Generate video on Google Flow (defaults to Agent tab mode + headless=False for visual debugging)."""
    ensure_storage()
    auth_file = setting_path("FLOW_AUTH_FILE", "auth.json")
    if not auth_file.is_file():
        raise FlowAuthError(
            f"Flow storage state not found: {auth_file}. Run scripts/save_flow_auth.py."
        )

    configured_mode = get_setting("FLOW_GENERATION_MODE", "agent").strip().lower()
    mode = _generation_mode_for_idea(idea, configured_mode)
    scenes = [scene for scene in idea.get("scenes", []) if isinstance(scene, dict)]
    headless = get_setting("FLOW_HEADLESS", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    use_agent_tab = _use_agent_tab_for_idea(
        idea,
        get_setting("FLOW_USE_AGENT_TAB", "true"),
    )
    render_timeout_ms = int(get_setting("FLOW_RENDER_TIMEOUT_MS", "900000"))
    base_name = _safe_filename(idea["title"], item_id)

    jobs: list[tuple[str, str, int, str, list[str], str]] = []
    if mode == "combined" or not scenes:
        jobs.append(
            (
                _combined_video_prompt(idea),
                f"video cho '{idea.get('title', item_id)}'",
                idea.get("duration_seconds", 10),
                base_name + ".mp4",
                [],
                "",
            )
        )
    else:
        for index, scene in enumerate(scenes, start=1):
            jobs.append(
                (
                    _scene_video_prompt(idea, scene),
                    f"Scene {index}/{len(scenes)} ({scene.get('duration', '')}) của '{idea.get('title', item_id)}'",
                    _scene_duration_seconds(scene),
                    f"{base_name}-scene{index}.mp4",
                    _scene_character_tags(scene),
                    str(scene.get("dialogue") or "").strip(),
                )
            )

    clips: list[Path] = []
    profile_dir = STORAGE_DIR / "chrome_flow_profile"
    async with async_playwright() as playwright:
        browser = None
        context = None
        try:
            if profile_dir.is_dir():
                try:
                    context = await playwright.chromium.launch_persistent_context(
                        user_data_dir=str(profile_dir),
                        channel="chrome",
                        headless=headless,
                        args=["--start-maximized"],
                        no_viewport=True if not headless else False,
                    )
                except Exception:
                    context = None
            if context is None:
                try:
                    browser = await playwright.chromium.launch(channel="chrome", headless=headless)
                except Exception:
                    browser = await playwright.chromium.launch(headless=headless)
                context = await browser.new_context(storage_state=str(auth_file))
            else:
                try:
                    saved_state = json.loads(auth_file.read_text(encoding="utf-8"))
                    if isinstance(saved_state, dict) and isinstance(saved_state.get("cookies"), list):
                        await context.add_cookies(saved_state["cookies"])
                except Exception:
                    pass

            page = context.pages[0] if context.pages else await context.new_page()
            if mode == "agent" and scenes:
                script_text = format_flow_agent_prompt(idea)
                expected_count = len(scenes)
                LOGGER.info(
                    "▶ Gửi tất cả %d Scene cho Flow Agent trong một prompt cho '%s' [headless=%s]...",
                    expected_count,
                    idea.get("title", item_id),
                    headless,
                )
                clips = await _generate_agent_multi_scene_clips(
                    page=page,
                    context=context,
                    auth_file=auth_file,
                    selectors=_flow_selectors(idea.get("duration_seconds", 20)),
                    script_text=script_text,
                    item_id=item_id,
                    idea=idea,
                    base_name=base_name,
                    expected_count=expected_count,
                    render_timeout_ms=render_timeout_ms,
                    headless=headless,
                )
            else:
                job_uses_agent_tab = use_agent_tab
                for job_index, (
                    video_prompt,
                    label,
                    seconds,
                    filename,
                    character_tags,
                    dialogue,
                ) in enumerate(jobs, start=1):
                    LOGGER.info(
                        "▶ Bắt đầu tạo %s (%d/%d) [headless=%s, agent_tab=%s]...",
                        label,
                        job_index,
                        len(jobs),
                        headless,
                        job_uses_agent_tab,
                    )
                    clip = await _generate_one_clip(
                        page,
                        context,
                        auth_file,
                        _flow_selectors(seconds),
                        video_prompt,
                        label,
                        item_id,
                        idea,
                        filename,
                        render_timeout_ms,
                        use_agent_tab=job_uses_agent_tab,
                        headless=headless,
                        character_tags=character_tags,
                        expected_dialogue=dialogue,
                    )
                    clips.append(clip)
                    LOGGER.info(
                        "✓ Đã tải xong %s (%d/%d): %s",
                        label,
                        job_index,
                        len(jobs),
                        clip.name,
                    )
        finally:
            if browser is not None:
                await browser.close()
            elif context is not None:
                await context.close()
    return _dedupe_clip_files(clips)


_last_generated_clips: dict[str, list[Path]] = {}


def get_scene_clips_for_item(
    item_id: str, video_path: Path | str | None = None
) -> list[Path]:
    """Return the list of individual scene clips for an item."""
    if item_id in _last_generated_clips:
        cached = [p for p in _last_generated_clips[item_id] if p.is_file()]
        if cached:
            return cached

    if video_path:
        p = Path(video_path)
        parent = p.parent
        stem = p.stem
        if stem.endswith("-full"):
            base_prefix = stem[:-5]
            found = sorted(parent.glob(f"{base_prefix}-scene*.mp4"))
            if found:
                return [f for f in found if f.is_file()]
    return []


async def generate_video(item_id: str, idea: dict[str, Any]) -> Path:
    """Generate all clips and return a single file (merged when there are several)."""
    clips = await generate_video_clips(item_id, idea)
    _last_generated_clips[item_id] = [c for c in clips if c.is_file()]
    if len(clips) == 1:
        return clips[0]
    merged = merge_clips(
        clips, DOWNLOADS_DIR / (_safe_filename(idea["title"], item_id) + "-full.mp4")
    )
    return merged or clips[0]


async def _legacy_generate_video(item_id: str, idea: dict[str, Any]) -> Path:
    ensure_storage()
    auth_file = setting_path("FLOW_AUTH_FILE", "auth.json")
    if not auth_file.is_file():
        raise FlowAuthError(
            f"Flow storage state not found: {auth_file}. Run scripts/save_flow_auth.py."
        )

    selectors = {
        "agent_toggle": get_setting("FLOW_AGENT_TOGGLE_SELECTOR"),
        "settings": get_setting("FLOW_SETTINGS_SELECTOR"),
        "video_mode": get_setting("FLOW_VIDEO_MODE_SELECTOR"),
        "aspect_ratio": get_setting("FLOW_ASPECT_RATIO_SELECTOR"),
        "duration": _duration_selector(
            get_setting("FLOW_DURATION_SELECTOR"),
            idea.get("duration_seconds", 10),
        ),
        "output_count": get_setting("FLOW_OUTPUT_COUNT_SELECTOR"),
        "prompt": get_setting("FLOW_PROMPT_SELECTOR"),
        "submit": get_setting("FLOW_SUBMIT_SELECTOR"),
        "result": get_setting("FLOW_RESULT_SELECTOR"),
        "download_menu": get_setting("FLOW_DOWNLOAD_MENU_SELECTOR"),
        "download": get_setting("FLOW_DOWNLOAD_SELECTOR"),
    }

    headless = get_setting("FLOW_HEADLESS", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    render_timeout_ms = int(get_setting("FLOW_RENDER_TIMEOUT_MS", "900000"))

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chrome",
                headless=headless,
            )
        except Exception:
            browser = await playwright.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(auth_file))
            page = await context.new_page()
            await page.goto(
                get_setting("FLOW_PROJECT_URL"),
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            try:
                await page.locator(selectors["prompt"]).wait_for(
                    state="visible", timeout=60_000
                )
            except PlaywrightTimeoutError as error:
                raise RuntimeError(
                    "The saved Flow session did not open the project editor. "
                    "Run scripts/save_flow_auth.py, sign in in its Chromium window, "
                    "open the configured project, and save the session again."
                ) from error
            await _ensure_direct_video_composer(page, selectors)
            try:
                await _configure_video_settings(page, selectors)
                try:
                    await context.storage_state(path=str(auth_file))
                except Exception:
                    pass
            except Exception as exc:
                LOGGER.warning(
                    "Không mở được bảng Settings trigger (%s); giữ nguyên cấu hình Video/9:16 hiện tại của Project và tiếp tục gửi prompt...",
                    exc,
                )
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
            results = page.locator(selectors["result"])
            await _wait_for_initial_grid_settle(page, results)
            await _mark_existing_tiles(results)
            initial_tiles = await _inspect_tiles(results)
            previous_labels = [t.get("label") for t in initial_tiles]
            previous_srcs = {
                str(t.get("src")) for t in initial_tiles if t.get("src")
            }
            LOGGER.info(
                "Đã mở Project Flow (hiện có %d video sẵn có). Đang gửi prompt tạo video cho '%s'...",
                len(previous_labels),
                idea.get("title", item_id),
            )
            video_prompt = _combined_video_prompt(idea)
            await page.locator(selectors["prompt"]).fill(video_prompt)
            await page.locator(selectors["submit"]).click()

            new_result_index = await _wait_for_new_video_tile_ready(
                page,
                results,
                previous_labels,
                previous_srcs,
                timeout_ms=render_timeout_ms,
                prompt_text=video_prompt,
            )
            return await _open_tile_and_download(
                page,
                results,
                new_result_index,
                selectors,
                item_id,
                idea,
                render_timeout_ms=render_timeout_ms,
            )
        finally:
            await browser.close()


async def download_latest_video(item_id: str, idea: dict[str, Any]) -> Path:
    ensure_storage()
    auth_file = setting_path("FLOW_AUTH_FILE", "auth.json")
    if not auth_file.is_file():
        raise FlowAuthError(
            f"Flow storage state not found: {auth_file}. Run scripts/save_flow_auth.py."
        )

    selectors = {
        "prompt": get_setting("FLOW_PROMPT_SELECTOR"),
        "result": get_setting("FLOW_RESULT_SELECTOR"),
        "download_menu": get_setting("FLOW_DOWNLOAD_MENU_SELECTOR"),
        "download": get_setting("FLOW_DOWNLOAD_SELECTOR"),
    }
    headless = get_setting("FLOW_HEADLESS", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    render_timeout_ms = int(get_setting("FLOW_RENDER_TIMEOUT_MS", "900000"))

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chrome",
                headless=headless,
            )
        except Exception:
            browser = await playwright.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(storage_state=str(auth_file))
            page = await context.new_page()
            await page.goto(
                get_setting("FLOW_PROJECT_URL"),
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await page.locator(selectors["prompt"]).wait_for(
                state="visible", timeout=60_000
            )
            results = page.locator(selectors["result"])
            await _wait_for_initial_grid_settle(page, results)
            LOGGER.info(
                "Đang kiểm tra video mới nhất trên Project Flow để tải cho '%s' (%s)...",
                idea.get("title", item_id),
                item_id,
            )
            tile_index = await _wait_for_new_video_tile_ready(
                page,
                results,
                [],
                set(),
                timeout_ms=render_timeout_ms,
            )
            return await _open_tile_and_download(
                page,
                results,
                tile_index,
                selectors,
                item_id,
                idea,
                render_timeout_ms=render_timeout_ms,
            )
        finally:
            await browser.close()


def read_approved_queue() -> list[dict[str, Any]]:
    ensure_storage()
    with FileLock(str(QUEUE_LOCK_FILE)):
        return read_json(APPROVED_QUEUE_FILE, [])


def acknowledge_processed(item_id: str) -> None:
    remove_approved_item(item_id)
