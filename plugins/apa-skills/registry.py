"""Centralized APA Skills Registry orchestrating discovery, metadata, and status inspection (Phase 4.2).

Reuses existing Hermes Agent catalog resolution and progressive disclosure mechanisms
without duplicating underlying file scanners or breaking prompt cache invariants.
"""

from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

from hermes_constants import get_skills_dir

from .models import RegistryStatus, SkillEntry, SkillSource, ValidationStatus

if TYPE_CHECKING:
    import agent.skill_utils
    import hermes_cli.plugins
    import tools.skills_tool

logger = logging.getLogger(__name__)

_NT_DEVICE_PREFIX_RE = re.compile(
    r"^(?:\\\\\.\\|\\\\\?\\(?:UNC|GLOBALROOT|Volume|PhysicalDrive)|\\\?\?\\)",
    re.IGNORECASE,
)
_MAX_METADATA_READ_BYTES = 8192

# Constants mirrored from Hermes skill_utils
AMBIGUOUS_SKILL_PREFIX = "[ambiguous: "
EXCLUDED_SKILL_DIRS = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
})
TIER_PROJECT = 0
TIER_CREATE_DIR = 1
TIER_LOCAL = 2
TIER_EXTERNAL = 3


def _skill_utils():
    """Dynamically resolve Hermes skill_utils to maintain plugin boundary isolation."""
    return importlib.import_module("agent.skill_utils")


def _hermes_plugins():
    """Dynamically resolve Hermes plugins subsystem."""
    return importlib.import_module("hermes_cli.plugins")


def _skills_tool():
    """Dynamically resolve tools.skills_tool for cache management."""
    return importlib.import_module("tools.skills_tool")


def extract_skill_conditions(meta: Mapping[str, Any]) -> dict[str, Any]:
    """Extract compatibility and tool requirement conditions from skill metadata."""
    return _skill_utils().extract_skill_conditions(meta)


def get_disabled_skill_names() -> set[str]:
    """Retrieve set of disabled skill names from Hermes configuration."""
    return _skill_utils().get_disabled_skill_names()


def get_skill_search_roots(
    profile_skills_dir: Optional[Path] = None, *, include_project: bool = True
) -> list[tuple[int, Path]]:
    """Retrieve ordered tier-tagged search roots from Hermes."""
    return _skill_utils().get_skill_search_roots(profile_skills_dir, include_project=include_project)


def is_quarantined_project_skill(path: Path) -> bool:
    """Check if a project-level skill is quarantined by Hermes security."""
    return _skill_utils().is_quarantined_project_skill(path)


def iter_project_skill_files(root: Path):
    """Iterate over skill files in project root."""
    return _skill_utils().iter_project_skill_files(root)


def iter_skill_index_files(root: Path, filename: str = "SKILL.md"):
    """Iterate over index skill files in root matching filename."""
    return _skill_utils().iter_skill_index_files(root, filename)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter and body from markdown content."""
    return _skill_utils().parse_frontmatter(text)


def resolve_skill_catalog(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve raw skills across roots applying Hermes precedence rules."""
    return _skill_utils().resolve_skill_catalog(entries)


def skill_matches_platform(frontmatter: Mapping[str, Any]) -> bool:
    """Check if conditions are compatible with target platform."""
    return _skill_utils().skill_matches_platform(frontmatter)


def discover_plugins() -> None:
    """Discover all plugins registered with Hermes."""
    _hermes_plugins().discover_plugins()


def get_plugin_manager():
    """Get the Hermes PluginManager singleton."""
    return _hermes_plugins().get_plugin_manager()


def clear_skills_cache() -> None:
    """Clear Hermes tool-level skills cache."""
    clear_fn = getattr(_skills_tool(), "clear_skills_cache", None)
    if callable(clear_fn):
        clear_fn()


def _classify_source(tier: int, root: Path, bundled_root: Optional[Path] = None) -> SkillSource:
    """Map a search root and tier to a structured :class:`SkillSource`."""
    if tier == TIER_PROJECT:
        return SkillSource.PROJECT
    if tier == TIER_CREATE_DIR:
        return SkillSource.CREATE_DIR
    if tier == TIER_EXTERNAL:
        return SkillSource.EXTERNAL
    if tier == TIER_LOCAL:
        if bundled_root is not None:
            try:
                if root.resolve() == bundled_root.resolve():
                    return SkillSource.BUNDLED
            except OSError:
                pass
        return SkillSource.LOCAL
    return SkillSource.LOCAL


def _extract_category(skill_md: Path, root: Path) -> Optional[str]:
    """Extract category name from relative directory depth, e.g. `<root>/<category>/<skill>/SKILL.md`."""
    try:
        rel_parts = skill_md.relative_to(root).parts
        if len(rel_parts) >= 3:
            return rel_parts[0]
    except (ValueError, OSError):
        pass
    return None


class APASkillsRegistry:
    """Thread-safe orchestration registry for APA skills discovery, state tracking, and inspection."""

    def __init__(self, *, custom_roots: Optional[Sequence[tuple[int, Path]]] = None) -> None:
        self._lock = threading.RLock()
        self._custom_roots = list(custom_roots) if custom_roots is not None else None
        self._entries: list[SkillEntry] = []
        self._entries_by_name: dict[str, list[SkillEntry]] = {}
        self._entries_by_load_name: dict[str, SkillEntry] = {}
        self._last_refresh: float = 0.0

    @property
    def last_refresh(self) -> float:
        """Timestamp of last successful discovery/refresh."""
        with self._lock:
            return self._last_refresh

    def _get_active_roots(self) -> list[tuple[int, Path]]:
        """Return search roots in precedence order."""
        if self._custom_roots is not None:
            return list(self._custom_roots)
        return get_skill_search_roots()

    def get_active_roots(self) -> list[tuple[int, Path]]:
        """Return search roots in precedence order."""
        return self._get_active_roots()

    def validate_path_safety(self, path: Path | str, *, allowed_roots: Optional[Sequence[Path]] = None) -> Path:
        """Validate that a skill path is canonical, safe, and within approved search roots.

        Raises:
            ValueError: If path contains traversal sequences, device namespaces, or escapes allowed roots.
        """
        raw_str = str(path).strip()
        if not raw_str:
            raise ValueError("Skill path cannot be empty.")
        if "\x00" in raw_str:
            raise ValueError("Null byte detected in skill path.")
        if _NT_DEVICE_PREFIX_RE.match(raw_str):
            raise ValueError(f"Blocked Windows NT device namespace path: {raw_str}")

        pure_posix = PurePosixPath(raw_str)
        pure_win = PureWindowsPath(raw_str)
        if pure_win.drive and len(pure_win.parts) == 1:
            raise ValueError(f"Bare Windows drive path is not allowed: {raw_str}")

        # Check for path traversal components
        if any(part == ".." for part in pure_posix.parts) or any(part == ".." for part in pure_win.parts):
            raise ValueError(f"Path traversal sequence '..' detected in skill path: {raw_str}")

        try:
            resolved = Path(raw_str).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Failed to resolve skill path: {exc}") from exc

        roots_to_check = (
            [r.resolve() for r in allowed_roots]
            if allowed_roots is not None
            else [r.resolve() for _t, r in self._get_active_roots() if r.exists()]
        )

        if roots_to_check and not any(resolved.is_relative_to(r) for r in roots_to_check):
            raise ValueError(
                f"Skill path '{resolved}' escapes all approved skill roots: {[str(r) for r in roots_to_check]}"
            )

        return resolved

    def _scan_raw_skills(self, roots: list[tuple[int, Path]]) -> list[dict[str, Any]]:
        """Walk roots and extract raw frontmatter and path entries."""
        raw_scanned: list[dict[str, Any]] = []

        for tier, search_dir in roots:
            if not search_dir.is_dir():
                continue

            file_iterator = (
                iter_project_skill_files(search_dir)
                if tier == TIER_PROJECT
                else iter_skill_index_files(search_dir)
            )

            for skill_md in file_iterator:
                if any(part in EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                    continue

                frontmatter: dict[str, Any] = {}
                validation_error: Optional[str] = None
                status = ValidationStatus.VALID

                try:
                    raw_text = skill_md.read_text(encoding="utf-8-sig")[:_MAX_METADATA_READ_BYTES]
                    frontmatter, _body = parse_frontmatter(raw_text)
                    if not frontmatter:
                        validation_error = "Frontmatter is empty or malformed."
                        status = ValidationStatus.INVALID
                except (UnicodeDecodeError, PermissionError, OSError) as exc:
                    validation_error = f"Failed to read skill file: {exc}"
                    status = ValidationStatus.INVALID

                skill_dir = skill_md.parent
                declared_name = str(frontmatter.get("name") or skill_dir.name).strip()
                if not declared_name:
                    declared_name = skill_dir.name

                raw_scanned.append({
                    "name": declared_name,
                    "frontmatter": frontmatter,
                    "tier": tier,
                    "root": search_dir,
                    "path": skill_md,
                    "skill_dir": skill_dir,
                    "status_override": status,
                    "validation_error": validation_error,
                })

        return raw_scanned

    def _discover_plugin_skills(self) -> list[SkillEntry]:
        """Query Hermes PluginManager for any dynamically registered plugin skills."""
        entries: list[SkillEntry] = []
        try:
            discover_plugins()
            pm = get_plugin_manager()
            now = time.time()
            disabled_names = get_disabled_skill_names()

            for p_skill in pm.list_plugin_skill_metadata():
                name = str(p_skill.get("name", "")).strip()
                if not name:
                    continue

                frontmatter = p_skill.get("frontmatter") or {}
                desc = str(p_skill.get("description") or frontmatter.get("description") or "").strip()
                cat = p_skill.get("category")
                path_obj = Path(p_skill.get("path", ""))

                entry = SkillEntry(
                    name=name,
                    load_name=name,
                    description=desc,
                    version=str(frontmatter.get("version")) if frontmatter.get("version") else None,
                    author=str(frontmatter.get("author")) if frontmatter.get("author") else None,
                    source=SkillSource.PLUGIN,
                    tier=1,
                    root=path_obj.parent if path_obj else Path(),
                    path=path_obj,
                    skill_dir=path_obj.parent if path_obj else Path(),
                    category=cat,
                    enabled=name not in disabled_names,
                    platform_compatible=skill_matches_platform(frontmatter),
                    validation_status=ValidationStatus.VALID,
                    validation_error=None,
                    tags=tuple(frontmatter.get("tags") or ()),
                    requires_tools=tuple(extract_skill_conditions(frontmatter).get("requires_tools", ())),
                    last_scanned=now,
                    metadata=frontmatter,
                )
                entries.append(entry)
        except Exception as exc:  # health: allow BLE001 -- graceful degradation if plugin manager fails
            logger.debug("Plugin skill discovery skipped: %s", exc)

        return entries

    def discover(self, force: bool = False) -> list[SkillEntry]:
        """Discover all skills across configured search roots and plugins.

        Thread-safe; caches results until :meth:`refresh` is called or *force* is True.
        """
        with self._lock:
            if self._entries and not force:
                return list(self._entries)

            roots = self._get_active_roots()
            raw_entries = self._scan_raw_skills(roots)

            # Re-use Hermes resolve_skill_catalog for cross-tier precedence & ambiguity resolution
            resolved_catalog = resolve_skill_catalog([
                {
                    "name": e["name"],
                    "tier": e["tier"],
                    "root": e["root"],
                    "path": e["path"],
                }
                for e in raw_entries
            ])

            disabled_names = get_disabled_skill_names()
            bundled_root = get_skills_dir()
            now = time.time()
            discovered: list[SkillEntry] = []

            for raw_item, resolved_item in zip(raw_entries, resolved_catalog):
                name = raw_item["name"]
                tier = raw_item["tier"]
                root = raw_item["root"]
                skill_md = raw_item["path"]
                skill_dir = raw_item["skill_dir"]
                frontmatter = raw_item["frontmatter"]

                hermes_status = resolved_item.get("status", "unique")
                load_name = resolved_item.get("load_name")

                status = raw_item["status_override"]
                val_error = raw_item["validation_error"]

                if status == ValidationStatus.VALID:
                    if tier == TIER_PROJECT and is_quarantined_project_skill(skill_md):
                        status = ValidationStatus.QUARANTINED
                        val_error = "Security scan quarantined this project skill as dangerous."
                        load_name = None
                    elif hermes_status == "shadowed":
                        status = ValidationStatus.SHADOWED
                    elif hermes_status == "ambiguous":
                        status = ValidationStatus.AMBIGUOUS

                category = _extract_category(skill_md, root)
                desc = str(frontmatter.get("description", "")).strip()
                if not desc:
                    desc = f"Skill {name}"

                source = _classify_source(tier, root, bundled_root)
                is_enabled = name not in disabled_names and (load_name is None or load_name not in disabled_names)
                is_compat = skill_matches_platform(frontmatter)

                conditions = extract_skill_conditions(frontmatter)
                requires_tools = tuple(str(t) for t in conditions.get("requires_tools", ()))
                tags = tuple(str(t) for t in (frontmatter.get("tags") or ()))

                discovered.append(
                    SkillEntry(
                        name=name,
                        load_name=load_name,
                        description=desc,
                        version=str(frontmatter.get("version")) if frontmatter.get("version") else None,
                        author=str(frontmatter.get("author")) if frontmatter.get("author") else None,
                        source=source,
                        tier=tier,
                        root=root,
                        path=skill_md,
                        skill_dir=skill_dir,
                        category=category,
                        enabled=is_enabled,
                        platform_compatible=is_compat,
                        validation_status=status,
                        validation_error=val_error,
                        tags=tags,
                        requires_tools=requires_tools,
                        last_scanned=now,
                        metadata=frontmatter,
                    )
                )

            # Query plugins for any contributed skills
            discovered.extend(self._discover_plugin_skills())

            self._entries = discovered
            self._rebuild_indices_locked()
            self._last_refresh = now
            return list(self._entries)

    def _rebuild_indices_locked(self) -> None:
        """Rebuild quick-lookup dictionary indices. Must be called while holding ``_lock``."""
        self._entries_by_name.clear()
        self._entries_by_load_name.clear()

        for entry in self._entries:
            self._entries_by_name.setdefault(entry.name, []).append(entry)
            if entry.load_name:
                self._entries_by_load_name[entry.load_name] = entry

    def refresh(self) -> list[SkillEntry]:
        """Force a fresh scan of skills, updating caches across APA and Hermes."""
        clear_skills_cache()
        return self.discover(force=True)

    def get_skill(self, identifier: str) -> Optional[SkillEntry]:
        """Look up a skill by bare name, qualified name, or unique load path.

        Refuses ambiguous same-tier collisions according to Hermes rules.
        """
        raw = (identifier or "").strip()
        if not raw:
            return None

        with self._lock:
            if not self._entries:
                self.discover()

            # 1. Exact match on unique load_name (e.g. "software-development/github" or "github")
            if raw in self._entries_by_load_name:
                return self._entries_by_load_name[raw]

            # 2. Match on declared name
            candidates = self._entries_by_name.get(raw, [])
            if not candidates:
                return None

            # Filter candidates that are valid/loadable
            loadable = [c for c in candidates if c.is_loadable]
            if len(loadable) == 1:
                return loadable[0]

            if len(loadable) > 1:
                # Same-tier collision refusal
                logger.warning(
                    "%s'%s': %d candidates share this name across roots; pass explicit load_name",
                    AMBIGUOUS_SKILL_PREFIX,
                    raw,
                    len(loadable),
                )
                return None

            # Fallback to single raw candidate even if not loadable (for info/inspection)
            if len(candidates) == 1:
                return candidates[0]

            return None

    def list_skills(
        self,
        *,
        category: Optional[str] = None,
        enabled_only: bool = False,
        source: Optional[SkillSource | str] = None,
        platform_compatible_only: bool = False,
    ) -> list[SkillEntry]:
        """Return a filtered list of discovered skills."""
        with self._lock:
            if not self._entries:
                self.discover()

            results = list(self._entries)

            if category is not None:
                cat_lower = category.strip().lower()
                results = [s for s in results if (s.category or "").lower() == cat_lower]

            if enabled_only:
                results = [s for s in results if s.enabled]

            if platform_compatible_only:
                results = [s for s in results if s.platform_compatible]

            if source is not None:
                src_val = source.value if isinstance(source, SkillSource) else str(source).lower().strip()
                results = [s for s in results if s.source.value == src_val]

            return sorted(results, key=lambda s: (s.category or "", s.name))

    def find_duplicates(self) -> dict[str, list[SkillEntry]]:
        """Identify all skills that have identical declared names in multiple roots."""
        with self._lock:
            if not self._entries:
                self.discover()

            return {
                name: entries
                for name, entries in self._entries_by_name.items()
                if len(entries) > 1
            }

    def get_status(self) -> RegistryStatus:
        """Return an aggregate status summary of the skills registry."""
        with self._lock:
            if not self._entries:
                self.discover()

            total = len(self._entries)
            enabled = sum(1 for s in self._entries if s.enabled)
            disabled = total - enabled
            shadowed = sum(1 for s in self._entries if s.validation_status == ValidationStatus.SHADOWED)
            ambiguous = sum(1 for s in self._entries if s.validation_status == ValidationStatus.AMBIGUOUS)

            duplicates_dict = self.find_duplicates()
            dup_count = sum(len(entries) for entries in duplicates_dict.values())

            sources: dict[str, int] = {}
            for s in self._entries:
                sources[s.source.value] = sources.get(s.source.value, 0) + 1

            return RegistryStatus(
                total_skills=total,
                enabled_count=enabled,
                disabled_count=disabled,
                duplicate_count=dup_count,
                shadowed_count=shadowed,
                ambiguous_count=ambiguous,
                sources_breakdown=sources,
                last_scanned=self._last_refresh,
            )


_registry_instance: Optional[APASkillsRegistry] = None
_registry_lock = threading.Lock()


def get_skills_registry() -> APASkillsRegistry:
    """Return the global process-wide :class:`APASkillsRegistry` instance."""
    global _registry_instance
    with _registry_lock:
        if _registry_instance is None:
            _registry_instance = APASkillsRegistry()
        return _registry_instance


def reset_skills_registry() -> None:
    """Reset the global registry instance (primarily used for unit testing)."""
    global _registry_instance
    with _registry_lock:
        _registry_instance = None
