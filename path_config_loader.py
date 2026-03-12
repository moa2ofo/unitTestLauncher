from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class PathsConfig:
    script_path: Path
    script_dir: Path
    project_root: Path
    git_result: Path
    unit_execution_folder: Path
    unit_execution_folder_test: Path
    unit_execution_folder_build: Path
    unit_result_folder: Path
    docker_mount_source: Path


_REQUIRED_KEYS = {
    "project_root",
    "git_result",
    "unit_execution_folder",
    "unit_execution_folder_test",
    "unit_execution_folder_build",
    "unit_result_folder",
    "docker_mount_source",
}


def _resolve_path(base_dir: Path, raw_value: str) -> Path:
    path = Path(raw_value)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _load_yaml(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"YAML config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML structure in {config_path}: expected a mapping at root level")

    paths_section = data.get("paths", data)
    if not isinstance(paths_section, dict):
        raise ValueError(f"Invalid 'paths' section in {config_path}: expected a mapping")

    missing_keys = sorted(_REQUIRED_KEYS - set(paths_section))
    if missing_keys:
        raise KeyError(f"Missing required path keys in {config_path}: {', '.join(missing_keys)}")

    return paths_section


def load_paths(current_file: str | Path, config_name: str = "unit_tests_paths.yml") -> PathsConfig:
    script_path = Path(current_file).resolve()
    script_dir = script_path.parent
    config_path = script_dir / config_name
    paths = _load_yaml(config_path)

    return PathsConfig(
        script_path=script_path,
        script_dir=script_dir,
        project_root=_resolve_path(script_dir, paths["project_root"]),
        git_result=_resolve_path(script_dir, paths["git_result"]),
        unit_execution_folder=_resolve_path(script_dir, paths["unit_execution_folder"]),
        unit_execution_folder_test=_resolve_path(script_dir, paths["unit_execution_folder_test"]),
        unit_execution_folder_build=_resolve_path(script_dir, paths["unit_execution_folder_build"]),
        unit_result_folder=_resolve_path(script_dir, paths["unit_result_folder"]),
        docker_mount_source=_resolve_path(script_dir, paths["docker_mount_source"]),
    )
