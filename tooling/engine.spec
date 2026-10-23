# PyInstaller specification for the engine only, never heavyweight inference.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

root = Path(SPECPATH).parent
resources = collect_data_files("meeting_assistant") + collect_data_files("reportlab", includes=["fonts/Vera*.ttf", "fonts/bitstream-vera-license.txt"])
for source, destination in ((root / "engine/migrations", "meeting_assistant/migrations"), (root / "prompts", "prompts"), (root / "catalog", "catalog"), (root / "release.toml", "."), (root / "packages/contracts/generated", "contracts")):
    if source.exists():
        resources.append((str(source), destination))
# Only identity-less developer bundles carry first-party build provenance data.
# These are comparison inputs, never plugin entry points or imported modules.
import tomllib
release = tomllib.loads((root / "release.toml").read_text(encoding="utf-8"))
if not release.get("publisher", {}).get("repository"):
    resources.append((str(root / "engine/meeting_assistant/infrastructure/workers/runtime.py"), "developer-worker-sources"))
    for family in ("whisper", "sherpa", "pyannote", "embeddings", "azure-speech"):
        for name in ("worker.py", "uv.lock"):
            resources.append((str(root / "workers" / family / name), f"developer-worker-sources/{family}"))
# pkgutil-based collection skips namespace directories used by service registration.
engine_modules = [
    ".".join(path.relative_to(root / "engine").with_suffix("").parts)
    for path in sorted((root / "engine/meeting_assistant").rglob("*.py"))
    if path.name != "__init__.py"
]
analysis = Analysis(
    [str(root / "tooling/engine_entry.py")],
    pathex=[str(root / "engine")],
    binaries=collect_dynamic_libs("sqlite_vec"),
    datas=resources,
    hiddenimports=engine_modules + collect_submodules("uvicorn") + ["sqlite_vec", "pyrage"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["torch", "pyannote", "sentence_transformers", "faster_whisper", "ctranslate2", "sherpa_onnx", "azure.cognitiveservices.speech"],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(archive, analysis.scripts, [], exclude_binaries=True, name="meeting-assistant-engine", debug=False, strip=False, upx=False, console=True)
collection = COLLECT(executable, analysis.binaries, analysis.datas, strip=False, upx=False, name="meeting-assistant-engine")
