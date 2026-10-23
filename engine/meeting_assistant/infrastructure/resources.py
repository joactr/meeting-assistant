"""Read-only application resources in source checkouts and frozen engine bundles."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(cast(str, vars(sys)["_MEIPASS"]))
    return Path(__file__).resolve().parents[3]
