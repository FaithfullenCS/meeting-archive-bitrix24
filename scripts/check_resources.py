"""Fast CI assertions for packaging inputs; no worker imports or model downloads."""
from pathlib import Path
import re


def main():
    root = Path(__file__).resolve().parents[1] / "meeting_archive"
    for profile, torch_suffix in (("cpu", "+cpu"), ("cuda", "+cu128"), ("cuda126", "+cu126")):
        text = (root / f"resources/worker-{profile}.lock").read_text(encoding="utf-8")
        assert f"torch==2.7.1{torch_suffix}" in text
        assert f"torchaudio==2.7.1{torch_suffix}" in text
        assert "https://download.pytorch.org/whl/" in text
        requirements = re.split(r"\n(?=[a-zA-Z0-9])", text)
        for requirement in requirements:
            if re.match(r"[a-zA-Z0-9][^\n]*==", requirement):
                assert "--hash=sha256:" in requirement, requirement.splitlines()[0]
        assert not re.search(r"https?://[^\s/]+:[^\s/]+@", text), "Credentials in lock file"
    assert (root / "worker/entry.py").is_file()
    assert (root / "static/index.html").is_file()
    assert (root / "static/app.js").is_file()
    assert (root / "static/styles.css").is_file()
    for name in ("chat-archive.js", "chat-archive.css"):
        assert (root / "static" / name).is_file()
    assert (root / "static/icon.png").is_file()
    assert (root / "static/icon.svg").is_file()
    assert (root / "static/favicon.ico").is_file()
    for name in ("worker-native.lock", "worker-gigaam-addons.lock"):
        text = (root / "resources" / name).read_text("utf-8")
        requirements = re.split(r"\n(?=[a-zA-Z0-9])", text)
        for requirement in requirements:
            if re.match(r"[a-zA-Z0-9][^\n]*==", requirement):
                assert "--hash=sha256:" in requirement, requirement.splitlines()[0]
        assert not re.search(r"https?://[^\s/]+:[^\s/]+@", text), "Credentials in lock file"
    for name in ("gigaam", "parakeet"):
        assert (root / "worker/core" / (name + ".py")).is_file()
        assert (root / "worker" / ("install_" + name + ".py")).is_file()
    print("Worker profiles have pins and hashes; runtime resources exist.")


if __name__ == "__main__":
    main()
