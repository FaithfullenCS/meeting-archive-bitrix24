from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from meeting_archive import resource_reuse as reuse


@pytest.fixture
def resources(tmp_path, monkeypatch):
    source = tmp_path / "existing-environment"
    site = source / "Lib/site-packages"
    site.mkdir(parents=True)
    base = tmp_path / "base-python"
    (base / "Lib").mkdir(parents=True)
    (base / "python.exe").write_bytes(b"synthetic runtime")
    (base / "python312.dll").write_bytes(b"synthetic dll")
    (base / "Lib/json.py").write_bytes(b"synthetic stdlib")
    (base / "Lib/site-packages").mkdir()
    (base / "Lib/site-packages/private.py").write_bytes(b"unrelated global package")
    distributions = {}
    for name in reuse.WORKER_PACKAGES:
        relative = name.replace("-", "_").replace(".", "_") + "/__init__.py"
        metadata = name.replace("-", "_").replace(".", "_") + "-1.0.dist-info/METADATA"
        for file in (relative, metadata):
            path = site / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("synthetic public package: " + name).encode())
        version = {"faster-whisper": "1.0.3", "ctranslate2": "4.8.2", "torch": "2.7.1+cu128"}.get(name, "1.0.0")
        distributions[name] = {"version": version, "requires": [], "files": [relative, metadata]}
    # Source configuration, an editable-install path hook, a Hugging Face token
    # and an unrelated installed GUI application must never be adopted.
    for private in ("settings.json", "token", "source.pth", "private.egg-link", "direct_url.json"):
        (site / private).write_bytes(b"synthetic private value; do not read")
        distributions["torch"]["files"].append(private)
    unrelated = site / "old_application.py"
    unrelated.write_bytes(b"synthetic old application")
    distributions["unrelated-gui"] = {"version": "1.0", "requires": [], "files": [unrelated.name]}
    (source / "settings.json").write_bytes(b"synthetic private application settings")
    cache = tmp_path / "public-model-cache"
    repo = cache / "models--Systran--faster-whisper-tiny"
    snapshot = repo / "snapshots/synthetic-commit"
    snapshot.mkdir(parents=True)
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json"):
        (snapshot / name).write_bytes(("public model " + name).encode())
    (repo / "refs").mkdir()
    (repo / "refs/main").write_text("synthetic-commit", encoding="utf-8")
    (cache / "token").write_bytes(b"synthetic private Hugging Face token")
    unrelated_model = cache / "models--private--customer-model"
    unrelated_model.mkdir()
    (unrelated_model / "private.bin").write_bytes(b"synthetic private model")
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"synthetic public ffmpeg")
    monkeypatch.setattr(reuse, "environment_candidates", lambda: [source])
    monkeypatch.setattr(reuse, "_cache_root", lambda: cache)
    monkeypatch.setattr(reuse, "_diarization_caches", lambda: (cache,))
    monkeypatch.setattr(reuse, "_ffmpeg", lambda *_: ffmpeg)

    def probe(path):
        own = path != source
        actual = path.parent / "python/reused-python" if own else base
        if own:
            content = (path / "pyvenv.cfg").read_text("utf-8")
            assert str(actual) in content
            assert ".reuse-staging-" not in content
        return {"base_python_root": str(actual), "python_version": [3, 12, 10], "bits": 64,
                "distributions": {k: v for k, v in distributions.items() if not own or k != "unrelated-gui"},
                "environment_path": str(path), "site_packages": str(path / "Lib/site-packages")}

    def uv(_uv, runtime, venv, _cancelled):
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts/python.exe").write_bytes(b"synthetic own venv launcher")
        (venv / "Lib/site-packages").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text(
            f"home = {runtime.parent}\nbase-prefix = {runtime.parent}\n"
            f"base-executable = {runtime}\ncommand = {runtime} -m venv {venv}\n", encoding="utf-8")

    monkeypatch.setattr(reuse, "_probe_environment", probe)
    monkeypatch.setattr(reuse, "_uv_venv", uv)
    return {"source": source, "site": site, "base": base, "cache": cache, "repo": repo,
            "snapshot": snapshot, "distributions": distributions, "target": tmp_path / "own-module"}


def test_plan_is_read_only_omits_private_files_and_selects_worker_dependencies(resources):
    r = resources
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in r["source"].rglob("*") if p.is_file()}
    discovered = reuse.discover_resources()
    assert len(discovered["sources"]) == 1
    source = discovered["sources"][0]
    assert source["compatible"] and source["models"] == ["tiny"]
    plan = reuse.plan_resources(r["source"], "tiny", r["target"])
    assert plan["download_bytes"] == 0
    assert plan["storage_mode"] == "hardlink"
    assert plan["source_id"] == source["source_id"]
    assert "unrelated-gui" not in plan["packages"]
    assert not r["target"].exists()
    assert before == {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in r["source"].rglob("*") if p.is_file()}


def test_cached_diarization_and_embedding_from_pyannote_cache_are_materialized_without_tokens(resources, monkeypatch):
    r = resources
    diarization_cache = r["source"].parent / "public-pyannote-cache"
    originals = []
    for repository in reuse.DIARIZATION_REPOS:
        snapshot = diarization_cache / repository / "snapshots/synthetic-diarization"
        snapshot.mkdir(parents=True)
        public_file = snapshot / "config.yaml"
        public_file.write_bytes(b"synthetic public model configuration")
        originals.append(public_file)
    (diarization_cache / "token").write_bytes(b"synthetic token must not be copied")
    monkeypatch.setattr(reuse, "_diarization_caches", lambda: (r["cache"], diarization_cache))
    plan = reuse.plan_resources(r["source"], "tiny", r["target"])
    assert set(reuse.DIARIZATION_REPOS).issubset({p["repo"] for p in plan["model_repos"]})
    assert plan["download_bytes"] == 0
    reuse.reuse_resources(plan, r["target"], Path("synthetic-uv"), lambda *_: None, lambda: False)
    for original in originals:
        repository = original.parents[2].name
        destination = r["target"] / "models/hub" / repository
        assert os.path.samefile(destination / "snapshots/synthetic-diarization/config.yaml", original)
        assert (destination / "refs/main").read_text("utf-8") == "synthetic-diarization"
    assert not (r["target"] / "models/hub/token").exists()


def test_whisper_text_vocabulary_is_reusable(resources):
    r = resources
    (r["snapshot"] / "vocabulary.json").rename(r["snapshot"] / "vocabulary.txt")
    assert reuse.discover_resources()["sources"][0]["models"] == ["tiny"]
    assert reuse.plan_resources(r["source"], "tiny", r["target"])["download_bytes"] == 0


def test_hardlink_clone_has_independent_paths_and_own_base_python(resources):
    r = resources
    original = r["site"] / "torch/__init__.py"
    before = original.stat()
    plan = reuse.plan_resources(r["source"], "tiny", r["target"])
    ready = reuse.reuse_resources(plan, r["target"], Path("synthetic-uv"), lambda *_: None, lambda: False)
    copied = r["target"] / "venv/Lib/site-packages/torch/__init__.py"
    assert os.path.samefile(copied, original)
    assert original.stat().st_mtime_ns == before.st_mtime_ns
    assert ready["reused"] and not ready["pinned"] and not ready["gpu_verified"]
    assert ready["shared_bytes"] == plan["source_bytes"] and ready["exclusive_bytes"] == 0
    assert ready["model_cache"] == str(r["target"] / "models/hub")
    own_site = r["target"] / "venv/Lib/site-packages"
    for private in ("settings.json", "token", "source.pth", "private.egg-link", "direct_url.json", "old_application.py"):
        assert not (own_site / private).exists()
    assert not (r["target"] / "python/reused-python/Lib/site-packages/private.py").exists()
    assert not (r["target"] / "models/hub/token").exists()
    assert not (r["target"] / "models/hub/models--private--customer-model").exists()
    assert not list(r["target"].glob(".reuse-staging-*"))
    assert not (r["target"] / "ready.json").exists()  # Caller publishes only after success.
    original.unlink()
    assert copied.read_bytes() == b"synthetic public package: torch"


def test_cross_volume_plan_requires_space_and_copy_is_independent(resources, monkeypatch):
    r = resources
    monkeypatch.setattr(reuse, "_destination_device", lambda _: -1)
    plan = reuse.plan_resources(r["source"], "tiny", r["target"])
    assert plan["storage_mode"] == "copy" and plan["required_bytes"] == plan["source_bytes"]
    ready = reuse.reuse_resources(plan, r["target"], Path("synthetic-uv"), lambda *_: None, lambda: False)
    original = r["site"] / "torch/__init__.py"
    copied = r["target"] / "venv/Lib/site-packages/torch/__init__.py"
    assert not os.path.samefile(copied, original)
    assert copied.read_bytes() == original.read_bytes()
    assert ready["shared_bytes"] == 0 and ready["exclusive_bytes"] == plan["source_bytes"]


def test_no_silent_hardlink_to_copy_fallback(resources, monkeypatch):
    r = resources
    plan = reuse.plan_resources(r["source"], "tiny", r["target"])
    monkeypatch.setattr(os, "link", lambda *_: (_ for _ in ()).throw(OSError("synthetic unsupported hardlink")))
    with pytest.raises(RuntimeError, match="hardlink"):
        reuse.reuse_resources(plan, r["target"], Path("synthetic-uv"), lambda *_: None, lambda: False)
    assert not (r["target"] / "venv").exists()
    assert not list(r["target"].glob(".reuse-staging-*"))
    assert (r["site"] / "torch/__init__.py").is_file()


def test_cancel_removes_only_new_staging_and_preserves_source_and_existing_module(resources):
    r = resources
    r["target"].mkdir()
    (r["target"] / reuse.MARKER).write_text("owned", encoding="utf-8")
    (r["target"] / "venv").mkdir()
    (r["target"] / "venv/preserve.txt").write_bytes(b"existing own worker")
    plan = reuse.plan_resources(r["source"], "tiny", r["target"])
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 6

    with pytest.raises(reuse.ResourceReuseCancelled):
        reuse.reuse_resources(plan, r["target"], Path("synthetic-uv"), lambda *_: None, cancelled)
    assert (r["target"] / "venv/preserve.txt").read_bytes() == b"existing own worker"
    assert (r["source"] / "settings.json").read_bytes() == b"synthetic private application settings"
    assert not list(r["target"].glob(".reuse-staging-*"))


def test_failed_verification_rolls_back_all_published_components(resources, monkeypatch):
    r = resources
    r["target"].mkdir()
    (r["target"] / reuse.MARKER).write_text("owned", encoding="utf-8")
    for component in ("python", "venv", "models", "tools"):
        folder = r["target"] / component
        folder.mkdir()
        (folder / "preserve.txt").write_bytes(component.encode())
    plan = reuse.plan_resources(r["source"], "tiny", r["target"])
    original_probe = reuse._probe_environment

    def fail_owned(path):
        if path != r["source"]:
            raise ValueError("synthetic final verification failure")
        return original_probe(path)

    monkeypatch.setattr(reuse, "_probe_environment", fail_owned)
    with pytest.raises(ValueError, match="synthetic final"):
        reuse.reuse_resources(plan, r["target"], Path("synthetic-uv"), lambda *_: None, lambda: False)
    for component in ("python", "venv", "models", "tools"):
        assert (r["target"] / component / "preserve.txt").read_bytes() == component.encode()
    assert not list(r["target"].glob(".reuse-staging-*"))


def test_source_or_storage_change_requires_new_plan_before_target_mutation(resources, monkeypatch):
    r = resources
    plan = reuse.plan_resources(r["source"], "tiny", r["target"])
    monkeypatch.setattr(reuse, "_destination_device", lambda _: -1)
    with pytest.raises(ValueError, match="заново подтвердите"):
        reuse.reuse_resources(plan, r["target"], Path("synthetic-uv"), lambda *_: None, lambda: False)
    assert not r["target"].exists()


def test_target_may_not_adopt_foreign_files_or_overlap_sources(resources):
    r = resources
    r["target"].mkdir()
    private = r["target"] / "keep.txt"
    private.write_bytes(b"foreign file")
    plan = reuse.plan_resources(r["source"], "tiny", r["target"])
    with pytest.raises(ValueError, match="не принадлежит"):
        reuse.reuse_resources(plan, r["target"], Path("synthetic-uv"), lambda *_: None, lambda: False)
    assert private.read_bytes() == b"foreign file"
    nested = r["source"] / "owned-module"
    plan = reuse.plan_resources(r["source"], "tiny", nested)
    with pytest.raises(ValueError, match="пересекается"):
        reuse.reuse_resources(plan, nested, Path("synthetic-uv"), lambda *_: None, lambda: False)
    assert not nested.exists()


def test_missing_selected_model_is_not_downloaded(resources):
    with pytest.raises(ValueError, match="ещё не сохранена"):
        reuse.plan_resources(resources["source"], "large-v3", resources["target"])
    assert not resources["target"].exists()


@pytest.mark.parametrize("model,repo", [("tiny.en", "Systran/faster-whisper-tiny.en"),
                                      ("large-v3-turbo", "dropbox-dash/faster-whisper-large-v3-turbo")])
def test_reuse_catalog_selects_only_existing_supported_snapshot(resources, model, repo):
    r = resources
    directory = r["cache"] / ("models--" + repo.replace("/", "--"))
    snapshot = directory / "snapshots/synthetic-model-revision"
    snapshot.mkdir(parents=True)
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json"):
        (snapshot / name).write_bytes(b"synthetic public model")
    plan = reuse.plan_resources(r["source"], model, r["target"])
    assert plan["model_repos"][0]["repo"] == directory.name
    assert model in reuse.discover_resources()["sources"][0]["models"]
    assert plan["download_bytes"] == 0
    ready = reuse.reuse_resources(plan, r["target"], Path("synthetic-uv"), lambda *_: None, lambda: False)
    assert ready["model"] == model
    assert (r["target"] / "models/hub" / directory.name / "refs/main").read_text("utf-8") == snapshot.name


def test_reuse_preserves_public_stdlib_token_and_certifi_certificate_only(resources):
    r = resources
    (r["base"] / "Lib/token.py").write_bytes(b"synthetic public standard library")
    (r["site"] / "certifi").mkdir()
    (r["site"] / "certifi/cacert.pem").write_bytes(b"synthetic public certificate bundle")
    (r["site"] / "certifi/private.key").write_bytes(b"synthetic private key, must not copy")
    r["distributions"]["certifi"] = {"version": "2026.1", "requires": [],
        "files": ["certifi/cacert.pem", "certifi/private.key"]}
    r["distributions"]["faster-whisper"]["requires"] = ["certifi"]
    plan = reuse.plan_resources(r["source"], "tiny", r["target"])
    reuse.reuse_resources(plan, r["target"], Path("synthetic-uv"), lambda *_: None, lambda: False)
    assert (r["target"] / "python/reused-python/Lib/token.py").is_file()
    own_certifi = r["target"] / "venv/Lib/site-packages/certifi"
    assert (own_certifi / "cacert.pem").is_file() and not (own_certifi / "private.key").exists()


def test_dependency_closure_ignores_unrequested_extras(resources):
    r = resources
    for name in ("shared-dependency", "optional-gui"):
        path = r["site"] / (name + ".py")
        path.write_bytes(b"synthetic dependency")
        r["distributions"][name] = {"version": "1.0", "requires": [], "files": [path.name]}
    r["distributions"]["torch"]["requires"] = ["shared-dependency>=1", "optional-gui; extra == 'gui'"]
    plan = reuse.plan_resources(r["source"], "tiny", r["target"])
    assert "shared-dependency" in plan["packages"] and "optional-gui" not in plan["packages"]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction containment")
@pytest.mark.parametrize("source_link", [False, True])
def test_rejects_source_and_target_junction_without_touching_external(resources, tmp_path, monkeypatch, source_link):
    import _winapi
    r = resources
    external = tmp_path / "external-private"
    external.mkdir()
    (external / "preserve.txt").write_bytes(b"private external file")
    link = r["site"] / "torch" if source_link else r["target"]
    if source_link:
        # Replace only this synthetic fixture directory with a junction.
        (link / "__init__.py").unlink()
        link.rmdir()
    _winapi.CreateJunction(str(external), str(link))
    try:
        with pytest.raises(ValueError):
            reuse.plan_resources(r["source"], "tiny", r["target"])
        assert (external / "preserve.txt").read_bytes() == b"private external file"
    finally:
        os.rmdir(link)


def test_preflight_disables_site_hooks_network_implicit_token_and_python_injection(tmp_path, monkeypatch):
    source, base = tmp_path / "venv", tmp_path / "base"
    (source / "Scripts").mkdir(parents=True)
    (source / "Lib/site-packages").mkdir(parents=True)
    (source / "Scripts/python.exe").write_bytes(b"synthetic source launcher")
    base.mkdir()
    (base / "python.exe").write_bytes(b"synthetic runtime")
    monkeypatch.setenv("HF_TOKEN", "synthetic token")
    monkeypatch.setenv("PYTHONPATH", "synthetic application source")
    monkeypatch.setenv("PYTHONHOME", "synthetic application runtime")

    def run(args, **kwargs):
        assert args[1:4] == ["-I", "-S", "-B"]
        assert args[-1] == str(source / "Lib/site-packages")
        env = kwargs["env"]
        assert "HF_TOKEN" not in env and "PYTHONPATH" not in env and "PYTHONHOME" not in env
        assert env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1" and env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env["HF_HUB_OFFLINE"] == "1" and kwargs["timeout"] == 20
        return subprocess.CompletedProcess(args, 0, json.dumps({"base_python_root": str(base),
            "python_version": [3, 12, 10], "bits": 64, "distributions": {}}), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert reuse._probe_environment(source)["base_python_root"] == str(base)


def test_cancelled_discovery_does_not_probe_another_environment(resources, monkeypatch):
    checked = []
    monkeypatch.setattr(reuse, "_probe_environment", lambda path: checked.append(path))
    with pytest.raises(reuse.ResourceReuseCancelled):
        reuse.discover_resources(cancelled=lambda: True)
    assert checked == []


def test_discovery_cancellation_interrupts_package_file_scan(resources, monkeypatch):
    inspected = []
    original = reuse._safe_file

    def inspect(path, root):
        inspected.append(path)
        return original(path, root)

    monkeypatch.setattr(reuse, "_safe_file", inspect)
    with pytest.raises(reuse.ResourceReuseCancelled):
        reuse.discover_resources(cancelled=lambda: bool(inspected))
    assert len(inspected) == 1
