#!/usr/bin/env python3
"""Open one HTTP(S) URL in a new window of the default Linux browser."""

from __future__ import annotations

import argparse
import configparser
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit


SNAP_DESKTOP_DIR = Path("/var/lib/snapd/desktop/applications")
FIELD_CODE = re.compile(r"%([A-Za-z%])")
WINDOW_ID = re.compile(r"0x[0-9a-fA-F]+")
GC_WINDOW_TITLE = "III Drone Ground Control"


class BrowserLaunchError(RuntimeError):
    """The default browser could not be launched in a new window."""


def validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
        # Accessing .port also validates malformed bracket/port syntax.
        parsed.port
    except ValueError:
        valid = False
    if not valid or any(character.isspace() or ord(character) < 32 for character in url):
        raise BrowserLaunchError("URL must be one valid absolute http or https URL")


def _xdg_command(name: str, arguments: list[str], env: dict[str, str]) -> str | None:
    executable = shutil.which(name, path=env.get("PATH"))
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, *arguments],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip().splitlines()
    return value[0].strip() if value and value[0].strip() else None


def _default_desktop_id(env: dict[str, str]) -> str:
    desktop_id = _xdg_command("xdg-settings", ["get", "default-web-browser"], env)
    if not desktop_id:
        desktop_id = _xdg_command("xdg-mime", ["query", "default", "x-scheme-handler/http"], env)
    if not desktop_id:
        raise BrowserLaunchError("could not resolve the default web browser with xdg-settings or xdg-mime")
    if Path(desktop_id).name != desktop_id or not desktop_id.endswith(".desktop"):
        raise BrowserLaunchError(f"default browser is not a desktop-file ID: {desktop_id!r}")
    return desktop_id


def _application_dirs(env: dict[str, str]) -> list[Path]:
    data_home = Path(env.get("XDG_DATA_HOME") or (Path(env.get("HOME", str(Path.home()))) / ".local/share"))
    data_dirs = env.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    result = [data_home / "applications"]
    result.extend(Path(directory) / "applications" for directory in data_dirs.split(":") if directory)
    result.append(SNAP_DESKTOP_DIR)
    # Preserve search order while avoiding redundant lookups.
    return list(dict.fromkeys(result))


def _desktop_file(desktop_id: str, env: dict[str, str]) -> Path:
    for directory in _application_dirs(env):
        candidate = directory / desktop_id
        if candidate.is_file():
            return candidate
    raise BrowserLaunchError(f"could not find desktop entry {desktop_id!r} in XDG application directories")


def _new_window_command(desktop_file: Path, url: str) -> list[str]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        with desktop_file.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error) as exc:
        raise BrowserLaunchError(f"could not read browser desktop entry {desktop_file}: {exc}") from exc

    if not parser.has_section("Desktop Entry"):
        raise BrowserLaunchError(f"browser desktop entry {desktop_file} has no [Desktop Entry] section")
    actions = parser.get("Desktop Entry", "Actions", fallback="").split(";")
    if "new-window" not in actions or not parser.has_section("Desktop Action new-window"):
        raise BrowserLaunchError(f"browser desktop entry {desktop_file} has no new-window action")
    command = parser.get("Desktop Action new-window", "Exec", fallback="").strip()
    if not command:
        raise BrowserLaunchError(f"browser desktop entry {desktop_file} has no Exec for its new-window action")

    try:
        words = shlex.split(command, posix=True)
    except ValueError as exc:
        raise BrowserLaunchError(f"invalid Exec in browser desktop entry {desktop_file}: {exc}") from exc
    if not words:
        raise BrowserLaunchError(f"empty Exec in browser desktop entry {desktop_file}")

    used_uri = False

    def substitute(match: re.Match[str]) -> str:
        nonlocal used_uri
        code = match.group(1)
        if code in {"u", "U"}:
            used_uri = True
            return url
        if code == "%":
            return "%"
        raise BrowserLaunchError(f"unsupported field code %{code} in new-window Exec")

    arguments = [FIELD_CODE.sub(substitute, word) for word in words]
    if not used_uri:
        # Desktop actions such as packaged Firefox's `firefox -new-window`
        # declare a new window but leave URI passing to the caller.
        arguments.append(url)
    return arguments


def _browser_window_class(desktop_file: Path) -> str:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        parser.read(desktop_file, encoding="utf-8")
    except configparser.Error as exc:
        raise BrowserLaunchError(f"could not read browser desktop entry {desktop_file}: {exc}") from exc
    window_class = parser.get("Desktop Entry", "StartupWMClass", fallback="").strip()
    if not window_class:
        raise BrowserLaunchError(f"default browser desktop entry {desktop_file} has no StartupWMClass for window inspection")
    return window_class


def _ground_control_windows(window_class: str, env: dict[str, str]) -> set[str]:
    xprop = shutil.which("xprop", path=env.get("PATH"))
    if not xprop:
        raise BrowserLaunchError("xprop is required to inspect existing browser windows")
    try:
        root = subprocess.run(
            [xprop, "-root", "_NET_CLIENT_LIST"], env=env,
            capture_output=True, text=True, check=False, timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrowserLaunchError(f"could not inspect desktop windows: {exc}") from exc
    if root.returncode != 0 or "_NET_CLIENT_LIST(WINDOW)" not in root.stdout:
        raise BrowserLaunchError("could not inspect X11 desktop window list")
    matches: set[str] = set()
    for window_id in WINDOW_ID.findall(root.stdout):
        try:
            details = subprocess.run(
                [xprop, "-id", window_id, "WM_CLASS", "_NET_WM_NAME"], env=env,
                capture_output=True, text=True, check=False, timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            continue  # A window can close between the two queries.
        if details.returncode != 0:
            continue
        class_line = next((line for line in details.stdout.splitlines() if line.startswith("WM_CLASS(")), "")
        title_line = next((line for line in details.stdout.splitlines() if line.startswith("_NET_WM_NAME(")), "")
        classes = re.findall(r'"([^"\\]*)"', class_line)
        if window_class in classes and GC_WINDOW_TITLE in title_line:
            matches.add(window_id.lower())
    return matches


def _session_display_environment(env: dict[str, str]) -> dict[str, str]:
    """Recover the user's graphical session when a launcher lacks DISPLAY."""
    if env.get("DISPLAY") and env.get("XAUTHORITY"):
        return env
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"], env=env,
            capture_output=True, text=True, check=False, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return env
    if result.returncode != 0:
        return env
    selected = dict(env)
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"DISPLAY", "XAUTHORITY"} and value and not selected.get(key):
            selected[key] = value
    return selected


def open_default_browser_window(url: str, *, env: dict[str, str] | None = None) -> str:
    validate_url(url)
    process_env = _session_display_environment(dict(os.environ if env is None else env))
    desktop_id = _default_desktop_id(process_env)
    desktop_file = _desktop_file(desktop_id, process_env)
    window_class = _browser_window_class(desktop_file)
    before = _ground_control_windows(window_class, process_env)
    if before:
        return "already-open"
    command = _new_window_command(desktop_file, url)
    try:
        process = subprocess.Popen(
            command,
            env=process_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        try:
            exit_code = process.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            pass
        else:
            if exit_code != 0:
                raise BrowserLaunchError(
                    f"default browser new-window command exited with status {exit_code}"
                )
    except OSError as exc:
        raise BrowserLaunchError(f"could not start default browser new-window command {command[0]!r}: {exc}") from exc
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if _ground_control_windows(window_class, process_env) - before:
            return "opened"
        time.sleep(0.1)
    raise BrowserLaunchError("default browser accepted the command but no new ground-control window appeared")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="one absolute http or https URL")
    args = parser.parse_args(argv)
    try:
        print(open_default_browser_window(args.url))
    except BrowserLaunchError as exc:
        print(f"open_default_browser_window: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
