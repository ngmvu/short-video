"""Parse a hand-written multi-scene script (pasted from Telegram) into an idea dict.

Accepted input looks like::

    Tạo 3 video theo 3 Scene sau (...):
    🔥 KỊCH BẢN: Thầy Bói Xem Quẻ Bách Phát Bách Trúng
    Scene 1 — Input / First Frame: @nguoi + @Voice_Nguoi: Vertical 9:16 ... "câu thoại" ...
    Scene 2 — Input / First Frame: @khi + @Voice_Khi: ...

Scenes may be on separate lines or run together on one line.
"""

from __future__ import annotations

import re
from typing import Any

MAX_SCENES = 20
MAX_SCRIPT_CHARS = 12_000

_SCENE_HEADER = re.compile(r"\bScene\s*(\d{1,2})\s*[—–\-:]+\s*", re.IGNORECASE)
_DIRECT_SCENE_HEADER = re.compile(
    r"^[ \t\u200b-\u200f\u2060\ufeff]*(?:\[[^\]\r\n]+\]"
    r"[ \t\u200b-\u200f\u2060\ufeff]*[^:\r\n]+:[ \t]*)?"
    r"\*{0,2}Scene(?:[ \t]*(\d{1,2}))?"
    r"(?:[ \t]*\(([^)\r\n]*)\))?\*{0,2}"
    r"[ \t]*(?:[—–\-:][ \t]*(.*))?$",
    re.IGNORECASE | re.MULTILINE,
)
_TITLE = re.compile(r"K[ỊI]CH\s*B[ẢA]N\s*[:：]\s*(.+)", re.IGNORECASE)
_FIRST_FRAME_PREFIX = re.compile(r"^\s*Input\s*/\s*First\s*Frame\s*[:：]\s*", re.IGNORECASE)
_FIRST_FRAME = re.compile(r"^\s*((?:@[\w.]+\s*\+\s*)*@[\w.]+)\s*[:：]\s*")
_LEADING_TAGS = re.compile(
    r"^\s*((?:@[\w.]+(?:\s*\+\s*|\s+))*@[\w.]+)(?=\s|[:：]|$)"
)
_QUOTE = re.compile(r"[\"“”]([^\"“”]{2,})[\"“”]")
_TAG = re.compile(r"@[\w.]+")


class ScriptParseError(ValueError):
    """Raised when the pasted text is not a usable multi-scene script."""


def looks_like_script(text: str) -> bool:
    """True when the text contains at least one 'Scene N —' header."""
    return bool(_SCENE_HEADER.search(text or ""))


def looks_like_direct_prompt(text: str) -> bool:
    """True when the text contains a numbered or unnumbered direct scene header."""
    return bool(_DIRECT_SCENE_HEADER.search(text or ""))


def _is_voice(tag: str) -> bool:
    return tag.lower().startswith("@voice")


def _extract_title(text: str, header_start: int) -> str:
    match = _TITLE.search(text)
    if match:
        title = match.group(1).splitlines()[0]
        title = _SCENE_HEADER.split(title)[0]
        title = title.strip(" \t\"'“”:-—–")
        if title:
            return title[:120]
    for line in text[:header_start].splitlines():
        cleaned = line.strip(" \t🔥🎬*#:-—–")
        if cleaned and not cleaned.lower().startswith("tạo "):
            return cleaned[:120]
    return "Kịch bản từ Telegram"


def _parse_scene_body(
    body: str,
    tag_to_voice: dict[str, str],
    voice_to_tag: dict[str, str],
) -> dict[str, str]:
    body = _FIRST_FRAME_PREFIX.sub("", body.strip(), count=1)
    frame_tags: list[str] = []
    frame_voice = ""
    frame_match = _FIRST_FRAME.match(body)
    raw_first_frame = ""
    if frame_match:
        matched_str = frame_match.group(1)
        raw_first_frame = matched_str
        all_tags = _TAG.findall(matched_str)
        for t in all_tags:
            if _is_voice(t):
                if not frame_voice:
                    frame_voice = t
            else:
                frame_tags.append(t)
        body = body[frame_match.end():]
    prompt = body.strip()

    speaker_tag = ""
    voice = frame_voice
    if "Speaker:" in prompt:
        after = prompt.split("Speaker:", 1)[1]
        tags = _TAG.findall(after[:120])
        speaker_tags = [t for t in tags if not _is_voice(t)]
        speaker_voices = [t for t in tags if _is_voice(t)]
        if speaker_tags:
            speaker_tag = speaker_tags[0]
        if speaker_voices and not voice:
            voice = speaker_voices[0]

    if not speaker_tag and frame_tags:
        speaker_tag = frame_tags[0]

    if not voice:
        voices = [t for t in _TAG.findall(prompt) if _is_voice(t)]
        if voices:
            voice = voices[0]
    if not speaker_tag and voice:
        speaker_tag = voice_to_tag.get(voice.lower(), "")
    if not speaker_tag:
        tags = [t for t in _TAG.findall(prompt) if not _is_voice(t)]
        if tags:
            speaker_tag = tags[0]
    if speaker_tag and not voice:
        voice = tag_to_voice.get(speaker_tag.lower(), "")

    first_frame = raw_first_frame if raw_first_frame else " + ".join(part for part in (speaker_tag, voice) if part)
    quote = _QUOTE.search(prompt)
    return {
        "first_frame": first_frame,
        "voice": voice,
        "prompt": prompt,
        "dialogue": quote.group(1).strip() if quote else "",
    }


def parse_script(
    text: str,
    characters: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Convert a pasted script into the idea structure used by the video pipeline.

    ``characters`` is a list of ``(tag, voice)`` pairs, e.g. ``[("@khi", "@Voice_Khi")]``,
    used to fill in a missing voice or speaker.
    """
    text = (text or "").strip()
    if not text:
        raise ScriptParseError("Kịch bản đang trống.")
    if len(text) > MAX_SCRIPT_CHARS:
        raise ScriptParseError(
            f"Kịch bản dài quá {MAX_SCRIPT_CHARS:,} ký tự, hãy rút gọn lại."
        )

    headers = list(_SCENE_HEADER.finditer(text))
    if not headers:
        raise ScriptParseError(
            "Không tìm thấy dòng 'Scene 1 — ...'. Hãy dán kịch bản có các Scene đánh số."
        )
    if len(headers) > MAX_SCENES:
        raise ScriptParseError(f"Tối đa {MAX_SCENES} Scene cho một video.")

    pairs = characters or []
    tag_to_voice = {tag.lower(): voice for tag, voice in pairs if tag and voice}
    voice_to_tag = {voice.lower(): tag for tag, voice in pairs if tag and voice}

    scenes: list[dict[str, str]] = []
    for position, header in enumerate(headers):
        end = headers[position + 1].start() if position + 1 < len(headers) else len(text)
        parsed = _parse_scene_body(text[header.end():end], tag_to_voice, voice_to_tag)
        if len(parsed["prompt"]) < 10:
            raise ScriptParseError(f"Scene {header.group(1)} chưa có mô tả cảnh.")
        if position == 0:
            scene_type = "story" if len(headers) == 1 else "intro"
        elif position == len(headers) - 1:
            scene_type = "punchline"
        else:
            scene_type = "dialogue"
        scenes.append(
            {
                "scene_title": f"Scene {header.group(1)}",
                "duration": "auto",
                **parsed,
                "type": scene_type,
            }
        )

    idea: dict[str, Any] = {
        "title": _extract_title(text, headers[0].start()),
        "source": "telegram_script",
        "scenes": scenes,
    }
    if len(pairs) >= 2:
        idea["character_1"] = {"ingredient": pairs[0][0]}
        idea["character_2"] = {"ingredient": pairs[1][0]}
    return idea


def parse_direct_prompt(text: str) -> dict[str, Any]:
    """Parse multi-scene Google Flow prompts pasted with Markdown headings."""
    text = (text or "").strip()
    if not text:
        raise ScriptParseError("Prompt đang trống.")
    if len(text) > MAX_SCRIPT_CHARS:
        raise ScriptParseError(
            f"Prompt dài quá {MAX_SCRIPT_CHARS:,} ký tự, hãy rút gọn lại."
        )

    headers = list(_DIRECT_SCENE_HEADER.finditer(text))
    if not headers:
        raise ScriptParseError(
            "Không tìm thấy tiêu đề cảnh. Hãy dùng `Scene 1 — Input / First Frame: ...` "
            "hoặc `Scene — Input / First Frame: ...` (mỗi cảnh một dòng)."
        )
    if len(headers) > MAX_SCENES:
        raise ScriptParseError(f"Tối đa {MAX_SCENES} Scene cho một video.")

    scenes: list[dict[str, str]] = []
    for position, header in enumerate(headers):
        end = headers[position + 1].start() if position + 1 < len(headers) else len(text)
        inline_body = header.group(3) or ""
        has_first_frame_prefix = bool(_FIRST_FRAME_PREFIX.match(inline_body))
        if not has_first_frame_prefix:
            inline_body = ""
        body = "\n".join(part for part in (inline_body, text[header.end():end]) if part)
        prompt_lines = []
        for line in body.splitlines():
            cleaned = re.sub(r"^[ \t]*>[ \t]?", "", line).strip()
            if cleaned.startswith("```") and cleaned.endswith("```"):
                continue
            if cleaned.startswith("`") and cleaned.endswith("`") and len(cleaned) >= 2:
                cleaned = cleaned[1:-1].strip()
            if cleaned:
                prompt_lines.append(cleaned)
        prompt = "\n".join(prompt_lines).strip()
        first_frame = ""
        if has_first_frame_prefix:
            prompt = _FIRST_FRAME_PREFIX.sub("", prompt, count=1)
            if prompt.startswith("`") and prompt.endswith("`") and len(prompt) >= 2:
                prompt = prompt[1:-1].strip()
            frame_match = _FIRST_FRAME.match(prompt)
            if frame_match:
                first_frame = frame_match.group(1).strip()
                prompt = prompt[frame_match.end():].lstrip()
            else:
                leading_tags_match = _LEADING_TAGS.match(prompt)
                if leading_tags_match:
                    first_frame = " + ".join(
                        dict.fromkeys(_TAG.findall(leading_tags_match.group(1)))
                    )
        if len(prompt) < 10:
            scene_number = header.group(1) or str(position + 1)
            raise ScriptParseError(f"Scene {scene_number} chưa có prompt.")

        duration_match = re.search(
            r"(\d+(?:[.,]\d+)?)\s*(?:giây|seconds?|secs?|s)\b",
            header.group(2) or "",
            re.IGNORECASE,
        )
        duration = (
            f"{duration_match.group(1).replace(',', '.')}s"
            if duration_match
            else "auto"
        )
        tags = [tag for tag in _TAG.findall(prompt) if not _is_voice(tag)]
        voices = [tag for tag in _TAG.findall(prompt) if _is_voice(tag)]
        if not first_frame:
            first_frame = " + ".join(dict.fromkeys(tags[:2]))
        if voices:
            first_frame = " + ".join(part for part in (first_frame, voices[0]) if part)
        quote = _QUOTE.search(prompt)
        scenes.append(
            {
                "scene_title": f"Scene {header.group(1) or position + 1}",
                "duration": duration,
                "first_frame": first_frame,
                "voice": voices[0] if voices else "",
                "prompt": prompt,
                "dialogue": quote.group(1).strip() if quote else "",
                "type": (
                    "story"
                    if len(headers) == 1
                    else "intro"
                    if position == 0
                    else "punchline"
                    if position == len(headers) - 1
                    else "dialogue"
                ),
            }
        )

    title = _extract_title(text, headers[0].start())
    if title in {"Kịch bản từ Telegram", "Prompt trực tiếp từ Telegram"}:
        if headers and headers[0].group(2):
            p = headers[0].group(2)
            cleaned = re.sub(
                r"^\s*\d+(?:[.,]\d+)?\s*(?:giây|seconds?|secs?|s)\s*[—–\-:]*\s*",
                "",
                p,
                flags=re.IGNORECASE,
            ).strip()
            if cleaned:
                title = cleaned
    if title == "Kịch bản từ Telegram":
        title = "Prompt trực tiếp từ Telegram"
    return {
        "title": title,
        "source": "telegram_direct",
        "scenes": scenes,
    }
