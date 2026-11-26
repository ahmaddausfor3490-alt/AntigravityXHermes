"""Secure, on-demand skill loader for APA (Phase 4.3).

Implements Progressive Disclosure Phase 2 by loading full instructions and
linked supporting references strictly on explicit request.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping, Optional

from .models import (
    AmbiguousSkillError,
    LoadedFile,
    LoadedSkill,
    SkillEntry,
    SkillLoadError,
    SkillNotFoundError,
    SkillSecurityError,
    ValidationStatus,
)
from .registry import APASkillsRegistry, get_skills_registry, parse_frontmatter
from .security import (
    is_sensitive_file,
    record_skill_security_audit,
    validate_skill_path_safety,
)

logger = logging.getLogger(__name__)

_SUPPORTED_SUBDIRS = ("references", "templates", "assets", "scripts")


class SkillLoader:
    """Thread-safe, on-demand loader for skills and their supporting files."""

    def __init__(self, registry: Optional[APASkillsRegistry] = None) -> None:
        self._registry = registry or get_skills_registry()
        self._lock = threading.RLock()
        self._cache: dict[str, LoadedSkill] = {}

    @property
    def registry(self) -> APASkillsRegistry:
        return self._registry

    def clear_cache(self) -> None:
        """Clear all in-memory loaded skill instruction caches."""
        with self._lock:
            self._cache.clear()

    def load_skill(
        self,
        name_or_identifier: str,
        *,
        force_reload: bool = False,
    ) -> LoadedSkill:
        """Load a skill's full instructions and discover its linked supporting files.

        Progressive Disclosure Phase 2: This is only invoked when an agent or user
        explicitly requests the full skill content.
        """
        raw_name = (name_or_identifier or "").strip()
        if not raw_name:
            raise SkillNotFoundError("Skill identifier cannot be empty.")

        # Defense-in-depth: Reject traversal or device prefixes in the identifier itself
        validate_skill_path_safety(raw_name, allow_absolute=False)

        with self._lock:
            cache_key = raw_name.lower()
            if not force_reload and cache_key in self._cache:
                return self._cache[cache_key]

            entry = self._registry.get_skill(raw_name)

            if entry is None:
                # Check if this was refused due to an ambiguous same-tier collision
                dups = self._registry.find_duplicates()
                if raw_name in dups:
                    contenders = [e.load_name or str(e.path) for e in dups[raw_name]]
                    record_skill_security_audit(
                        action="load_skill",
                        target=raw_name,
                        decision="denied",
                        reason="Ambiguous name collision in same tier",
                        details={"contenders": contenders},
                    )
                    raise AmbiguousSkillError(
                        f"Skill name '{raw_name}' is ambiguous across multiple locations: {contenders}. "
                        "Specify the qualified load path (e.g. 'category/name')."
                    )

                record_skill_security_audit(
                    action="load_skill",
                    target=raw_name,
                    decision="denied",
                    reason="Skill not found",
                )
                raise SkillNotFoundError(f"Skill '{raw_name}' not found in any configured search root.")

            # Validate security and status constraints
            if entry.validation_status == ValidationStatus.QUARANTINED:
                record_skill_security_audit(
                    action="load_skill",
                    target=entry.name,
                    decision="denied",
                    reason="Skill quarantined by security scanner",
                    details={"path": str(entry.path)},
                )
                raise SkillSecurityError(
                    f"Skill '{entry.name}' is quarantined and blocked from execution: {entry.validation_error}"
                )

            # Ensure SKILL.md is safely contained inside the registered root
            validate_skill_path_safety(entry.path, approved_root=entry.root, allow_absolute=True)

            if not entry.path.is_file():
                record_skill_security_audit(
                    action="load_skill",
                    target=entry.name,
                    decision="denied",
                    reason="SKILL.md does not exist on disk",
                )
                raise SkillNotFoundError(f"Skill index file '{entry.path}' does not exist on disk.")

            try:
                raw_text = entry.path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise SkillLoadError(f"SKILL.md at '{entry.path}' is not valid UTF-8: {exc}") from exc
            except OSError as exc:
                raise SkillLoadError(f"Failed to read SKILL.md at '{entry.path}': {exc}") from exc

            # Parse frontmatter and extract instructions body
            frontmatter, instructions = parse_frontmatter(raw_text)

            # Scan linked supporting files
            linked_files = self._scan_linked_files(entry.skill_dir)

            loaded = LoadedSkill(
                name=entry.name,
                load_name=entry.load_name,
                description=entry.description,
                skill_dir=entry.skill_dir,
                skill_md_path=entry.path,
                frontmatter=frontmatter,
                instructions=instructions,
                linked_files=linked_files,
                source=entry.source,
                tier=entry.tier,
                loaded_at=time.time(),
            )

            # Cache by both declared name and qualified load_name
            self._cache[entry.name.lower()] = loaded
            if entry.load_name:
                self._cache[entry.load_name.lower()] = loaded

            record_skill_security_audit(
                action="load_skill",
                target=entry.name,
                decision="allowed",
                reason="Skill loaded successfully on demand",
                details={"tier": entry.tier, "source": entry.source.value},
            )
            return loaded

    def reload_skill(self, name_or_identifier: str) -> LoadedSkill:
        """Force a fresh disk read and update of the skill in registry and cache."""
        with self._lock:
            # Drop from local cache
            raw_key = (name_or_identifier or "").strip().lower()
            self._cache.pop(raw_key, None)

            # Refresh registry to pick up directory edits
            self._registry.refresh()
            return self.load_skill(name_or_identifier, force_reload=True)

    def load_linked_file(
        self,
        skill_name_or_identifier: str,
        relative_file_path: str,
    ) -> LoadedFile:
        """Load a linked supporting reference or template file from within a skill's directory."""
        clean_rel = (relative_file_path or "").strip()
        if not clean_rel:
            raise SkillNotFoundError("Relative file path cannot be empty.")

        # 1. Path safety check on the requested relative path
        validate_skill_path_safety(clean_rel, allow_absolute=False)

        # 2. Lookup owning skill
        skill = self.load_skill(skill_name_or_identifier)

        # 3. Construct and validate absolute target path
        target_path = skill.skill_dir / clean_rel

        # Verify that target_path stays strictly inside skill.skill_dir (prevents symlink/junction escape)
        validate_skill_path_safety(target_path, approved_root=skill.skill_dir, allow_absolute=True)

        if not target_path.exists():
            record_skill_security_audit(
                action="load_linked_file",
                target=clean_rel,
                decision="denied",
                reason="Linked file not found",
                details={"skill": skill.name},
            )
            raise SkillNotFoundError(f"File '{clean_rel}' not found in skill '{skill.name}'.")

        if not target_path.is_file():
            raise SkillLoadError(f"Target '{clean_rel}' in skill '{skill.name}' is a directory, not a file.")

        # 4. Check for sensitive files
        if is_sensitive_file(target_path):
            record_skill_security_audit(
                action="load_linked_file",
                target=clean_rel,
                decision="denied",
                reason="Attempted access to sensitive file",
                details={"skill": skill.name},
            )
            raise SkillSecurityError(f"Access to sensitive file '{clean_rel}' is strictly forbidden.")

        # 5. Read content (handling binary vs text)
        size = target_path.stat().st_size
        file_type = target_path.suffix.lower()

        try:
            content = target_path.read_text(encoding="utf-8")
            is_binary = False
        except (UnicodeDecodeError, OSError):
            content = ""
            is_binary = True

        record_skill_security_audit(
            action="load_linked_file",
            target=clean_rel,
            decision="allowed",
            reason="Linked file read successfully",
            details={"skill": skill.name, "is_binary": is_binary, "size_bytes": size},
        )

        return LoadedFile(
            skill_name=skill.name,
            relative_path=clean_rel,
            absolute_path=target_path,
            content=content,
            is_binary=is_binary,
            size_bytes=size,
            file_type=file_type,
        )

    @staticmethod
    def _scan_linked_files(skill_dir: Path) -> dict[str, tuple[str, ...]]:
        """Discover linked files in standard support subdirectories."""
        found: dict[str, tuple[str, ...]] = {}
        for sub in _SUPPORTED_SUBDIRS:
            sub_path = skill_dir / sub
            if not sub_path.is_dir():
                continue

            entries: list[str] = []
            try:
                for root, _, files in os.walk(str(sub_path)):
                    root_path = Path(root)
                    for f in sorted(files):
                        file_path = root_path / f
                        if is_sensitive_file(file_path):
                            continue
                        rel = file_path.relative_to(skill_dir).as_posix()
                        entries.append(rel)
            except OSError as exc:
                logger.debug("Failed scanning support dir %s: %s", sub_path, exc)

            if entries:
                found[sub] = tuple(entries)

        return found


_loader_instance: Optional[SkillLoader] = None


def get_skill_loader(registry: Optional[APASkillsRegistry] = None) -> SkillLoader:
    """Return the global process-wide SkillLoader instance."""
    global _loader_instance
    if registry is not None:
        return SkillLoader(registry)
    if _loader_instance is None:
        _loader_instance = SkillLoader()
    return _loader_instance


def load_skill(name_or_identifier: str, force_reload: bool = False) -> LoadedSkill:
    """Top-level convenience function for loading a skill on demand."""
    return get_skill_loader().load_skill(name_or_identifier, force_reload=force_reload)


def load_linked_file(skill_name_or_identifier: str, relative_file_path: str) -> LoadedFile:
    """Top-level convenience function for loading a skill's linked reference file."""
    return get_skill_loader().load_linked_file(skill_name_or_identifier, relative_file_path)
