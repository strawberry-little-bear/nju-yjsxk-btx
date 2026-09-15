"""跨平台 Chrome/Selenium 启动支持。"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service


def _driver_candidates() -> list[Path]:
    """Return likely local ChromeDriver paths for Windows and macOS/Linux."""
    candidates: list[Path] = []
    configured = os.environ.get("CHROMEDRIVER")
    if configured:
        candidates.append(Path(configured).expanduser())

    on_windows = os.name == "nt"
    executable_names = {"chromedriver.exe"} if on_windows else {"chromedriver"}
    cache_root = Path.home() / ".wdm" / "drivers" / "chromedriver"
    if cache_root.is_dir():
        try:
            candidates.extend(
                path
                for path in cache_root.rglob("*")
                if path.is_file() and path.name in executable_names
            )
        except OSError:
            pass

    which_driver = shutil.which("chromedriver")
    if which_driver:
        candidates.append(Path(which_driver))

    seen: set[str] = set()
    existing: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key not in seen and resolved.is_file():
            seen.add(key)
            existing.append(resolved)
    return existing


def _chrome_candidates() -> list[Path]:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        return [
            Path(program_files) / "Google/Chrome/Application/chrome.exe",
            Path(program_files_x86) / "Google/Chrome/Application/chrome.exe",
            Path(local_app_data) / "Google/Chrome/Application/chrome.exe" if local_app_data else Path(),
        ]
    if sys.platform == "darwin":
        return [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    return [
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    ]


def build_driver():
    """Create an isolated Chrome profile using local Driver or Selenium Manager."""
    options = Options()
    profile_dir = Path.cwd() / f".chrome-profile-{datetime.now():%Y%m%d-%H%M%S-%f}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-popup-blocking")

    configured_chrome = os.environ.get("GOOGLE_CHROME_BIN")
    chrome_candidates = ([Path(configured_chrome).expanduser()] if configured_chrome else [])
    chrome_candidates.extend(_chrome_candidates())
    for chrome_path in chrome_candidates:
        if chrome_path and chrome_path.is_file():
            options.binary_location = str(chrome_path)
            break

    driver_paths = _driver_candidates()
    if driver_paths:
        try:
            driver = webdriver.Chrome(service=Service(str(driver_paths[0])), options=options)
        except Exception as cached_error:
            # A cached driver may lag behind an auto-updated Chrome. Let
            # Selenium Manager resolve a compatible driver before failing.
            try:
                driver = webdriver.Chrome(options=options)
            except Exception:
                raise cached_error
    else:
        # Selenium Manager handles the normal PATH/download resolution on both
        # Windows and macOS when no local driver was found.
        driver = webdriver.Chrome(options=options)
    driver.set_window_size(1200, 900)
    return driver
