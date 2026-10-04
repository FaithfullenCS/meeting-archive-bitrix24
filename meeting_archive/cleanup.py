"""Preview and remove explicit, confined local material selections."""
from __future__ import annotations

import hashlib
import json
import shutil
import stat
from pathlib import Path


def reparse(path: Path) -> bool:
    value = path.lstat()
    return path.is_symlink() or bool(getattr(value, "st_file_attributes", 0) &
                                   getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def selection_plan(root: Path, targets: list[str], *, internal_links=False) -> dict:
    root = root.absolute()
    if root.resolve() != root:
        raise ValueError("Папка содержит внешнюю ссылку. Удаление остановлено")
    for parent in (root, *root.parents):
        if parent.exists() and reparse(parent):
            raise ValueError("Папка содержит внешнюю ссылку. Удаление остановлено")
    if not isinstance(targets, list) or not targets or len(targets) > 1000:
        raise ValueError("Выберите материалы для удаления")
    names = set()
    for name in targets:
        if not isinstance(name, str) or not name or "\\" in name or ":" in name:
            raise ValueError("Недопустимый путь удаления")
        parts = name.split("/")
        if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
            raise ValueError("Недопустимый путь удаления")
        names.add(name)
    names = sorted(name for name in names if not any(name.startswith(other + "/") for other in names if name != other))
    entries, items = [], []
    def inspect(path):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if reparse(path):
            if not internal_links or not path.resolve().is_relative_to(root):
                raise ValueError("В выбранных материалах обнаружена внешняя ссылка. Удаление остановлено")
            entries.append((relative, "link", 0, info.st_mtime_ns))
            return
        kind = "directory" if path.is_dir() else "file"
        entries.append((relative, kind, info.st_size if kind == "file" else 0, info.st_mtime_ns))
        if kind == "directory":
            for child in sorted(path.iterdir()):
                inspect(child)
    for name in names:
        path = root.joinpath(*name.split("/"))
        for parent in (path, *path.parents):
            if parent == root:
                break
            if parent.exists() and reparse(parent):
                raise ValueError("Выбранный путь содержит ссылку. Удаление остановлено")
        if not path.resolve().is_relative_to(root):
            raise ValueError("Путь удаления выходит за пределы выбранной папки")
        before = len(entries)
        if path.exists():
            inspect(path)
        content = entries[before:]
        items.append({"name": name, "files": sum(item[1] in {"file", "link"} for item in content),
                      "bytes": sum(item[2] for item in content)})
    fingerprint = hashlib.sha256(json.dumps([str(root), names, sorted(entries)], ensure_ascii=False).encode()).hexdigest()
    return {"token": fingerprint, "targets": names, "items": items,
            "files": sum(item["files"] for item in items), "bytes": sum(item["bytes"] for item in items)}


def remove_selection(root: Path, targets: list[str], token: str, *, internal_links=False) -> dict:
    plan = selection_plan(root, targets, internal_links=internal_links)
    if not isinstance(token, str) or token != plan["token"]:
        raise ValueError("Состав файлов изменился. Откройте удаление заново и проверьте список")
    for name in plan["targets"]:
        path = root.joinpath(*name.split("/"))
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    return plan
