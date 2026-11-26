"""Structured data models for APA Skills Registry (Phase 4.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional


class SkillSource(str, Enum):
    """Source origin and tier of an installed or discovered skill."""

    PROJECT = "project"
    LOCAL = "local"
    CREATE_DIR = "create_dir"
    EXTERNAL = "external"
    BUNDLED = "bundled"
    PLUGIN = "plugin"


class ValidationStatus(str, Enum):
    """Validation and availability status of a skill entry."""

    VALID = "valid"
    INVALID = "invalid"
    QUARANTINED = "quarantined"
    SHADOWED = "shadowed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class SkillEntry:
    """Immutable structured record representing a single discovered or registered skill."""

    name: str
    load_name: Optional[str]
    description: str
    version: Optional[str] = None
    author: Optional[str] = None
    source: SkillSource = SkillSource.LOCAL
    tier: int = 1
    root: Path = field(default_factory=Path)
    path: Path = field(default_factory=Path)  # Path to SKILL.md
    skill_dir: Path = field(default_factory=Path)  # Parent directory of SKILL.md
    category: Optional[str] = None
    enabled: bool = True
    platform_compatible: bool = True
    validation_status: ValidationStatus = ValidationStatus.VALID
    validation_error: Optional[str] = None
    tags: tuple[str, ...] = ()
    requires_tools: tuple[str, ...] = ()
    last_scanned: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_loadable(self) -> bool:
        """True if the skill can be loaded by skill_view and offered to the agent."""
        return (
            self.enabled
            and self.platform_compatible
            and self.validation_status in (ValidationStatus.VALID, ValidationStatus.AMBIGUOUS)
            and bool(self.load_name)
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert entry to a JSON-serializable dictionary."""
        return {
            "name": self.name,
            "load_name": self.load_name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "source": self.source.value,
            "tier": self.tier,
            "root": str(self.root),
            "path": str(self.path),
            "skill_dir": str(self.skill_dir),
            "category": self.category,
            "enabled": self.enabled,
            "platform_compatible": self.platform_compatible,
            "validation_status": self.validation_status.value,
            "validation_error": self.validation_error,
            "tags": list(self.tags),
            "requires_tools": list(self.requires_tools),
            "last_scanned": self.last_scanned,
            "is_loadable": self.is_loadable,
        }


@dataclass(frozen=True)
class RegistryStatus:
    """Snapshot status of the APA Skills Registry."""

    total_skills: int
    enabled_count: int
    disabled_count: int
    duplicate_count: int
    shadowed_count: int
    ambiguous_count: int
    sources_breakdown: Mapping[str, int]
    last_scanned: float

    def to_dict(self) -> dict[str, Any]:
        """Convert status snapshot to dictionary."""
        return {
            "total_skills": self.total_skills,
            "enabled_count": self.enabled_count,
            "disabled_count": self.disabled_count,
            "duplicate_count": self.duplicate_count,
            "shadowed_count": self.shadowed_count,
            "ambiguous_count": self.ambiguous_count,
            "sources_breakdown": dict(self.sources_breakdown),
            "last_scanned": self.last_scanned,
        }


# --- Phase 4.3 Models & Validation Types ---

class ValidationLevel(str, Enum):
    """Validation severity outcome for a skill."""

    VALID = "valid"
    WARNING = "warning"
    INVALID = "invalid"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ValidationFinding:
    """A specific issue or observation discovered during skill validation."""

    code: str
    message: str
    level: ValidationLevel = ValidationLevel.WARNING
    path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "level": self.level.value,
            "path": self.path,
        }


@dataclass(frozen=True)
class ValidationReport:
    """Comprehensive validation outcome for a skill directory or manifest."""

    skill_name: str
    skill_dir: Path
    level: ValidationLevel = ValidationLevel.VALID
    findings: tuple[ValidationFinding, ...] = ()
    is_safe_to_load: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "skill_dir": str(self.skill_dir),
            "level": self.level.value,
            "findings": [f.to_dict() for f in self.findings],
            "is_safe_to_load": self.is_safe_to_load,
        }


@dataclass(frozen=True)
class LoadedFile:
    """A supporting or reference file read from within a skill's directory."""

    skill_name: str
    relative_path: str
    absolute_path: Path
    content: str
    is_binary: bool = False
    size_bytes: int = 0
    file_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "relative_path": self.relative_path,
            "absolute_path": str(self.absolute_path),
            "content": self.content if not self.is_binary else f"[Binary file: {self.size_bytes} bytes]",
            "is_binary": self.is_binary,
            "size_bytes": self.size_bytes,
            "file_type": self.file_type,
        }


@dataclass(frozen=True)
class LoadedSkill:
    """A fully resolved skill with instructions and linked files loaded into memory on demand."""

    name: str
    load_name: Optional[str]
    description: str
    skill_dir: Path
    skill_md_path: Path
    frontmatter: Mapping[str, Any]
    instructions: str
    linked_files: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    source: SkillSource = SkillSource.LOCAL
    tier: int = 1
    loaded_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "load_name": self.load_name,
            "description": self.description,
            "skill_dir": str(self.skill_dir),
            "skill_md_path": str(self.skill_md_path),
            "frontmatter": dict(self.frontmatter),
            "instructions": self.instructions,
            "linked_files": {k: list(v) for k, v in self.linked_files.items()},
            "source": self.source.value,
            "tier": self.tier,
            "loaded_at": self.loaded_at,
        }


# --- Exceptions ---

class SkillError(Exception):
    """Base exception for all APA skill operations."""


class SkillNotFoundError(SkillError):
    """Raised when a requested skill cannot be found across any search root."""


class AmbiguousSkillError(SkillError):
    """Raised when an unqualified skill name matches multiple same-tier skills."""


class SkillLoadError(SkillError):
    """Raised when reading or parsing a skill fails."""


class SkillValidationError(SkillError):
    """Raised when a skill fails strict structural or safety validation."""


class SkillSecurityError(SkillError):
    """Raised when a skill attempts a path traversal, symlink escape, or security violation."""

