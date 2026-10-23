"""Read-only host GPU inspection used to explain optional GPU runtime eligibility.

Detection never loads a driver library into the engine and never selects a device
for the user; the Whisper device stays an explicit model-profile option.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

_QUERY = "name,driver_version,memory.total"


def _nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if os.name == "nt":
        candidate = (
            Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        )
        return str(candidate) if candidate.is_file() else None
    return None


def parse_nvidia_smi(output: str) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3 or not parts[0]:
            continue
        try:
            driver_major = int(parts[1].split(".")[0])
            vram = int(float(parts[2])) * 1024 * 1024
        except ValueError:
            continue
        gpus.append(
            {"name": parts[0], "driver": parts[1], "driver_major": driver_major, "vram_bytes": vram}
        )
    return gpus


def detect_nvidia() -> list[dict[str, Any]]:
    executable = _nvidia_smi()
    if executable is None:
        return []
    try:
        result = subprocess.run(
            [executable, f"--query-gpu={_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_nvidia_smi(result.stdout) if result.returncode == 0 else []


def evaluate_gpu_requirement(
    requirement: dict[str, Any], gpus: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return {met, reasons, gpus} for a descriptor ``host_requirements.gpu`` block."""
    if requirement.get("vendor") != "nvidia":
        return {"met": False, "reasons": ["Unsupported GPU vendor requirement"], "gpus": []}
    if not gpus:
        return {
            "met": False,
            "reasons": ["No NVIDIA GPU or nvidia-smi was found on this computer"],
            "gpus": [],
        }
    reasons: list[str] = []
    eligible = [
        gpu
        for gpu in gpus
        if gpu["driver_major"] >= requirement.get("min_driver_major", 0)
        and gpu["vram_bytes"] >= requirement.get("min_vram_bytes", 0)
    ]
    if not eligible:
        reasons.append(
            f"No NVIDIA GPU meets driver >= {requirement.get('min_driver_major', 0)} "
            f"and {requirement.get('min_vram_bytes', 0) // (1024 * 1024)} MiB memory"
        )
    return {"met": bool(eligible), "reasons": reasons, "gpus": gpus}
