"""Actual Windows installed-surface smoke; no mock window or direct engine HTTP.

Requires explicit Windows UI acceptance dependency pywinauto, an interactive unlocked
Windows desktop, built WebView2 application and its bundled real engine. Run with no
existing Meeting Assistant instance. This does not prove microphone correctness.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", required=True, type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Windows UIA smoke requires Windows; macOS/Linux are experimental untested")
    if not args.installed.is_absolute() or not args.installed.is_file():
        parser.error("--installed must select the actual installed executable")
    from pywinauto import Application

    app = Application(backend="uia").start(str(args.installed))
    window = app.window(title="Meeting Assistant")
    window.wait("visible enabled ready", timeout=45)
    window.set_focus()
    handle = window.handle
    second = subprocess.Popen([str(args.installed)])
    second.wait(timeout=15)
    assert window.handle == handle and window.is_active(), (
        "Second instance did not focus the original window"
    )
    window.type_keys("^,")
    window.child_window(title="Settings", control_type="Text").wait("visible", timeout=15)
    window.type_keys("^k")
    window.child_window(title="Query", control_type="Edit").wait("visible", timeout=15)
    # Genuine keyboard entry, never filling React state through a fake transport.
    window.child_window(title="Query", control_type="Edit").type_keys("native{SPACE}acceptance")
    window.type_keys("^,")
    window.child_window(title="Settings", control_type="Text").wait("visible", timeout=15)
    print(
        json.dumps(
            {
                "installed_executable": str(args.installed),
                "window_handle": handle,
                "actual_window": True,
                "single_instance_focus": True,
                "keyboard_settings_search": True,
                "hardware_exercised": False,
            }
        )
    )
    # Deliberately do not terminate an application or discard a meeting. The user
    # closes via the application's Stop and preserve / Quit reservation workflow.


if __name__ == "__main__":
    main()
