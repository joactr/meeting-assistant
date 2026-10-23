from meeting_assistant.infrastructure.hardware import evaluate_gpu_requirement, parse_nvidia_smi

REQUIREMENT = {"vendor": "nvidia", "min_driver_major": 525, "min_vram_bytes": 2 * 1024**3}


def test_parses_nvidia_smi_and_ignores_malformed_rows() -> None:
    gpus = parse_nvidia_smi(
        "NVIDIA GeForce RTX 4060 Ti, 610.88, 16380\nbroken row\nGPU, notnumber, 1\n"
    )
    assert gpus == [
        {
            "name": "NVIDIA GeForce RTX 4060 Ti",
            "driver": "610.88",
            "driver_major": 610,
            "vram_bytes": 16380 * 1024 * 1024,
        }
    ]


def test_old_driver_or_small_gpu_is_not_eligible_and_explains_why() -> None:
    old = [{"name": "A", "driver": "470.1", "driver_major": 470, "vram_bytes": 8 * 1024**3}]
    small = [{"name": "B", "driver": "560.0", "driver_major": 560, "vram_bytes": 1024**3}]
    for gpus in (old, small):
        result = evaluate_gpu_requirement(REQUIREMENT, gpus)
        assert result["met"] is False and result["reasons"]


def test_no_gpu_is_not_eligible_and_a_capable_gpu_is() -> None:
    assert evaluate_gpu_requirement(REQUIREMENT, [])["met"] is False
    capable = [{"name": "C", "driver": "610.88", "driver_major": 610, "vram_bytes": 16 * 1024**3}]
    assert evaluate_gpu_requirement(REQUIREMENT, capable) == {
        "met": True,
        "reasons": [],
        "gpus": capable,
    }
