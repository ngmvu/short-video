from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from google import genai

from core.config import ROOT_DIR, get_setting
from core.social_uploader import build_youtube_shorts_title

LOGGER = logging.getLogger("short_video_distributor")

SKILL_FILE_PATH = ROOT_DIR / "SKILL.md"


def _load_skill_instructions() -> str:
    """Load SKILL.md (video-hai-two-shot-creator) instructions from project root."""
    for candidate in (
        SKILL_FILE_PATH,
        Path(__file__).resolve().parent.parent.parent / "SKILL.md",
    ):
        try:
            if candidate.is_file():
                content = candidate.read_text(encoding="utf-8").strip()
                if content:
                    return content
        except Exception:
            continue
    return ""


def _load_recent_draft_titles(limit: int = 12) -> list[str]:
    """Return recent draft titles from storage/drafts.json to avoid duplicate topics."""
    recent_titles: list[str] = []
    for candidate in (
        ROOT_DIR / "storage" / "drafts.json",
        Path(__file__).resolve().parent.parent / "storage" / "drafts.json",
    ):
        try:
            if candidate.is_file():
                saved_drafts = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(saved_drafts, list):
                    for item in saved_drafts[-limit:]:
                        if isinstance(item, dict) and isinstance(item.get("idea"), dict):
                            t = str(item["idea"].get("title") or "").strip()
                            if t and t not in recent_titles:
                                recent_titles.append(t)
                if recent_titles:
                    break
        except Exception:
            continue
    return recent_titles


DEFAULT_FREE_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-2.5-pro",
)

FIXED_KHI_IMAGE_PROMPT = (
    "on the left, mischievous mechanic monkey wearing a dirty greasy white tank top "
    "and dark mechanic work trousers holding a wrench"
)
FIXED_NGUOI_IMAGE_PROMPT = (
    "on the right, middle-aged Vietnamese uncle wearing a dark matte half-helmet "
    "and dark grey work shirt"
)

DEFAULT_IDEA_STYLE = (
    "Kịch bản video ngắn hài dân gian đời thường Việt Nam bắt trend và viral mạnh trên TikTok, Facebook Reels và YouTube Shorts "
    "theo chuẩn SKILL video-hai-two-shot-creator. "
    "Bộ đôi cố định: @khi (luôn ở bên trái - on the left, mặc áo ba lỗ trắng cũ dính dầu mỡ lấm lem và quần dài thợ máy tối màu, cầm cờ-lê, "
    "xưng 'con/tui' gọi 'chú', giọng cao ranh mãnh 'high-pitched mischievous Vietnamese lip sync') là khỉ ma ranh, mồm mép, kèo trên và chủ tiệm sửa xe; "
    "@nguoi (luôn ở bên phải - on the right, đội mũ bảo hiểm nửa đầu màu tối và mặc áo thun thợ màu xám đen tối màu, "
    "xưng 'tao' gọi 'mày/khỉ', giọng trầm già gắt 'deep gruff older Vietnamese lip sync') là ông chú trung niên chất phác, nóng tính, "
    "thích thể hiện, sĩ diện, sợ vợ nhưng thường bị khỉ bắt bài. Giữ đúng cá tính, trang phục và quan hệ này. "
    "BỐI CẢNH & CHỦ ĐỀ HOÀN TOÀN TỰ DO, KHÔNG nhất thiết ở tiệm sửa xe (có thể ở chợ, quán cà phê, đám cưới, đám giỗ, bệnh viện, công sở, trường học, du lịch, nhà hàng, xóm trọ, livestream bán hàng, đồng ruộng, sân bóng, siêu thị, phòng gym, bến xe, tiệm tóc, ngân hàng, văn phòng công an phường... hoặc bất kỳ đâu hợp chủ đề); ngoại hình và tính cách bộ đôi giữ nguyên dù ở bối cảnh nào. Đa dạng tình huống hài dân gian trong đời sống Việt Nam: bắt chữ nghĩa đen / chơi chữ lươn lẹo, chữa lợn lành thành lợn què, "
    "ông chú sĩ diện bị bóc mẽ, dịch vụ khởi nghiệp bá đạo, hỏi xoáy đáp xoay, cà khịa, khách mặc cả, xe hỏng, gia đình, hàng xóm. "
    "Khi hai nhân vật đối thoại phải nhìn trực diện vào nhau (looking directly at each other), chỉ liếc nhìn camera ở nhịp gây cười hoặc cảnh kết. "
    "BẮT BUỘC đổi vai luân phiên nghiêm ngặt (Scene 1 Nhân vật A nói -> Scene 2 Nhân vật B nói -> Scene 3 Nhân vật A nói, cấm 1 nhân vật nói 2 cảnh liền). "
    "Mỗi Scene chỉ đúng 1 người nói; người nghe bắt buộc khóa câm rõ ràng (remains completely silent with closed mouth) nhưng phản ứng vi biểu cảm sống động, không đứng hình. "
    "ĐẶT DURATION: AUTO (cấm hardcode số giây cứng), thoại mỗi cảnh ngắn gọn súc tích tối đa 12-18 chữ tiếng Việt, vào thoại ngay từ đầu cảnh. "
    "Scene cuối luôn là đỉnh điểm gây cười: @nguoi chốt câu mắng 'Đúng là khỉ!' (chỉ xuất hiện duy nhất 1 lần ở cảnh cuối) và @khi cười vui vẻ hả hê."
)


_POLICY_SAFE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"\bchém gió\b", "nói phét"),
    (r"\bchém\b", "cắt"),
    (r"\bgiết\b", "hạ"),
    (r"\bđâm chết\b", "đụng trúng"),
    (r"\bmáu me\b", "lem luốc"),
    (r"\buncontrollable\b", "hearty"),
    (r"\bhysterical(?:ly)?\b", "joyful"),
    (r"\bviolently\b", "playfully"),
    (r"\bviolent\b", "lively"),
    (r"\bfuriously\b", "sternly"),
    (r"\baggressively\b", "firmly"),
    (r"\bsheer rage\b", "great frustration"),
    (r"\brage\b", "frustration"),
    (r"\bscreaming\b", "exclaiming"),
    (r"\bslapping\b", "patting"),
    (r"\bgloating,? triumphant\b", "cheerful, playful"),
    (r"\bwheezing\b", "happy"),
)


def sanitize_flow_prompt(prompt: str) -> str:
    """Soften words that commonly trigger Google Flow's harmful-content filter."""
    cleaned = prompt
    for pattern, replacement in _POLICY_SAFE_REPLACEMENTS:
        cleaned = re.sub(
            pattern,
            lambda m, r=replacement: r.capitalize() if m.group(0)[:1].isupper() else r,
            cleaned,
            flags=re.IGNORECASE,
        )
    return cleaned


def _clean_scene_prompt(prompt: str) -> str:
    cleaned = prompt.strip()
    cleaned = re.sub(
        r"^(?:Vertical\s+9:16\s+framing\.?\s*|Handheld\s+camera\s+style\.?\s*|Climax\.?\s*)+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    return sanitize_flow_prompt(cleaned)


def _format_hashtag_str(raw: Any, default_str: str) -> str:
    if isinstance(raw, list):
        tokens: list[str] = []
        for item in raw:
            if isinstance(item, str):
                tokens.extend(item.replace(",", " ").split())
    elif isinstance(raw, str):
        tokens = raw.replace(",", " ").split()
    else:
        tokens = []
    cleaned: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        tag = token.strip().lstrip("#").strip()
        if not tag:
            continue
        low = tag.lower()
        if low not in seen:
            seen.add(low)
            cleaned.append(f"#{tag}")
    return " ".join(cleaned) if cleaned else default_str


def format_five_part_script(idea: dict[str, Any]) -> str:
    """Format an idea dictionary into the 5-part Vietnamese script & Google Flow Agent prompt."""
    if isinstance(idea.get("full_script_text"), str) and idea["full_script_text"].strip():
        return idea["full_script_text"].strip()

    title = str(idea.get("title") or "Video ngắn hài hước").strip()
    setting = str(
        idea.get("setting")
        or "Không gian đời thường bình dân Việt Nam gần gũi, chi tiết sống động"
    ).strip()
    twist_highlight = str(
        idea.get("twist_highlight")
        or "Tình huống đảo ngược bất ngờ giữa lời nói khoác lác ban đầu và phản ứng thật hài hước lúc cao trào."
    ).strip()
    duration_seconds = idea.get("duration_seconds", 20)
    total_duration_label = str(
        idea.get("total_duration_label")
        or f"~{duration_seconds} giây (đã bù thêm 1–2 giây mỗi cảnh để thoại đọc thong thả, không bị ngắt cụt)"
    ).strip()

    scenes = [s for s in idea.get("scenes", []) if isinstance(s, dict)]
    last_dialogue = ""
    for s in reversed(scenes):
        if str(s.get("dialogue") or "").strip():
            last_dialogue = str(s["dialogue"]).strip()
            break
    viral_quote = str(idea.get("viral_quote") or last_dialogue or title).strip().strip('"')

    hook_summary = str(
        idea.get("hook_summary")
        or (scenes[0].get("scene_title") if len(scenes) >= 1 else "Mở đầu gây chú ý ngay giây đầu tiên.")
        or "Mở đầu gây chú ý ngay giây đầu tiên."
    ).strip()
    conflict_summary = str(
        idea.get("conflict_summary")
        or (scenes[1].get("scene_title") if len(scenes) >= 2 else "Tình huống bất ngờ đẩy mâu thuẫn lên cao.")
        or "Tình huống bất ngờ đẩy mâu thuẫn lên cao."
    ).strip()
    twist_summary = str(
        idea.get("twist_summary")
        or (scenes[2].get("scene_title") if len(scenes) >= 3 else "Màn bóc mẽ hài hước và cái kết quê độ.")
        or "Màn bóc mẽ hài hước và cái kết quê độ."
    ).strip()

    if not hook_summary.lower().startswith("hook"):
        hook_summary = f"Hook: {hook_summary}"
    if not conflict_summary.lower().startswith("conflict"):
        conflict_summary = f"Conflict: {conflict_summary}"
    if not twist_summary.lower().startswith("twist"):
        twist_summary = f"Twist: {twist_summary}"

    scene_blocks: list[str] = []
    for idx, scene in enumerate(scenes, start=1):
        first_frame = str(scene.get("first_frame") or "").strip()
        prompt_str = str(scene.get("prompt") or "").strip()
        if first_frame:
            scene_blocks.append(
                f"Scene {idx}\nInput / First Frame: {first_frame}\n\n{prompt_str}"
            )
        else:
            scene_blocks.append(f"Scene {idx}\n\n{prompt_str}")

    overlay_text = str(
        idea.get("overlay_text") or f"Khi {title.lower()}..."
    ).strip().strip('"')
    hashtags_str = _format_hashtag_str(
        idea.get("hashtags")
        or idea.get("social_posts", {}).get("tiktok", {}).get("hashtags"),
        "#chuyendoithuong #haivl #reelsfb #trending",
    )
    raw_caption = str(
        idea.get("caption")
        or idea.get("social_posts", {}).get("facebook", {}).get("caption")
        or f"Chuyện đời thường mà ai nghe cũng thấy quen: {twist_highlight}"
    ).strip().strip('"')
    if hashtags_str and hashtags_str.split()[0].lower() not in raw_caption.lower():
        full_caption = f"{raw_caption} {hashtags_str}".strip()
    else:
        full_caption = raw_caption

    engagement_prompt = str(
        idea.get("engagement_prompt")
        or "Bạn từng gặp tình huống giống vậy chưa? Kể lại ở phần bình luận nhé!"
    ).strip().strip('"')

    scenes_joined = "\n\n".join(scene_blocks)
    return (
        f"PHẦN 1 – THÔNG TIN VIDEO\n"
        f"Tên video: {title}\n\n"
        f"Bối cảnh: {setting}\n\n"
        f"Điểm gây cười & Cú twist: {twist_highlight}\n\n"
        f"Thời lượng tổng: {total_duration_label}\n\n"
        f"Câu thoại viral nhất: \"{viral_quote}\"\n\n"
        f"PHẦN 2 – TÓM TẮT KỊCH BẢN\n"
        f"{hook_summary}\n\n"
        f"{conflict_summary}\n\n"
        f"{twist_summary}\n\n"
        f"PHẦN 3 – GOOGLE FLOW PROMPTS\n"
        f"{scenes_joined}\n\n"
        f"PHẦN 4 – TEXT & CAPTION\n"
        f"Text trên video: \"{overlay_text}\"\n\n"
        f"Caption: \"{full_caption}\"\n\n"
        f"Hashtag: {hashtags_str}\n\n"
        f"PHẦN 5 – GỢI Ý TƯƠNG TÁC\n"
        f"\"{engagement_prompt}\""
    )


def format_flow_agent_prompt(idea: dict[str, Any]) -> str:
    """Format only the scene generation prompt to paste into Google Flow's Agent tab (no PHẦN 1/2/4/5 and no hardcoded seconds)."""
    scenes = [s for s in idea.get("scenes", []) if isinstance(s, dict)]
    scene_count = len(scenes)
    lines: list[str] = [
        f"Create exactly {scene_count} separate video clips, one for each Scene below. "
        f"Generate all {scene_count} scenes; do not stop after the first. "
        "If Flow requires approval to continue, continue after approval until all "
        "requested scenes are generated.",
        "",
    ]
    char1_tag = _opt_setting("CHARACTER_1_TAG", "@khi")
    char1_voice = _opt_setting("CHARACTER_1_VOICE", "@Voice_Khi")
    char2_tag = _opt_setting("CHARACTER_2_TAG", "@nguoi")
    char2_voice = _opt_setting("CHARACTER_2_VOICE", "@Voice_Nguoi")

    for idx, scene in enumerate(scenes, start=1):
        first_frame = str(scene.get("first_frame") or "").strip()
        voice = str(scene.get("voice") or "").strip()
        if voice.casefold() == char1_voice.casefold():
            speaker_tag, listener_tag = char1_tag, char2_tag
        elif voice.casefold() == char2_voice.casefold():
            speaker_tag, listener_tag = char2_tag, char1_tag
        else:
            frame_tags = [
                tag
                for tag in re.findall(r"@[\w.]+", first_frame)
                if not tag.lower().startswith("@voice")
            ]
            frame_speaker = frame_tags[0] if frame_tags else ""
            speaker_tag = (
                frame_speaker
                if frame_speaker in {char1_tag, char2_tag}
                else (char2_tag if idx % 2 else char1_tag)
            )
            listener_tag = char1_tag if speaker_tag == char2_tag else char2_tag
        if not first_frame:
            speaker_voice = (
                char1_voice if speaker_tag == char1_tag else char2_voice
            )
            first_frame = f"{speaker_tag} + {listener_tag} + {speaker_voice}"
        prompt_str = sanitize_flow_prompt(str(scene.get("prompt") or "").strip())
        lines.append(
            f"Scene {idx} — Input / First Frame: {first_frame}: {prompt_str}\n"
            f"PERFORMANCE: {speaker_tag} is the only speaker, faces camera with a clearly "
            f"visible face, and lip-syncs the dialogue. {listener_tag} stays silent with "
            "a relaxed closed mouth and no lip-sync, but visibly reacts throughout with "
            "eye/head movement, changing expressions, nods, posture shifts, or small "
            "natural gestures; never freeze or hold a static pose."
        )
    return "\n".join(lines)


def _normalize_social_posts(
    raw_posts: Any,
    fallback_title: str,
    scenes: list[dict[str, Any]] | None = None,
    fallback_caption: str = "",
    fallback_hashtags: str = "",
) -> dict[str, dict[str, str]]:
    title = (fallback_title or "Video ngắn hài hước").strip()
    dialogue_summary = ""
    if isinstance(scenes, list):
        dialogues = [
            str(s.get("dialogue", "")).strip()
            for s in scenes
            if isinstance(s, dict) and str(s.get("dialogue", "")).strip()
        ]
        if dialogues:
            dialogue_summary = " ".join(dialogues[:2])

    base_caption = (
        fallback_caption.strip()
        or (f"{title}! {dialogue_summary}".strip() if dialogue_summary else f"{title} — Xem đến cuối để thấy pha xử lý đi vào lòng đất 😂")
    )
    base_hashtags = fallback_hashtags.strip()

    raw_dict = raw_posts if isinstance(raw_posts, dict) else {}
    tt_raw = raw_dict.get("tiktok") if isinstance(raw_dict.get("tiktok"), dict) else {}
    fb_raw = raw_dict.get("facebook") if isinstance(raw_dict.get("facebook"), dict) else {}
    yt_raw = raw_dict.get("youtube") if isinstance(raw_dict.get("youtube"), dict) else {}

    tt_title = str(tt_raw.get("title") or title).strip()[:150]
    tt_caption = str(tt_raw.get("caption") or base_caption).strip()
    tt_hashtags = _format_hashtag_str(
        tt_raw.get("hashtags") or base_hashtags,
        "#chuyendoithuong #haihuoc #xuhuong #tiktokvn #trending",
    )

    fb_title = str(fb_raw.get("title") or title).strip()[:150]
    fb_caption = str(fb_raw.get("caption") or base_caption).strip()
    fb_hashtags = _format_hashtag_str(
        fb_raw.get("hashtags") or base_hashtags,
        "#chuyendoithuong #haihuoc #reelsfb #trending",
    )

    yt_caption = str(yt_raw.get("caption") or base_caption).strip()
    yt_hashtags = _format_hashtag_str(
        yt_raw.get("hashtags") or base_hashtags,
        "#Shorts #chuyendoithuong #haihuoc #trending",
    )
    if "#shorts" not in yt_hashtags.lower():
        yt_hashtags = f"#Shorts {yt_hashtags}".strip()

    yt_raw_title = str(yt_raw.get("title") or title).strip()
    yt_title = build_youtube_shorts_title(yt_raw_title, yt_hashtags, max_length=100)

    return {
        "tiktok": {
            "title": tt_title,
            "caption": tt_caption,
            "hashtags": tt_hashtags,
        },
        "facebook": {
            "title": fb_title,
            "caption": fb_caption,
            "hashtags": fb_hashtags,
        },
        "youtube": {
            "title": yt_title,
            "caption": yt_caption,
            "hashtags": yt_hashtags,
        },
    }


def _validate_ideas(ideas: Any, scene_count: int = 3) -> list[dict[str, Any]]:
    if not isinstance(ideas, list) or len(ideas) < 2:
        raise ValueError("Gemini must return a JSON list containing at least 2 ideas.")

    for index, idea in enumerate(ideas, start=1):
        if not isinstance(idea, dict) or not isinstance(idea.get("title"), str):
            raise ValueError(f"Idea {index} is missing a string title.")
        for character_key in ("character_1", "character_2"):
            character = idea.get(character_key)
            if character is not None and (
                not isinstance(character, dict)
                or not isinstance(character.get("ingredient", ""), str)
            ):
                raise ValueError(f"Idea {index} has an invalid {character_key}.")
        scenes = idea.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scene_count:
            raise ValueError(
                f"Idea {index} must contain exactly {scene_count} scenes."
            )
        for scene_index, scene in enumerate(scenes, start=1):
            required = ("duration", "prompt", "type")
            if not isinstance(scene, dict) or not all(
                isinstance(scene.get(field), str) for field in required
            ):
                raise ValueError(
                    f"Idea {index}, scene {scene_index} is missing required fields."
                )
            if scene_index > 1 and not all(
                isinstance(scene.get(field), str)
                for field in ("dialogue", "voice")
            ):
                raise ValueError(
                    f"Idea {index}, scene {scene_index} needs dialogue and voice."
                )
    return ideas


def _opt_setting(key: str, default: str) -> str:
    try:
        value = get_setting(key, default)
        return value.strip() if isinstance(value, str) and value.strip() else default
    except (KeyError, ValueError):
        return default


def _candidate_api_keys() -> list[str]:
    primary_raw = _opt_setting("GEMINI_API_KEY", "") or _opt_setting(
        "GEMINI_API_KEY1", ""
    )
    if not primary_raw:
        primary_raw = get_setting("GEMINI_API_KEY")
    fallback_raw = _opt_setting("GEMINI_FALLBACK_API_KEYS", "")
    extra_keys = [
        _opt_setting("GEMINI_API_KEY1", ""),
        _opt_setting("GEMINI_API_KEY2", ""),
        _opt_setting("GEMINI_API_KEY3", ""),
        _opt_setting("GEMINI_API_KEY4", ""),
        _opt_setting("GEMINI_API_KEY_1", ""),
        _opt_setting("GEMINI_API_KEY_2", ""),
        _opt_setting("GEMINI_API_KEY_3", ""),
        _opt_setting("GEMINI_API_KEY_4", ""),
    ]
    keys: list[str] = []
    for raw in (primary_raw, fallback_raw, *extra_keys):
        for part in raw.replace(";", ",").split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in keys:
                keys.append(cleaned)
    return keys


def _candidate_models() -> list[str]:
    primary_model = get_setting("GEMINI_MODEL", "gemini-3.8-flash")
    custom_fallbacks_raw = _opt_setting("GEMINI_FALLBACK_MODELS", "")
    custom_fallbacks = [
        item.strip()
        for item in custom_fallbacks_raw.split(",")
        if item.strip()
    ]
    candidate_models: list[str] = []
    for model_name in (
        primary_model,
        *custom_fallbacks,
        *DEFAULT_FREE_FALLBACK_MODELS,
    ):
        if model_name and model_name not in candidate_models:
            candidate_models.append(model_name)
    return candidate_models


def _call_gemini_json(contents: Any, temperature: float = 0.9) -> str:
    api_keys = _candidate_api_keys()
    candidate_models = _candidate_models()
    response = None
    last_error: Exception | None = None
    clients_by_key: dict[str, Any] = {}
    try:
        for model_index, model_name in enumerate(candidate_models):
            succeeded = False
            for key_index, api_key in enumerate(api_keys):
                try:
                    if api_key not in clients_by_key:
                        clients_by_key[api_key] = genai.Client(api_key=api_key)
                    client = clients_by_key[api_key]
                    response = client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config={
                            "response_mime_type": "application/json",
                            "temperature": temperature,
                        },
                    )
                    if response and response.text:
                        if model_index > 0 or key_index > 0:
                            LOGGER.info(
                                "Đã tạo nội dung thành công bằng API key #%d trên model: %s",
                                key_index + 1,
                                model_name,
                            )
                        succeeded = True
                    else:
                        raise RuntimeError("Gemini returned an empty response.")
                except Exception as exc:
                    last_error = exc
                    has_next_key = key_index + 1 < len(api_keys)
                    has_next_model = model_index + 1 < len(candidate_models)
                    if has_next_key:
                        LOGGER.warning(
                            "Gemini API key #%d gặp lỗi trên model '%s' (%s). Đang chuyển sang API key dự phòng #%d...",
                            key_index + 1,
                            model_name,
                            exc,
                            key_index + 2,
                        )
                        continue
                    if has_next_model:
                        next_model = candidate_models[model_index + 1]
                        LOGGER.warning(
                            "Model '%s' đang quá tải hoặc gặp lỗi (%s). Đang tự động chuyển sang model miễn phí dự phòng '%s'...",
                            model_name,
                            exc,
                            next_model,
                        )
                        break
                    raise
                if succeeded:
                    break
            if succeeded:
                break
    finally:
        for client in clients_by_key.values():
            client.close()
    if response is None or not response.text:
        if last_error is not None:
            raise last_error
        raise RuntimeError("Gemini returned an empty response.")
    return response.text


def generate_ideas(
    prompt: str | None = None,
    scene_count: int = 3,
    duration_seconds: int = 10,
    ideas_count: int = 10,
) -> list[dict[str, Any]]:
    if not 1 <= scene_count <= 20:
        raise ValueError("Scene count must be between 1 and 20.")
    if not 1 <= duration_seconds <= 180:
        raise ValueError("Duration must be between 1 and 180 seconds.")
    if not 1 <= ideas_count <= 25:
        raise ValueError("Ideas count must be between 1 and 25.")

    idea_style = get_setting("IDEA_STYLE", DEFAULT_IDEA_STYLE)
    skill_instructions = _load_skill_instructions()
    char1_tag = _opt_setting("CHARACTER_1_TAG", "@khi")
    char1_voice = _opt_setting("CHARACTER_1_VOICE", "@Voice_Khi")
    char2_tag = _opt_setting("CHARACTER_2_TAG", "@nguoi")
    char2_voice = _opt_setting("CHARACTER_2_VOICE", "@Voice_Nguoi")
    custom_prompt = (prompt or "").strip()
    recent_titles = _load_recent_draft_titles(limit=10)

    avoid_note = (
        f"\nKhông lặp lại các tiêu đề/chủ đề gần đây này: {', '.join(recent_titles)}. "
        f"Tạo {ideas_count} tình huống mới, mỗi ý tưởng khác bối cảnh và mâu thuẫn."
        if recent_titles
        else ""
    )
    user_prompt = (
        (
            f"YÊU CẦU CHỦ ĐỀ ƯU TIÊN: {custom_prompt}\n"
            "Bám sát chủ đề người dùng đưa ra; tạo các biến thể khác nhau trong đúng chủ đề đó, "
            f"không đổi sang mô-típ khác. Luôn giữ {char1_tag} là khỉ chủ tiệm sửa xe "
            f"và {char2_tag} là ông chú (đây chỉ là nhận diện nhân vật; bối cảnh và nội dung truyện "
            "tự do theo chủ đề, KHÔNG bắt buộc diễn ra ở tiệm sửa xe)."
            + avoid_note
        )
        if custom_prompt
        else (
            f"No additional user prompt. Tạo {ideas_count} kịch bản hài có bộ đôi cố định: {char1_tag} là khỉ ma ranh, chủ tiệm sửa xe; {char2_tag} là ông chú trung niên sĩ diện, khôn lỏi nhưng hay gặp sự cố đời thường. Mỗi kịch bản có bối cảnh và mâu thuẫn riêng, diễn ở BẤT KỲ bối cảnh nào (chợ, quán cà phê, đám cưới, đám giỗ, bệnh viện, công sở, trường học, du lịch, nhà hàng, xóm trọ, livestream bán hàng, đồng ruộng, sân bóng, siêu thị, phòng gym, bến xe, tiệm tóc, ngân hàng, văn phòng công an phường...), không nhất thiết ở tiệm sửa xe. Ưu tiên hỏi xoáy đáp xoay, cà khịa duyên, tình huống sửa xe và drama đời thường được hư cấu hóa; không gán tin đồn cho người thật. Thoại tiếng Việt ngắn gọn, tối đa 12–18 từ mỗi cảnh, vào thoại ngay đầu clip. Cảnh cuối có cú bẻ lái bất ngờ nhưng hợp lý. Mỗi cảnh dùng duration auto."
            + avoid_note
        )
    )
    scene_examples: list[dict[str, str]] = []
    for index in range(scene_count):
        is_character_1_speaking = index % 2 == 1
        speaker_tag = char1_tag if is_character_1_speaking else char2_tag
        speaker_voice = char1_voice if is_character_1_speaking else char2_voice
        listener_tag = char2_tag if is_character_1_speaking else char1_tag
        speaker_pos = "on the left" if is_character_1_speaking else "on the right"
        listener_pos = "on the right" if is_character_1_speaking else "on the left"
        speaker_timbre = (
            "high-pitched mischievous Vietnamese lip sync"
            if is_character_1_speaking
            else "deep gruff older Vietnamese lip sync"
        )
        is_last_scene = index == scene_count - 1
        listener_action = (
            "remains completely silent with closed mouth, then bursts into cheerful, playful hearty laughter, head tilted back, holding belly, patting knee, shoulders bouncing with wide-smile giggles and funny gasps of gleeful satisfaction"
            if is_last_scene and listener_tag == char1_tag
            else "remains completely silent with closed mouth and no lip-sync, but visibly reacts throughout the line: follows the speaker with eyes/head, nods or raises eyebrows, shifts posture or makes a small fitting gesture"
        )
        scene_examples.append(
            {
                "scene_title": "<short title specific to this scene>",
                "duration": "auto",
                "first_frame": f"{speaker_tag} + {listener_tag} + {speaker_voice}",
                "prompt": (
                    f"Medium two-shot, vertical 9:16 in <everyday Vietnamese setting>: "
                    "Vietnamese language audio only, strictly no English speech: "
                    f"Speaker: {speaker_pos}, {speaker_tag} with {speaker_voice}, looking directly at {listener_tag} with clearly visible face, "
                    f'speaking immediately with {speaker_timbre}: "<short Vietnamese dialogue>". '
                    f"Listener: {listener_pos}, {listener_tag} {listener_action}. "
                    f"Keep the listener naturally animated, never frozen. Only {speaker_tag} speaks."
                ),
                "dialogue": "<short Vietnamese dialogue for this scene>",
                "voice": speaker_voice,
                "type": "punchline" if is_last_scene else "dialogue",
            }
        )

    scene_examples_json = json.dumps(scene_examples, ensure_ascii=False, indent=4)
    prompt_text = """
Create exactly {ideas_count} distinct Vietnamese short-video comedy scripts featuring the SAME duo:
`{char1_tag}` is a mischievous, sharp-tongued monkey who owns a neighborhood motorbike repair shop (always positioned `on the left`, wearing a dirty greasy white tank top and dark mechanic work trousers, holding or using mechanic tools, addressing `{char2_tag}` as "chú" and self as "con/tui");
`{char2_tag}` is a proud, street-smart middle-aged Vietnamese uncle who is usually the shop's customer
or the monkey's friend (always positioned `on the right`, wearing a dark matte half-helmet and dark grey work shirt, addressing `{char1_tag}` as "mày/khỉ" and self as "tao"). Keep their personalities consistent and make their interaction the comic engine.
SETTING FREEDOM: the story is NOT limited to the repair shop. Each script may happen in ANY everyday Vietnamese (or travel) setting and topic (market, cafe, wedding, hospital, office, school, trip, restaurant, rented-room neighborhood, livestream, rice field, football pitch, supermarket, gym, bus station, barber, bank...). Vary settings across scripts; only the two characters' look, voices and personalities stay fixed.
The user's selected topic is the primary brief. If it is VF3 drama, keep it fictional and anonymized:
do not use real people's names or present rumors as facts. Generate different funny scenarios within
that topic rather than switching to unrelated stories.
Give every script a specific believable setting and conflict with a surprising but logical final twist.
When no topic is supplied, vary formats across the 5 core Vietnamese folk-comedy archetypes:
(1) Bắt chữ nghĩa đen / chơi chữ lươn lẹo, (2) Chữa lợn lành thành lợn què, (3) Ông chú sĩ diện bị bóc mẽ,
(4) Dịch vụ / khởi nghiệp bá đạo, (5) Đú trend / công nghệ nửa mùa, (6) Hỏi ngu / hỏi dại nghe rất có lý, (7) Hót trend & meme đang viral hôm nay (generic and fictional, no real people), as well as hỏi ngu / hỏi dại nghe có lý, bắt trend & meme đang hot (hư cấu, không nhắc người thật), hỏi AI/Google rồi làm theo sái cổ, phỏng vấn xin việc, tỏ tình/xem mắt, review quán ăn, chuyện giá cả/lương/vé Tết, thầy bói tay ngang, bóng đá/thi đấu phong trào, mẹo vặt đời sống, tranh luận vô tri, hỏi xoáy đáp xoay, cà khịa ông chú,
shop/customer misunderstandings, everyday mechanic mishaps, family, neighbors, work, transport, and viral trends.
Keep each Vietnamese dialogue to 12–18 words maximum.
1. `{char1_tag}` uses voice `{char1_voice}` (`with high-pitched mischievous Vietnamese lip sync`).
2. `{char2_tag}` uses voice `{char2_voice}` (`with deep gruff older Vietnamese lip sync`).

SKILL INSTRUCTIONS (VIDEO-HÀI-TWO-SHOT-CREATOR – HÀI DÂN GIAN & GOOGLE FLOW CHUẨN FORM):
{skill_instructions}

CRITICAL SINGLE-FRAME TWO-SHOT & POSITION RULE:
- Every single scene MUST show both configured characters `{char1_tag}` (`on the left`) and `{char2_tag}` (`on the right`) together side-by-side in a `Medium two-shot`, `vertical 9:16` frame (strictly 2 characters total).
- Always include `Vietnamese language audio only, strictly no English speech` in each scene prompt.
- Never duplicate characters or introduce extra people. Never use `reference: ...` file tags in prompts.
- The Input / First Frame MUST specify both character assets plus the speaker's voice: `{speaker_tag} + {listener_tag} + {speaker_voice}`.

CRITICAL VOICE, TIMBRE, EYE CONTACT & EXPLICIT SILENCE RULE (AI NÓI THÌ VOICE NGƯỜI ĐÓ, CHỐNG NHẦM GIỌNG):
- In each scene, EXACTLY ONE character speaks and lip-syncs, paired with their own unique voice asset and audio timbre:
  * When `{char2_tag}` (`on the right`) speaks, use voice `{char2_voice}` and specify `with deep gruff older Vietnamese lip sync: "..."`.
  * When `{char1_tag}` (`on the left`) speaks, use voice `{char1_voice}` and specify `with high-pitched mischievous Vietnamese lip sync: "..."`.
- EYE CONTACT & CAMERA BREAK: While speaking or arguing, characters MUST look directly at each other (`looking directly at each other`, `turning head facing {char1_tag}` / `eyes locked onto {char2_tag}'s face`). Only glance directly into the camera (`glancing directly into the camera`) on a sly comedic beat or during the final scene's hysterical laugh.
- NO FACE COVERINGS & NO DARK SUNGLASSES: Keep faces and mouths clearly visible and unobstructed (NO dark sunglasses covering eyes/face, NO hands covering mouth).
- EXPLICIT SILENCE & LISTENER PERFORMANCE: The non-speaking character MUST have an explicit silence lock (`on the left, {char1_tag} remains completely silent with closed mouth` or `on the right, {char2_tag} remains completely silent with closed mouth`) with NO words/audio and NO lip-sync, while reacting vividly with micro-expressions:
  * `{char2_tag}` reactions: frowning sternly with furrowed brows, gesturing with a wrench, or freezing in utter disbelief with mouth agape before shaking head in defeat.
  * `{char1_tag}` reactions: deadpan innocent expression, wide innocent eyes, nonchalant shoulder shrug while wiping a greasy rag or spinning a bolt, and in the FINAL scene (`Scene {scene_count}`), `{char1_tag}` MUST explode into laughter: `bursts into cheerful, playful hearty laughter, head tilted back, holding belly, patting knee, shoulders bouncing with wide-smile giggles and funny gasps of gleeful satisfaction`.
- SIGNATURE FINAL PUNCHLINE: The phrase `"Đúng là khỉ!"` spoken by `{char2_tag}` may appear AT MOST ONCE, strictly in the final scene (`Scene {scene_count}`) as the closing exclamation after being outsmarted.

CRITICAL DIALOGUE PACING & DURATION RULE (ANTI-CUTOFF):
- SAFE WORDING (Google Flow content filter): never use violent or weapon-like words in dialogue or prompts (e.g. "chém", "giết", "đâm chết", "máu", "đánh nhau", "violently", "furiously"). Use harmless comic wording instead (e.g. "cắt", "nói phét", "cà khịa").
- Do NOT hardcode fixed seconds like "(4 giây)" or "(6 giây)" in the Scene titles or prompts, because hardcoded short durations cut off the character's speech before they finish talking! Set `"duration": "auto"` for every scene.
- Keep the spoken Vietnamese dialogue in EACH scene concise, punchy, and fast-paced (maximum 15–20 Vietnamese words total per scene) so the character ALWAYS finishes speaking 100% of the sentence with a 1-second facial reaction buffer before the clip ends.
- Always instruct the character in the English prompt to start speaking immediately at the beginning of the clip (`immediately at the start... speaking briskly with lip sync: "..." — finishes speaking completely before clip ends`).

CRITICAL CHARACTER TURN-TAKING & ROLE ALTERNATION (BẮT BUỘC ĐỔI VAI LUÂN PHIÊN, CẤM NÓI LIỀN 2 SCENE):
- The 2 characters MUST STRICTLY ALTERNATE speaking turns between scenes:
  * Start with {char2_tag}, then alternate to {char1_tag}, then back to {char2_tag}.
- ABSOLUTELY FORBIDDEN: NEVER allow the same character to speak in Scene 2 and then speak AGAIN in Scene 3!

Each idea MUST follow the exact structure below:

PHẦN 1 – THÔNG TIN VIDEO
Tên video: <title>
Bối cảnh: <setting>
Điểm gây cười & Cú twist: <twist_highlight>
Thời lượng tổng: Tự động theo độ dài thoại của 3 Scene (đảm bảo đọc trọn câu không bị ngắt cụt)
Câu thoại viral nhất: "<viral_quote>"

PHẦN 2 – TÓM TẮT KỊCH BẢN
Hook: <hook_summary>
Conflict: <conflict_summary>
Twist: <twist_summary>

PHẦN 3 – GOOGLE FLOW PROMPTS
Scene 1 — Input / First Frame: {char2_tag} + {char1_tag} + {char2_voice}: Medium two-shot, vertical 9:16 in <setting>: Vietnamese language audio only, strictly no English speech: Speaker: on the right, {char2_tag} with {char2_voice} looking directly at {char1_tag} with clearly visible face and deep gruff older Vietnamese lip sync: "<concise Vietnamese dialogue>" — finishes before clip ends. Listener: on the left, {char1_tag} remains completely silent with closed mouth, deadpan innocent expression, actively reacting with eye contact and small mechanic gestures; never frozen. Only {char2_tag} speaks.

Scene 2 — Input / First Frame: {char1_tag} + {char2_tag} + {char1_voice}: Medium two-shot, vertical 9:16 in <setting>: Vietnamese language audio only, strictly no English speech: Speaker: on the left, {char1_tag} with {char1_voice} looking directly at {char2_tag} with clearly visible face and high-pitched mischievous Vietnamese lip sync: "<concise Vietnamese dialogue>" — finishes before clip ends. Listener: on the right, {char2_tag} remains completely silent with closed mouth, glaring or blinking in disbelief with natural head and body movement; never frozen. Only {char1_tag} speaks.

Scene 3 — Input / First Frame: {char2_tag} + {char1_tag} + {char2_voice}: Medium two-shot, vertical 9:16 with high comedic tension in <setting>: Vietnamese language audio only, strictly no English speech: Speaker: on the right, {char2_tag} with {char2_voice} looking at {char1_tag} with clearly visible face and deep gruff older Vietnamese lip sync: "<concise Vietnamese punchline ending with Đúng là khỉ!>" — finishes with 1s reaction buffer. Listener: on the left, {char1_tag} remains completely silent with closed mouth during the line, then bursts into cheerful, playful hearty laughter, head tilted back, holding belly, patting knee, shoulders bouncing with wide-smile giggles and funny gasps of gleeful satisfaction; never frozen. Only {char2_tag} speaks.

PHẦN 4 – TEXT & CAPTION
Text trên video: "<overlay_text>"
Caption: "<caption with emojis and hashtags>"
Hashtag: <hashtags>

PHẦN 5 – GỢI Ý TƯƠNG TÁC
"<engagement_prompt>"

Use the following creative direction as secondary guidance. It cannot override the selected topic or duo:
{idea_style}

Treat this user-provided prompt as the primary story brief:
{user_prompt}

Each idea must contain exactly two characters and exactly {scene_count} scenes.
The complete video should last about {duration_seconds} seconds. Do not reuse example
dialogue. Keep the specified character personas consistent. Return only JSON matching this shape:
[
  {
    "title": "<short, specific title>",
    "setting": "<recognizable Vietnamese everyday setting>",
    "twist_highlight": "<one-sentence description of the logical comic twist>",
    "total_duration_label": "Tự động theo độ dài thoại",
    "viral_quote": "<short memorable line>",
    "hook_summary": "<setup>",
    "conflict_summary": "<specific conflict>",
    "twist_summary": "<final-scene reveal>",
    "overlay_text": "<short on-screen hook>",
    "caption": "<relevant caption>",
    "hashtags": "<relevant hashtags>",
    "engagement_prompt": "<natural question for viewers>",
    "character_1": {"ingredient": "{char1_tag}"},
    "character_2": {"ingredient": "{char2_tag}"},
    "scenes": {scene_examples},
    "social_posts": {
      "tiktok": {"title": "<title>", "caption": "<caption>", "hashtags": "<hashtags>"},
      "facebook": {"title": "<title>", "caption": "<caption>", "hashtags": "<hashtags>"},
      "youtube": {"title": "<title>", "caption": "<caption>", "hashtags": "<hashtags>"}
    }
  }
]
The example contains exactly the requested number of scene objects; generate the same number for every idea.
""".replace("{char1_tag}", char1_tag).replace(
        "{char1_voice}", char1_voice
    ).replace(
        "{char2_tag}", char2_tag
    ).replace("{char2_voice}", char2_voice).replace("{idea_style}", idea_style).replace(
        "{skill_instructions}", skill_instructions
    ).replace(
        "{user_prompt}", user_prompt
    ).replace("{ideas_count}", str(ideas_count)).replace(
        "{scene_count}", str(scene_count)
    ).replace(
        "{duration_seconds}", str(duration_seconds)
    ).replace(
        "{scene_examples}", scene_examples_json
    )

    raw_json_text = _call_gemini_json(prompt_text, temperature=0.9)
    ideas = _validate_ideas(json.loads(raw_json_text), scene_count)
    for idea in ideas:
        for character_key, default_tag in (
            ("character_1", char1_tag),
            ("character_2", char2_tag),
        ):
            character = idea.get(character_key)
            tag = (
                character.get("ingredient")
                if isinstance(character, dict) and character.get("ingredient")
                else default_tag
            )
            idea[character_key] = {"ingredient": tag}
        for scene in idea.get("scenes", []):
            if isinstance(scene, dict) and isinstance(scene.get("prompt"), str):
                scene["prompt"] = _clean_scene_prompt(scene["prompt"])
        idea["duration_seconds"] = duration_seconds
        if custom_prompt:
            idea["user_prompt"] = custom_prompt
        idea["social_posts"] = _normalize_social_posts(
            idea.get("social_posts"),
            fallback_title=idea.get("title", ""),
            scenes=idea.get("scenes"),
            fallback_caption=str(idea.get("caption") or ""),
            fallback_hashtags=str(idea.get("hashtags") or ""),
        )
        if any(
            idea.get(k)
            for k in (
                "setting",
                "twist_highlight",
                "viral_quote",
                "hook_summary",
                "conflict_summary",
                "twist_summary",
                "overlay_text",
                "engagement_prompt",
            )
        ):
            idea["full_script_text"] = format_flow_agent_prompt(idea)
            idea["five_part_script_text"] = format_five_part_script(idea)
    return ideas


def generate_social_posts_for_video(
    video_path: str | Path | None = None,
    video_title: str = "",
    hint_text: str = "",
) -> dict[str, dict[str, str]]:
    clean_title = (video_title or "").strip()
    if not clean_title and video_path:
        clean_title = Path(video_path).stem.replace("_", " ").replace("-", " ").strip()
    clean_hint = (hint_text or "").strip()

    prompt_instruction = f"""
Bạn là chuyên gia sáng tạo nội dung video ngắn đa nền tảng tại Việt Nam (TikTok, Facebook Reels, YouTube Shorts).
Hãy viết bộ Tiêu đề (title), Caption/Mô tả (caption) và Hashtag (hashtags) tiếng Việt RIÊNG BIỆT, tối ưu thuật toán cho từng nền tảng dựa trên video này:
- Tên/Chủ đề video: "{clean_title or 'Video ngắn hài hước'}"
- Gợi ý/Mô tả thêm: "{clean_hint or 'Xem nội dung video đính kèm hoặc tên file để viết nội dung hài hước, bắt trend, kéo tương tác bình luận'}"

Yêu cầu cho từng nền tảng:
1. `tiktok`:
   - `title`: Hook cực ngắn, gây tò mò ngay 3 giây đầu (tối đa 120 ký tự).
   - `caption`: Văn phong trẻ trung, bắt trend TikTok, đặt câu hỏi hoặc tình huống kích thích người xem bình luận tranh luận.
   - `hashtags`: Chuỗi 5-7 hashtag xu hướng TikTok Việt Nam (bắt đầu bằng dấu #, cách nhau bởi dấu cách, ví dụ: "#xuhuong #tiktokvn #haihuoc ...").
2. `facebook`:
   - `title`: Tiêu đề gần gũi, đời thường, thu hút người dùng Facebook Reels.
   - `caption`: Văn phong kể chuyện đời thường hài hước, dễ đồng cảm để tăng chia sẻ và bình luận trên Fanpage.
   - `hashtags`: Chuỗi 4-6 hashtag phù hợp Facebook Reels (ví dụ: "#reelsvn #facebookreels #haihuoc ...").
3. `youtube`:
   - `title`: Tiêu đề chuẩn SEO trên YouTube Shorts (tối đa 100 ký tự), có hashtag phù hợp nội dung như `#Shorts #chuyendoithuong`.
   - `caption`: Mô tả tóm tắt nội dung hấp dẫn, kèm lời kêu gọi Like & Đăng ký kênh (Subscribe).
   - `hashtags`: Chuỗi 5-7 hashtag/từ khóa chuẩn SEO YouTube Shorts (bắt buộc có `#Shorts` và hashtag phù hợp chủ đề).

Chỉ trả về đúng JSON theo cấu trúc sau (không kèm markdown):
{{
  "general_title": "Tiêu đề ngắn gọn đại diện cho video",
  "tiktok": {{
    "title": "...",
    "caption": "...",
    "hashtags": "#xuhuong #tiktokvn ..."
  }},
  "facebook": {{
    "title": "...",
    "caption": "...",
    "hashtags": "#reelsvn #facebookreels ..."
  }},
  "youtube": {{
    "title": "... #Shorts #chuyendoithuong ...",
    "caption": "...",
    "hashtags": "#Shorts #chuyendoithuong #haihuoc ..."
  }}
}}
""".strip()

    contents: Any = prompt_instruction
    path_obj = Path(video_path) if video_path else None
    if (
        path_obj is not None
        and path_obj.is_file()
        and 0 < path_obj.stat().st_size <= 18 * 1024 * 1024
        and hasattr(genai, "types")
        and hasattr(genai.types, "Part")
        and hasattr(genai.types.Part, "from_bytes")
    ):
        try:
            video_bytes = path_obj.read_bytes()
            video_part = genai.types.Part.from_bytes(
                data=video_bytes,
                mime_type="video/mp4",
            )
            contents = [video_part, prompt_instruction]
        except Exception:
            contents = prompt_instruction

    try:
        raw_text = _call_gemini_json(contents, temperature=0.85)
    except Exception:
        if isinstance(contents, list):
            raw_text = _call_gemini_json(prompt_instruction, temperature=0.85)
        else:
            raise

    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError("Gemini did not return a JSON object for social posts.")

    resolved_title = str(parsed.get("general_title") or clean_title or "Video ngắn").strip()
    normalized = _normalize_social_posts(parsed, fallback_title=resolved_title)
    normalized["general_title"] = {"title": resolved_title, "caption": "", "hashtags": ""}
    return normalized


def suggest_trending_topics(count: int = 5) -> list[str]:
    """Suggest distinct, specific comedy premises for the monkey-and-uncle duo."""
    skill_instructions = _load_skill_instructions()
    recent_titles = _load_recent_draft_titles(limit=12)
    avoid_block = (
        f"\n- Tránh lặp lại các tiêu đề/chủ đề đã tạo gần đây: {', '.join(recent_titles)}."
        if recent_titles
        else ""
    )
    prompt = f"""
Bạn là biên kịch hài video ngắn Việt Nam theo chuẩn SKILL video-hai-two-shot-creator (Hài dân gian & Google Flow chuẩn form).
Hãy đề xuất đúng {count} ý tưởng làm nút chọn
cho bộ đôi cố định: @khi là khỉ ma ranh, chủ tiệm sửa xe (áo ba lỗ trắng dính dầu mỡ, quần thợ máy tối màu, cầm cờ-lê, xưng "con/tui" gọi "chú", lý sự cùn lươn lẹo); @nguoi là ông chú trung niên
thích thể hiện, sĩ diện, khôn lỏi nhưng thường gặp sự cố đời thường (đội mũ bảo hiểm nửa đầu màu tối, áo thun thợ màu xám đen, nóng tính nhưng sợ vợ, xưng "tao" gọi "mày/khỉ").

YÊU CẦU CHẤT LƯỢNG & 5 MÔ-TÍP HÀI DÂN GIAN ĐẮT GIÁ:
- Mỗi ý tưởng phải là một tình huống cụ thể, dễ hình dung, có chất gây cười cho bộ đôi này;
  dùng đa dạng hỏi xoáy đáp xoay, cà khịa và MỌI chủ đề/bối cảnh đời sống (chợ, quán cà phê, đám cưới, bệnh viện, công sở, trường học, du lịch, nhà hàng, xóm trọ, livestream, đồng ruộng, sân bóng...), KHÔNG nhất thiết ở tiệm sửa xe và mỗi ý tưởng một bối cảnh khác nhau, xoay quanh các nhóm miếng hài dân gian (ưu tiên trộn cả nhóm 6-8, ít nhất 2/5 ý tưởng là hỏi ngu hoặc hót trend):
  1. Bắt chữ nghĩa đen / chơi chữ lươn lẹo (chú dặn một đằng, khỉ hiểu theo nghĩa đen làm một nẻo khiến chú đứng hình).
  2. Chữa lợn lành thành lợn què (sửa chữa/giúp việc bằng mẹo bá đạo khiến kết quả dở khóc dở cười).
  3. Ông chú sĩ diện bị bóc mẽ (khoe khoang bản lĩnh, tiền bạc, không sợ vợ rồi bị phát hiện sự thật phũ phàng ngay tại chỗ).
  4. Dịch vụ / khởi nghiệp bá đạo ở bất kỳ nơi nào (khuyến mãi, bảng giá, quy định nghe hợp lý nhưng tính tiền ra cực kỳ lươn lẹo).
  5. Đú trend / công nghệ nửa mùa (camera lùi, AI, định vị, tóp tóp, hóng drama mạng bị dính bẫy đời thường).
  6. Hỏi ngu / hỏi dại nghe rất có lý (khỉ hỏi câu ngô nghê, ông chú giải thích nghiêm túc rồi tự vấp logic).
  7. Hót trend & meme hôm nay (câu nói viral, thử thách, filter, nhạc trend; chung chung, hư cấu, không nhắc người thật).
  8. Các tình huống khác: hỏi ngu / hỏi dại nghe có lý, bắt trend & meme đang hot (hư cấu, không nhắc người thật), hỏi AI/Google rồi làm theo sái cổ, phỏng vấn xin việc, tỏ tình/xem mắt, review quán ăn, chuyện giá cả/lương/vé Tết, thầy bói tay ngang, bóng đá/thi đấu phong trào, mẹo vặt đời sống, tranh luận vô tri.
- Mỗi ý tưởng dùng bối cảnh/mâu thuẫn khác nhau, khả thi, gần gũi với đời sống Việt Nam, dẫn tới câu chốt ông chú mắng "Đúng là khỉ!" và khỉ cười hả hê đắc thắng.
- Có thể có mục drama VF3/công sở đang được bàn tán nhưng phải hư cấu, ẩn danh;
  không dùng tên hoặc hình ảnh người thật, không kể tin đồn như sự thật.
- Không khẳng định đang là trend thật nếu không có dữ liệu. Ưu tiên chất liệu đời thường,
  lời lẽ tự nhiên, tiêu đề ngắn gọn (khoảng 8–18 từ), có tình huống cụ thể.
- Các ý tưởng phải khác nhau về cả bối cảnh lẫn tình huống, không chỉ đổi câu chữ.{avoid_block}

THAM CHIẾU SKILL:
{skill_instructions}

Chỉ trả về JSON là một mảng chuỗi gồm chính xác {count} phần tử, không thêm giải thích:
["Tình huống cụ thể có mâu thuẫn và cú bẻ lái", "..."]
"""
    try:
        raw_text = _call_gemini_json(prompt, temperature=0.95)
        parsed = json.loads(raw_text)
        if isinstance(parsed, list):
            res = [
                topic.strip()
                for topic in parsed
                if isinstance(topic, str) and topic.strip()
            ]
        elif isinstance(parsed, dict) and isinstance(parsed.get("topics"), list):
            res = [
                topic.strip()
                for topic in parsed["topics"]
                if isinstance(topic, str) and topic.strip()
            ]
        else:
            res = []

        normalized_topics = [re.sub(r"\s+", " ", topic).casefold() for topic in res]
        if len(res) != count or len(set(normalized_topics)) != count:
            raise ValueError(
                f"Gemini must return exactly {count} distinct topic suggestions."
            )
        return res
    except Exception as exc:
        LOGGER.exception("Could not suggest topics via Gemini.")
        raise RuntimeError(
            "Gemini could not provide topic suggestions. Check the API key and model."
        ) from exc


def rewrite_idea_twist_or_refine(
    idea: dict[str, Any], twist_only: bool = True
) -> dict[str, Any]:
    """Rewrite the punchline/twist of an idea, or polish all 3 scenes."""
    char1_tag = _opt_setting("CHARACTER_1_TAG", "@khi")
    char1_voice = _opt_setting("CHARACTER_1_VOICE", "@Voice_Khi")
    char2_tag = _opt_setting("CHARACTER_2_TAG", "@nguoi")
    char2_voice = _opt_setting("CHARACTER_2_VOICE", "@Voice_Nguoi")
    skill_instructions = _load_skill_instructions()

    title = idea.get("title", "Kịch bản hài hước")
    setting = idea.get("setting", "Việt Nam đời thường")
    scenes = idea.get("scenes", [])
    scenes_text = json.dumps(scenes, ensure_ascii=False, indent=2)

    task_desc = (
        "ĐỔI CÚ BẺ LÁI (PLOT TWIST): Giữ nguyên bối cảnh và câu dẫn ở Scene 1, nhưng hãy viết lại một cú twist hoàn toàn MỚI, bất ngờ và hài hước hơn nhiều ở Scene 2 và Scene 3!"
        if twist_only
        else f"TỐI ƯU TOÀN DIỆN KỊCH BẢN: Viết lại cả 3 Scene sao cho thoại dưới 20 chữ, cú twist ở Scene 3 bất ngờ và hợp lý; giữ đúng chủ đề đời thường cùng hai nhân vật {char1_tag} và {char2_tag}."
    )

    prompt = f"""
Bạn là đạo diễn và biên kịch video ngắn hài hước hàng đầu Việt Nam theo chuẩn SKILL video-hai-two-shot-creator.
{task_desc}
Hai tag nhân vật là asset hình ảnh/giọng nói có sẵn; không biến danh tính, loài hoặc tuổi của nhân vật thành chủ đề kịch bản. Giữ nguyên tình huống đời thường của bản gốc.

THAM CHIẾU SKILL:
{skill_instructions}

Kịch bản hiện tại:
- Tiêu đề: "{title}"
- Bối cảnh: "{setting}"
- Các cảnh hiện tại:
{scenes_text}

QUY TẮC BẮT BUỘC:
1. KHUNG HÌNH & VỊ TRÍ CỐ ĐỊNH: Mọi Scene đều là Medium two-shot, vertical 9:16 có cả {char1_tag} (on the left, thợ sửa xe áo ba lỗ trắng dính dầu mỡ) và {char2_tag} (on the right, đội mũ bảo hiểm nửa đầu màu tối, áo thợ tối màu) cạnh nhau; không nhân đôi nhân vật, không thêm người, luôn có `Vietnamese language audio only, strictly no English speech`.
2. FIRST FRAME: Bắt buộc liệt kê cả 2 nhân vật và voice:
   - Scene 1: "{char2_tag} + {char1_tag} + {char2_voice}"
   - Scene 2: "{char1_tag} + {char2_tag} + {char1_voice}"
   - Scene 3: "{char2_tag} + {char1_tag} + {char2_voice}"
3. ĐỔI VAI LUÂN PHIÊN & ÂM SẮC:
   - Scene 1: {char2_tag} nói (`with deep gruff older Vietnamese lip sync`, xưng "tao" gọi "mày/khỉ");
   - Scene 2: {char1_tag} nói (`with high-pitched mischievous Vietnamese lip sync`, xưng "con/tui" gọi "chú");
   - Scene 3: {char2_tag} nói (`with deep gruff older Vietnamese lip sync`, chốt câu "Đúng là khỉ!" duy nhất 1 lần ở cảnh cuối).
4. ÁNH NHÌN & KHÓA CÂM NGƯỜI NGHE: Khi đối thoại hai nhân vật nhìn trực diện vào nhau (`looking directly at each other`). Nhân vật không thoại phải khóa câm rõ ràng (`remains completely silent with closed mouth`) nhưng phản ứng vi biểu cảm sống động; riêng ở cuối Scene 3, {char1_tag} bùng nổ cười đắc thắng (`bursts into cheerful, playful hearty laughter, head tilted back, holding belly, patting knee, shoulders bouncing with wide-smile giggles and funny gasps of gleeful satisfaction...`).
5. THOẠI NGẮN GỌN: Dưới 15–20 từ tiếng Việt, nói nhanh, vào thoại ngay đầu cảnh.

Trả về duy nhất JSON theo cấu trúc:
{{
  "title": "{title}",
  "setting": "{setting}",
  "twist_highlight": "<tóm tắt cú twist mới ngắn gọn 1 câu>",
  "scenes": [
    {{
      "duration": "auto",
      "first_frame": "{char2_tag} + {char1_tag} + {char2_voice}",
      "dialogue": "<thoại Scene 1>",
      "voice": "{char2_voice}",
      "prompt": "Medium two-shot, vertical 9:16 in the story's everyday setting: Vietnamese language audio only, strictly no English speech: Speaker: on the right, {char2_tag} with {char2_voice}, looking directly at {char1_tag} with clearly visible face, speaking with deep gruff older Vietnamese lip sync: \\\"<thoại Scene 1>\\\" — finishes before clip ends. Listener: on the left, {char1_tag} remains completely silent with closed mouth, deadpan innocent expression, visibly reacting with eye contact and small mechanic gestures; never frozen. Only {char2_tag} speaks."
    }},
    {{
      "duration": "auto",
      "first_frame": "{char1_tag} + {char2_tag} + {char1_voice}",
      "dialogue": "<thoại Scene 2>",
      "voice": "{char1_voice}",
      "prompt": "Medium two-shot, vertical 9:16 in the story's everyday setting: Vietnamese language audio only, strictly no English speech: Speaker: on the left, {char1_tag} with {char1_voice}, looking directly at {char2_tag} with clearly visible face, speaking with high-pitched mischievous Vietnamese lip sync: \\\"<thoại Scene 2>\\\" — finishes before clip ends. Listener: on the right, {char2_tag} remains completely silent with closed mouth, freezing in disbelief with natural head and body movement; never frozen. Only {char1_tag} speaks."
    }},
    {{
      "duration": "auto",
      "first_frame": "{char2_tag} + {char1_tag} + {char2_voice}",
      "dialogue": "<thoại Scene 3>",
      "voice": "{char2_voice}",
      "prompt": "Medium two-shot, vertical 9:16 with high comedic tension in the story's everyday setting: Vietnamese language audio only, strictly no English speech: Speaker: on the right, {char2_tag} with {char2_voice}, looking at {char1_tag} with clearly visible face, speaking with deep gruff older Vietnamese lip sync: \\\"<thoại Scene 3>\\\" — finishes with 1s reaction buffer. Listener: on the left, {char1_tag} remains completely silent with closed mouth during the line, then bursts into cheerful, playful hearty laughter, head tilted back, holding belly, patting knee, shoulders bouncing with wide-smile giggles and funny gasps of gleeful satisfaction; never frozen. Only {char2_tag} speaks."
    }}
  ]
}}
"""
    try:
        raw_text = _call_gemini_json(prompt, temperature=0.95)
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict) and isinstance(parsed.get("scenes"), list) and len(parsed["scenes"]) >= 2:
            new_idea = dict(idea)
            new_idea["title"] = str(parsed.get("title") or title).strip()
            new_idea["setting"] = str(parsed.get("setting") or setting).strip()
            if parsed.get("twist_highlight"):
                new_idea["twist_highlight"] = str(parsed["twist_highlight"]).strip()
            new_idea["scenes"] = parsed["scenes"]
            new_idea["full_script_text"] = format_flow_agent_prompt(new_idea)
            return new_idea
    except Exception as exc:
        LOGGER.warning("Could not rewrite idea twist via Gemini: %s", exc)
    return idea
