"""Focused behavior tests for the Linux default-browser window helper."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


SCRIPT = Path(__file__).with_name("open_default_browser_window.py")


def _executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def _environment(tmp_path: Path, desktop_entry: str, *, settings: str = "firefox_firefox.desktop\n") -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    applications = tmp_path / "data" / "applications"
    bin_dir.mkdir()
    applications.mkdir(parents=True)
    desktop = applications / "firefox_firefox.desktop"
    desktop.write_text(
        desktop_entry.replace("[Desktop Entry]\n", "[Desktop Entry]\nStartupWMClass=fake-firefox\n", 1),
        encoding="utf-8",
    )
    launch_log = tmp_path / "browser-argv.json"
    windows_log = tmp_path / "windows.json"
    _executable(bin_dir / "xdg-settings", "#!/bin/sh\nprintf '%s' \"$DEFAULT_BROWSER\"\n")
    _executable(
        bin_dir / "xdg-mime",
        "#!/bin/sh\nprintf '%s' \"$FALLBACK_BROWSER\"\n",
    )
    _executable(
        bin_dir / "fake-firefox",
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['BROWSER_LAUNCH_LOG'], 'w', encoding='utf-8') as stream:\n"
        "    json.dump(sys.argv[1:], stream)\n"
        "with open(os.environ['BROWSER_WINDOWS_LOG'], 'w', encoding='utf-8') as stream:\n"
        "    json.dump([{'id':'0xabc123','class':'fake-firefox','title':'III Drone Ground Control'}], stream)\n",
    )
    _executable(
        bin_dir / "xprop",
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "p=Path(os.environ['BROWSER_WINDOWS_LOG'])\n"
        "windows=json.loads(p.read_text()) if p.exists() else []\n"
        "if sys.argv[1]=='-root':\n"
        " print('_NET_CLIENT_LIST(WINDOW): window id # '+', '.join(w['id'] for w in windows))\n"
        "elif sys.argv[1]=='-id':\n"
        " w=next((w for w in windows if w['id']==sys.argv[2]),None)\n"
        " if w is None: raise SystemExit(1)\n"
        " print('WM_CLASS(STRING) = '+json.dumps('Navigator')+', '+json.dumps(w['class']))\n"
        " print('_NET_WM_NAME(UTF8_STRING) = '+json.dumps(w['title']))\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_DATA_DIRS": str(tmp_path / "system-data"),
            "DEFAULT_BROWSER": settings,
            "FALLBACK_BROWSER": "",
            "BROWSER_LAUNCH_LOG": str(launch_log),
            "BROWSER_WINDOWS_LOG": str(windows_log),
        }
    )
    return env, launch_log


def _run(url: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), url], env=env, text=True, capture_output=True, check=False)


def test_firefox_new_window_action_receives_uri(tmp_path):
    env, launch_log = _environment(
        tmp_path,
        "[Desktop Entry]\nType=Application\nActions=new-window;\n"
        "[Desktop Action new-window]\nName=New Window\nExec=fake-firefox --new-window %u\n",
    )

    result = _run("https://example.test/path?q=one%20two", env)

    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 2
    while not launch_log.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert launch_log.exists()
    assert json.loads(launch_log.read_text(encoding="utf-8")) == ["--new-window", "https://example.test/path?q=one%20two"]
    assert result.stdout.strip() == "opened"


def test_existing_ground_control_window_is_reused(tmp_path):
    env, launch_log = _environment(
        tmp_path,
        "[Desktop Entry]\nType=Application\nActions=new-window;\n"
        "[Desktop Action new-window]\nExec=fake-firefox --new-window %u\n",
    )
    Path(env["BROWSER_WINDOWS_LOG"]).write_text(
        json.dumps([{"id": "0xabc123", "class": "fake-firefox", "title": "III Drone Ground Control"}]),
        encoding="utf-8",
    )
    result = _run("http://127.0.0.1:5174", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "already-open"
    assert not launch_log.exists()


def test_browser_uses_user_session_display_when_launcher_has_none(tmp_path):
    env, launch_log = _environment(
        tmp_path,
        "[Desktop Entry]\nType=Application\nActions=new-window;\n"
        "[Desktop Action new-window]\nExec=fake-firefox --new-window %u\n",
    )
    env.pop("DISPLAY", None)
    env.pop("XAUTHORITY", None)
    bin_dir = tmp_path / "bin"
    _executable(
        bin_dir / "systemctl",
        "#!/bin/sh\nprintf 'DISPLAY=:77\\nXAUTHORITY=/tmp/test-Xauthority\\n'\n",
    )
    xprop = bin_dir / "xprop"
    xprop.write_text(
        xprop.read_text(encoding="utf-8").replace(
            "import json,os,sys\n",
            "import json,os,sys\n"
            "assert os.environ.get('DISPLAY') == ':77'\n"
            "assert os.environ.get('XAUTHORITY') == '/tmp/test-Xauthority'\n",
        ), encoding="utf-8",
    )

    result = _run("http://127.0.0.1:5174", env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "opened"
    assert launch_log.exists()


def test_closed_window_reopens_and_unrelated_window_does_not_suppress(tmp_path):
    env, launch_log = _environment(
        tmp_path,
        "[Desktop Entry]\nType=Application\nActions=new-window;\n"
        "[Desktop Action new-window]\nExec=fake-firefox --new-window %u\n",
    )
    windows = Path(env["BROWSER_WINDOWS_LOG"])
    windows.write_text(
        json.dumps([{"id": "0xother", "class": "other-browser", "title": "III Drone Ground Control"}]),
        encoding="utf-8",
    )
    first = _run("http://127.0.0.1:5174", env)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "opened"
    windows.write_text("[]", encoding="utf-8")
    launch_log.unlink()
    second = _run("http://127.0.0.1:5174", env)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "opened"
    assert launch_log.exists()


def test_missing_new_window_action_fails_without_opening_browser(tmp_path):
    env, launch_log = _environment(
        tmp_path,
        "[Desktop Entry]\nType=Application\nExec=fake-firefox %u\n",
    )

    result = _run("https://example.test/", env)

    assert result.returncode == 1
    assert "no new-window action" in result.stderr
    assert not launch_log.exists()


def test_new_window_action_without_uri_field_appends_url(tmp_path):
    env, launch_log = _environment(
        tmp_path,
        "[Desktop Entry]\nType=Application\nActions=new-window;\n"
        "[Desktop Action new-window]\nExec=fake-firefox -new-window\n",
    )
    result = _run("https://example.test/append", env)
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 2
    while not launch_log.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert json.loads(launch_log.read_text(encoding="utf-8")) == ["-new-window", "https://example.test/append"]


def test_immediately_failing_browser_is_reported(tmp_path):
    env, launch_log = _environment(
        tmp_path,
        "[Desktop Entry]\nType=Application\nActions=new-window;\n"
        "[Desktop Action new-window]\nExec=fake-firefox --new-window %u\n",
    )
    _executable(tmp_path / "bin" / "fake-firefox", "#!/bin/sh\nexit 7\n")
    result = _run("https://example.test/", env)
    assert result.returncode == 1
    assert "exited with status 7" in result.stderr
    assert not launch_log.exists()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https:///missing-host", "https://example.test/has space"])
def test_bad_url_is_rejected(tmp_path, url):
    env, launch_log = _environment(
        tmp_path,
        "[Desktop Entry]\nType=Application\nActions=new-window;\n"
        "[Desktop Action new-window]\nExec=fake-firefox --new-window %u\n",
    )

    result = _run(url, env)

    assert result.returncode == 1
    assert "valid absolute http or https URL" in result.stderr
    assert not launch_log.exists()


def test_missing_default_browser_fails_clearly(tmp_path):
    env, launch_log = _environment(
        tmp_path,
        "[Desktop Entry]\nType=Application\nActions=new-window;\n"
        "[Desktop Action new-window]\nExec=fake-firefox --new-window %u\n",
        settings="",
    )

    result = _run("https://example.test/", env)

    assert result.returncode == 1
    assert "could not resolve the default web browser" in result.stderr
    assert not launch_log.exists()


def test_xdg_mime_fallback_resolves_default_desktop_id(tmp_path):
    env, launch_log = _environment(
        tmp_path,
        "[Desktop Entry]\nType=Application\nActions=new-window;\n"
        "[Desktop Action new-window]\nExec=fake-firefox --new-window %u\n",
        settings="",
    )
    env["FALLBACK_BROWSER"] = "firefox_firefox.desktop\n"

    result = _run("https://example.test/fallback", env)

    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 2
    while not launch_log.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert json.loads(launch_log.read_text(encoding="utf-8")) == ["--new-window", "https://example.test/fallback"]
