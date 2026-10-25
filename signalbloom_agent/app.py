"""Helpers for running as a packaged Windows program (PyInstaller exe).

When frozen, every file the agent makes (config.yaml, data\\) lives in the
folder next to the exe, not the current directory or PyInstaller's temp folder.
When running from source, nothing here changes how the agent behaves.
"""

import json
import re
import sys
from pathlib import Path

DEFAULT_CLOUD_URL = "https://signalbloom.ai.studio"
CONFIG_NAME = "config.yaml"
TEMPLATE_NAME = "config.example.yaml"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """The exe's folder when frozen, else the current directory."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def resource_path(name: str) -> Path:
    """A file bundled into the exe (or the repo root when running from source)."""
    base = getattr(sys, "_MEIPASS", None)
    if is_frozen() and base:
        return Path(base) / name
    return Path(__file__).resolve().parent.parent / name


def create_config(path: str | Path, setup_code: str,
                  cloud_url: str = DEFAULT_CLOUD_URL) -> None:
    """Write a new config.yaml from the bundled template with the URL and code filled in."""
    text = resource_path(TEMPLATE_NAME).read_text(encoding="utf-8")
    text = text.replace(
        "# SignalBloom agent config. Copy this file to config.yaml and fill it in.",
        "# SignalBloom agent config, created on first run. Delete it to register again.",
    )
    for key, value in (("cloud_url", cloud_url), ("setup_code", setup_code)):
        line = f"{key}: {json.dumps(value)}"
        text = re.sub(rf"^{key}:.*$", lambda _m: line, text, count=1, flags=re.MULTILINE)
    Path(path).write_text(text, encoding="utf-8")


def ask_setup_code() -> str:
    """Ask in the console for the one-time setup code. Never returns an empty string."""
    while True:
        code = input("Setup code: ").strip().upper()
        if code:
            return code


def pause_before_exit() -> None:
    """Keep a double-clicked exe's window open so the message can be read."""
    if not is_frozen():
        return
    try:
        input("Press Enter to close...")
    except (EOFError, KeyboardInterrupt):
        pass
