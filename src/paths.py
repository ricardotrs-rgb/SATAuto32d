from __future__ import annotations

from pathlib import Path

from src.models import ProjectPaths


def iter_paths(paths: ProjectPaths) -> list[Path]:
    return [
        paths.control,
        paths.publico,
        paths.terceros,
        paths.errores,
        paths.logs,
    ]


def find_missing_directories(paths: ProjectPaths) -> list[Path]:
    return [path for path in iter_paths(paths) if not path.exists()]


def ensure_directories(paths: ProjectPaths) -> list[Path]:
    created_paths: list[Path] = []
    for path in find_missing_directories(paths):
            path.mkdir(parents=True, exist_ok=True)
            created_paths.append(path)

    return created_paths