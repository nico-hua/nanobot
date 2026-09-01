"""Load static Markdown Skills from one workspace directory."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SKILL_FILENAME = "SKILL.md"
_FRONTMATTER_DELIMITER = "---"
_SKILL_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])\$(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)(?![A-Za-z0-9_/-]|\.(?=[A-Za-z0-9_/]))"
)
_BIN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SkillMetadataValue = str | bool | tuple[str, ...]


class SkillLoadError(RuntimeError):
    """Raised when a discovered Skill cannot be read."""


class SkillNotFoundError(LookupError):
    """Raised when a named Skill is not available."""


@dataclass(frozen=True)
class SkillInfo:
    """Static metadata for one Markdown Skill."""

    name: str
    description: str
    path: Path
    always: bool = False
    is_available: bool = True
    missing_dependencies: tuple[str, ...] = ()


class SkillsLoader:
    """Discover and read static Skills without executing their contents."""

    def __init__(self, workspace: str | Path) -> None:
        if not isinstance(workspace, (str, Path)):
            raise TypeError("SkillsLoader workspace must be a string or Path")

        self._workspace_skills_path = Path(workspace).resolve() / "skills"

    def list_skills(self) -> tuple[SkillInfo, ...]:
        """Return workspace Skills in a stable name order."""

        return tuple(
            sorted(
                self._scan_directory(self._workspace_skills_path),
                key=lambda skill: skill.name,
            )
        )

    def read_skill(self, name: str) -> str:
        """Return one Skill body without its optional YAML frontmatter."""

        normalized_name = _validate_skill_name(name)
        skill = next(
            (candidate for candidate in self.list_skills() if candidate.name == normalized_name),
            None,
        )
        if skill is None:
            raise SkillNotFoundError(f"Skill was not found: {normalized_name}")

        try:
            content = skill.path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SkillNotFoundError(f"Skill file was not found: {normalized_name}") from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise SkillLoadError(f"Skill could not be read: {normalized_name}") from exc
        return _split_frontmatter(content, fallback_name=skill.name)[1]

    def find_referenced_skills(self, content: str) -> tuple[SkillInfo, ...]:
        """Return known ``$skill-name`` references in first-occurrence order."""

        if not isinstance(content, str):
            raise TypeError("Skill reference content must be a string")

        skills_by_name = {skill.name: skill for skill in self.list_skills()}
        references: list[SkillInfo] = []
        seen_names: set[str] = set()
        for match in _SKILL_REFERENCE_PATTERN.finditer(content):
            name = match.group("name")
            if name in seen_names:
                continue
            skill = skills_by_name.get(name)
            if skill is None:
                continue
            seen_names.add(name)
            references.append(skill)
        return tuple(references)

    def _scan_directory(self, directory: Path) -> tuple[SkillInfo, ...]:
        try:
            skill_directories = sorted(
                (path for path in directory.iterdir() if path.is_dir()),
                key=lambda path: path.name,
            )
        except FileNotFoundError:
            return ()
        except OSError:
            logger.warning("Skipping unreadable Skill directory")
            return ()

        skills: list[SkillInfo] = []
        for skill_directory in skill_directories:
            path = skill_directory / _SKILL_FILENAME
            try:
                content = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except (OSError, UnicodeDecodeError):
                logger.warning("Skipping unreadable Skill (name=%s)", skill_directory.name)
                continue

            metadata, _ = _split_frontmatter(content, fallback_name=skill_directory.name)
            required_bins = _metadata_values(metadata, "requires_bins")
            required_env = _metadata_values(metadata, "requires_env")
            skills.append(
                SkillInfo(
                    name=_metadata_text(metadata, "name", skill_directory.name),
                    description=_metadata_text(metadata, "description", ""),
                    path=path,
                    always=metadata.get("always") is True,
                    **_availability_fields(required_bins, required_env),
                )
            )
        return tuple(skills)


def _split_frontmatter(
    content: str,
    *,
    fallback_name: str,
) -> tuple[dict[str, _SkillMetadataValue], str]:
    """Return simple YAML metadata and the Markdown body without raising on errors."""

    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return {}, content

    closing_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == _FRONTMATTER_DELIMITER
        ),
        None,
    )
    if closing_index is None:
        logger.warning("Ignoring invalid Skill frontmatter (name=%s)", fallback_name)
        return {}, content

    metadata = _parse_frontmatter(lines[1:closing_index], fallback_name)
    return metadata, "".join(lines[closing_index + 1 :])


def _parse_frontmatter(
    lines: list[str],
    fallback_name: str,
) -> dict[str, _SkillMetadataValue]:
    metadata: dict[str, _SkillMetadataValue] = {}
    inline_metadata = _parse_inline_object("".join(lines).strip())
    if inline_metadata is not None:
        _parse_metadata_mapping(metadata, (), inline_metadata)
        return metadata

    parent_paths: list[tuple[int, tuple[str, ...]]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indentation = len(line) - len(line.lstrip())
        if stripped.startswith("- "):
            if parent_paths:
                _append_requirement_value(
                    metadata,
                    parent_paths[-1][1],
                    stripped[2:].strip(),
                )
            continue
        key, separator, value = stripped.partition(":")
        if not separator or not key.strip():
            logger.warning("Ignoring invalid Skill frontmatter (name=%s)", fallback_name)
            return {}
        normalized_key = key.strip()
        normalized_value = value.strip().strip("\"'")

        while parent_paths and indentation <= parent_paths[-1][0]:
            parent_paths.pop()
        parent_path = parent_paths[-1][1] if parent_paths else ()
        path = (*parent_path, *normalized_key.split("."))
        if not normalized_value:
            parent_paths.append((indentation, path))
            continue
        _parse_metadata_value(metadata, path, normalized_value)
    return metadata


def _metadata_text(
    metadata: dict[str, _SkillMetadataValue],
    name: str,
    fallback: str,
) -> str:
    value = metadata.get(name)
    return value if isinstance(value, str) else fallback


def _parse_bool(value: str) -> bool | None:
    normalized_value = value.lower()
    if normalized_value == "true":
        return True
    if normalized_value == "false":
        return False
    return None


def _parse_metadata_value(
    metadata: dict[str, _SkillMetadataValue],
    path: tuple[str, ...],
    value: str,
) -> None:
    inline_object = _parse_inline_object(value)
    if inline_object is not None:
        _parse_metadata_mapping(metadata, path, inline_object)
        return
    if path in {("name",), ("description",)}:
        metadata[path[0]] = value
        return
    if path in {("always",), ("metadata", "nanobot", "always")}:
        always = _parse_bool(value)
        if always is not None:
            metadata["always"] = always
        return
    _set_requirement_values(metadata, path, value)


def _parse_inline_object(value: str) -> Mapping[object, object] | None:
    """Return a JSON-compatible inline mapping without raising on malformed input."""

    if not value.startswith("{"):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _parse_metadata_mapping(
    metadata: dict[str, _SkillMetadataValue],
    parent_path: tuple[str, ...],
    values: Mapping[object, object],
) -> None:
    """Extract supported metadata from a JSON-compatible inline mapping."""

    for raw_key, value in values.items():
        if not isinstance(raw_key, str):
            continue
        path = (*parent_path, *raw_key.split("."))
        if isinstance(value, Mapping):
            _parse_metadata_mapping(metadata, path, value)
        elif isinstance(value, bool):
            _parse_metadata_value(metadata, path, str(value).lower())
        elif isinstance(value, str):
            _parse_metadata_value(metadata, path, value)
        elif isinstance(value, list):
            _set_requirement_items(metadata, path, value)


def _set_requirement_items(
    metadata: dict[str, _SkillMetadataValue],
    path: tuple[str, ...],
    values: Iterable[object],
) -> None:
    key = _requirement_metadata_key(path)
    if key is None:
        return
    valid_values = tuple(
        value
        for value in values
        if isinstance(value, str) and _is_valid_requirement_value(key, value)
    )
    metadata[key] = _unique_values((*_metadata_values(metadata, key), *valid_values))


def _append_requirement_value(
    metadata: dict[str, _SkillMetadataValue],
    path: tuple[str, ...],
    value: str,
) -> None:
    key = _requirement_metadata_key(path)
    if key is None:
        return
    normalized_value = value.strip().strip("\"'")
    if not _is_valid_requirement_value(key, normalized_value):
        return
    metadata[key] = _unique_values((*_metadata_values(metadata, key), normalized_value))


def _set_requirement_values(
    metadata: dict[str, _SkillMetadataValue],
    path: tuple[str, ...],
    value: str,
) -> None:
    key = _requirement_metadata_key(path)
    if key is None:
        return
    values = _parse_requirement_values(value)
    if values is None:
        return
    metadata[key] = _unique_values(
        requirement
        for requirement in values
        if _is_valid_requirement_value(key, requirement)
    )


def _requirement_metadata_key(path: tuple[str, ...]) -> str | None:
    if path[:1] == ("metadata",):
        path = path[1:]
    if path == ("nanobot", "requires", "bins"):
        return "requires_bins"
    if path == ("nanobot", "requires", "env"):
        return "requires_env"
    return None


def _parse_requirement_values(value: str) -> tuple[str, ...] | None:
    if value.startswith("["):
        if not value.endswith("]"):
            return None
        values = (item.strip().strip("\"'") for item in value[1:-1].split(","))
    else:
        values = (value.strip().strip("\"'"),)
    return _unique_values(item for item in values if item)


def _is_valid_requirement_value(key: str, value: str) -> bool:
    pattern = _BIN_NAME_PATTERN if key == "requires_bins" else _ENV_NAME_PATTERN
    return bool(pattern.fullmatch(value))


def _metadata_values(
    metadata: dict[str, _SkillMetadataValue],
    key: str,
) -> tuple[str, ...]:
    value = metadata.get(key)
    return value if isinstance(value, tuple) else ()


def _unique_values(values: Iterable[str]) -> tuple[str, ...]:
    unique_values: list[str] = []
    for value in values:
        if value not in unique_values:
            unique_values.append(value)
    return tuple(unique_values)


def _availability_fields(
    required_bins: tuple[str, ...],
    required_env: tuple[str, ...],
) -> dict[str, bool | tuple[str, ...]]:
    missing_dependencies = tuple(
        f"bin: {name}" for name in required_bins if shutil.which(name) is None
    ) + tuple(
        f"env: {name}" for name in required_env if not os.environ.get(name, "").strip()
    )
    return {
        "is_available": not missing_dependencies,
        "missing_dependencies": missing_dependencies,
    }


def _validate_skill_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("Skill name must be a string")
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("Skill name must not be blank")
    return normalized_name
