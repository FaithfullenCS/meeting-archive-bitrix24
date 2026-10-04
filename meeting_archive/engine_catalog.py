"""Explicit engine/model catalogue; downloading a file does not register a runtime."""
import json
from pathlib import Path
from .worker.core.model_catalog import GIGAAM_MODEL, PARAKEET_MODEL, WHISPER_MODELS, whisper_repo


def read_marker(path: Path) -> dict:
    try:
        if path.stat().st_size > 2 * 1024**2:
            return {}
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def present(path: Path, size: int | None = None) -> bool:
    try:
        return path.is_file() and (path.stat().st_size == size if size is not None else path.stat().st_size > 0)
    except OSError:
        return False


def full_runtime_ready(root: Path, profile: str) -> bool:
    ready = read_marker(root / "ready.json")
    compatible = ready.get("profile") == profile or (ready.get("profile") == "cuda" and profile == "cpu")
    return compatible and present(root / "venv/Scripts/python.exe") and present(root / "venv/Lib/site-packages/torch/__init__.py")


def whisper_installed(root: Path, model: str):
    directory = root / "models/hub" / ("models--" + whisper_repo(model).replace("/", "--"))
    return any(all(present(snapshot / name) for name in ("model.bin", "config.json", "tokenizer.json"))
               and any(present(snapshot / name) for name in ("vocabulary.json", "vocabulary.txt"))
               for snapshot in directory.glob("snapshots/*"))


def parakeet_installed(root: Path, profile: str) -> bool:
    from .worker.install_parakeet import MODEL_BYTES, MODEL_FILE, MODEL_REVISION, VERSION
    engine = root / "engines/parakeet"
    ready = read_marker(engine / f"ready-{profile}.json")
    runtime_path, model_path = ready.get("runtime_path"), ready.get("model_path")
    if not all(isinstance(value, str) and value for value in (runtime_path, model_path)):
        return False
    binary = Path(runtime_path)
    expected_model = engine / "models" / MODEL_FILE
    return (bool(read_marker(root / "ready.json")) and present(root / "venv/Scripts/python.exe")
            and ready.get("model") == PARAKEET_MODEL and ready.get("profile") == profile
            and ready.get("runtime_version") == VERSION and ready.get("model_revision") == MODEL_REVISION
            and Path(model_path) == expected_model
            and binary.is_absolute() and binary.is_relative_to(engine / ("runtime-" + profile))
            and present(binary) and present(expected_model, MODEL_BYTES))


def gigaam_installed(root: Path, profile: str) -> bool:
    from .worker.core.gigaam import MODEL_FILES, MODEL_REVISION, SOURCE_COMMIT, SOURCE_FILES_SHA256
    engine = root / "engines/gigaam"
    ready = read_marker(engine / "ready.json")
    # Hashes are verified by the worker before inference; the catalogue performs
    # cheap availability/size checks so it never re-reads multi-GB weights per poll.
    return (ready.get("model") == GIGAAM_MODEL and ready.get("source_commit") == SOURCE_COMMIT
            and ready.get("model_revision") == MODEL_REVISION and full_runtime_ready(root, profile)
            and all(present(engine / "model" / name, size) for name, (size, _) in MODEL_FILES.items())
            and all(present(engine / "packages/gigaam" / name) for name in SOURCE_FILES_SHA256)
            and all(present(engine / "addons" / name) for name in
                    ("hydra/__init__.py", "omegaconf/__init__.py", "silero_vad/__init__.py", "sentencepiece/__init__.py")))


def catalogue(root: Path, profile: str = "cuda") -> dict:
    whisper = [{"id": name, "label": "Whisper " + name, "download_gb": size,
                "installed": full_runtime_ready(root, profile) and whisper_installed(root, name), "languages": "en" if name.endswith(".en") else "auto"}
               for name, (size, _, _) in WHISPER_MODELS.items()]
    return {"engines": [
        {"id": "whisper", "label": "Whisper", "models": whisper},
        {"id": "parakeet", "label": "Parakeet v3", "models": [{"id": PARAKEET_MODEL,
            "label": "Parakeet TDT 0.6B v3 · Q8", "download_gb": .714,
            "installed": parakeet_installed(root, profile), "languages": "auto"}]},
        {"id": "gigaam", "label": "GigaAM v3", "models": [{"id": GIGAAM_MODEL,
            "label": "GigaAM v3 e2e RNNT · русский", "download_gb": .449,
            "installed": gigaam_installed(root, profile), "languages": "ru"}]},
    ]}


def validate_packages(packages: list[dict]) -> list[dict]:
    allowed = {"whisper": set(WHISPER_MODELS), "parakeet": {PARAKEET_MODEL}, "gigaam": {GIGAAM_MODEL}}
    if not isinstance(packages, list) or not packages or len(packages) > 20:
        raise ValueError("Выберите хотя бы один пакет расшифровки")
    result, seen = [], set()
    for package in packages:
        if not isinstance(package, dict) or package.get("model") not in allowed.get(package.get("engine"), set()):
            raise ValueError("Выберите поддерживаемую модель из каталога")
        key = (package["engine"], package["model"])
        if key not in seen:
            result.append({"engine": key[0], "model": key[1]})
            seen.add(key)
    return result


def installation_plan(root: Path, profile: str, packages: list[dict], *, full_features=False,
                      cached_whisper=(), borrowed_runtime=False) -> dict:
    if profile not in {"cuda", "cpu"}:
        raise ValueError("Выберите CUDA или CPU")
    packages = validate_packages(packages)
    models = {(engine["id"], m["id"]): m for engine in catalogue(root, profile)["engines"] for m in engine["models"]}
    ready = (root / "ready.json").is_file() and (root / "venv/Scripts/python.exe").is_file()
    full = full_features or any(p["engine"] in {"whisper", "gigaam"} for p in packages)
    full_ready = full_runtime_ready(root, profile)
    # Conservative download/disk estimates are labelled as such. Weights are
    # counted once; Python/PyTorch are shared by all Python engines in this profile.
    dependencies = (7 if profile == "cuda" else 3) if full and not full_ready else (.12 if not ready else 0)
    if borrowed_runtime and all(p["engine"] == "whisper" for p in packages):
        dependencies = 0
    from .worker.install_parakeet import MODEL_BYTES, MODEL_FILE
    def have_weights(package):
        if package["engine"] == "whisper":
            return whisper_installed(root, package["model"]) or package["model"] in cached_whisper
        if package["engine"] == "parakeet":
            return present(root / "engines/parakeet/models" / MODEL_FILE, MODEL_BYTES)
        from .worker.core.gigaam import MODEL_FILES
        return all(present(root / "engines/gigaam/model" / name, size) for name, (size, _) in MODEL_FILES.items())
    weights = sum(models[(p["engine"], p["model"])]["download_gb"] for p in packages if not have_weights(p))
    native = (.101 if profile == "cuda" else .005) if any(p["engine"] == "parakeet" and
        not models[(p["engine"], p["model"])]["installed"] for p in packages) else 0
    addons = .05 if any(p["engine"] == "gigaam" and not models[(p["engine"], p["model"])]["installed"] for p in packages) else 0
    download = round(dependencies + weights + native + addons, 3)
    return {"profile": profile, "packages": packages, "download_gb": download,
            "required_gb": round(download * 2 + .5, 2), "estimate": True,
            "common_ready": bool(borrowed_runtime) or (full_ready if full else ready), "weights_gb": round(weights, 3), "full_features": bool(full_features),
            "note": "Оценка с запасом; общая среда устанавливается один раз. Уже сохранённые модели не скачиваются повторно."}
