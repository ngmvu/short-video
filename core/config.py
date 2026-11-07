from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = ROOT_DIR / "storage"
DOWNLOADS_DIR = STORAGE_DIR / "downloads"

load_dotenv(ROOT_DIR / ".env")


def get_setting(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or not value.strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return value.strip()


def setting_path(name: str, default: str) -> Path:
    path = Path(get_setting(name, default)).expanduser()
    return path if path.is_absolute() else ROOT_DIR / path
