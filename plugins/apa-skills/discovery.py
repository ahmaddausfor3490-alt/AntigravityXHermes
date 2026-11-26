"""Skills Discovery Layer for APA (Phase 4.3).

Provides filtered, platform-aware, and tier-ordered discovery of skills using the
centralized APASkillsRegistry while preserving progressive disclosure invariants.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .models import SkillEntry, SkillSource, ValidationStatus
from .registry import APASkillsRegistry, get_skills_registry
from .security import check_symlink_or_junction_escape

logger = logging.getLogger(__name__)


class SkillsDiscovery:
    """Discovers and inspects skills across approved search roots."""

    def __init__(self, registry: Optional[APASkillsRegistry] = None) -> None:
        self._registry = registry or get_skills_registry()

    @property
    def registry(self) -> APASkillsRegistry:
        return self._registry

    def discover(
        self,
        *,
        category: Optional[str] = None,
        source: Optional[SkillSource | str] = None,
        include_disabled: bool = False,
        platform_compatible_only: bool = True,
        force_refresh: bool = False,
    ) -> list[SkillEntry]:
        """Discover and filter skills according to capability, platform, and availability."""
        if force_refresh:
            self._registry.refresh()

        entries = self._registry.list_skills(
            category=category,
            source=source,
            enabled_only=not include_disabled,
            platform_compatible_only=platform_compatible_only,
        )
        return entries

    def list_progressive_summary(
        self,
        *,
        category: Optional[str] = None,
        source: Optional[SkillSource | str] = None,
        include_disabled: bool = False,
        platform_compatible_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Progressive Disclosure Phase 1: Return lightweight summaries of loadable skills.

        Strictly omits full instructions and body markdown to preserve prompt cache invariants.
        """
        skills = self.discover(
            category=category,
            source=source,
            include_disabled=include_disabled,
            platform_compatible_only=platform_compatible_only,
        )
        return [self.to_summary_dict(s) for s in skills]

    @staticmethod
    def to_summary_dict(entry: SkillEntry) -> dict[str, Any]:
        """Format a single SkillEntry as a Phase 1 lightweight discovery summary."""
        return {
            "name": entry.name,
            "load_name": entry.load_name or entry.name,
            "description": entry.description,
            "category": entry.category,
            "source": entry.source.value,
            "tier": entry.tier,
            "enabled": entry.enabled,
            "platform_compatible": entry.platform_compatible,
            "is_loadable": entry.is_loadable,
            "validation_status": entry.validation_status.value,
            "tags": list(entry.tags),
            "requires_tools": list(entry.requires_tools),
        }

    def inspect_search_roots(self) -> dict[str, Any]:
        """Audit filesystem health of configured skill search roots.

        Detects missing directories, permission errors, subdirectories missing SKILL.md,
        and potential boundary escapes.
        """
        active_roots = self._registry.get_active_roots()
        report: dict[str, Any] = {
            "total_roots": len(active_roots),
            "valid_roots": [],
            "missing_roots": [],
            "inaccessible_roots": [],
            "missing_skill_md_dirs": [],
            "escaped_dirs": [],
        }

        for tier, search_dir in active_roots:
            if not search_dir.exists():
                report["missing_roots"].append({"tier": tier, "path": str(search_dir)})
                continue

            if not search_dir.is_dir():
                report["inaccessible_roots"].append({
                    "tier": tier,
                    "path": str(search_dir),
                    "reason": "Not a directory",
                })
                continue

            try:
                # Test directory readability
                with os.scandir(search_dir) as it:
                    _ = next(it, None)
                report["valid_roots"].append({"tier": tier, "path": str(search_dir)})
            except OSError as exc:
                report["inaccessible_roots"].append({
                    "tier": tier,
                    "path": str(search_dir),
                    "reason": str(exc),
                })
                continue

            # Check subdirectories for missing SKILL.md or symlink escapes
            self._inspect_root_subdirs(search_dir, tier, report)

        return report

    def _inspect_root_subdirs(self, search_dir: Path, tier: int, report: dict[str, Any]) -> None:
        """Inspect child directories of search_dir for missing SKILL.md or escapes."""
        try:
            for entry in os.scandir(search_dir):
                if not entry.is_dir(follow_symlinks=False) or entry.name.startswith((".", "_")):
                    continue

                subdir = Path(entry.path)
                if check_symlink_or_junction_escape(subdir, search_dir):
                    report["escaped_dirs"].append({
                        "tier": tier,
                        "path": str(subdir),
                        "root": str(search_dir),
                    })
                    continue

                if (subdir / "SKILL.md").is_file():
                    continue

                # Category directory pattern: inspect child subdirectories
                child_dirs = self._list_child_dirs(subdir)
                if not child_dirs:
                    report["missing_skill_md_dirs"].append({
                        "tier": tier,
                        "path": str(subdir),
                    })
                else:
                    for child in child_dirs:
                        if not (child / "SKILL.md").is_file():
                            report["missing_skill_md_dirs"].append({
                                "tier": tier,
                                "path": str(child),
                            })
        except OSError as exc:
            logger.debug("Failed scanning subdirectories of %s: %s", search_dir, exc)

    @staticmethod
    def _list_child_dirs(directory: Path) -> list[Path]:
        """List non-hidden child directories of directory."""
        dirs: list[Path] = []
        try:
            for entry in os.scandir(directory):
                if entry.is_dir(follow_symlinks=False) and not entry.name.startswith((".", "_")):
                    dirs.append(Path(entry.path))
        except OSError:
            pass
        return dirs

    def get_precedence_chain(self, skill_name: str) -> list[dict[str, Any]]:
        """Return the tier precedence resolution chain for a skill name across all tiers."""
        clean_name = skill_name.strip()
        if not clean_name:
            return []

        # Force discovery if uninitialized
        self._registry.discover()
        duplicates = self._registry.find_duplicates()
        entries = duplicates.get(clean_name, [])

        if not entries:
            single = self._registry.get_skill(clean_name)
            if single:
                entries = [single]

        # Sort by tier (Tier 0 PROJECT wins over Tier 2 LOCAL)
        sorted_entries = sorted(entries, key=lambda s: s.tier)
        return [
            {
                "name": s.name,
                "tier": s.tier,
                "source": s.source.value,
                "path": str(s.path),
                "is_winner": idx == 0 and s.validation_status != ValidationStatus.AMBIGUOUS,
                "validation_status": s.validation_status.value,
                "load_name": s.load_name,
            }
            for idx, s in enumerate(sorted_entries)
        ]


_discovery_instance: Optional[SkillsDiscovery] = None


def get_skills_discovery(registry: Optional[APASkillsRegistry] = None) -> SkillsDiscovery:
    """Return the singleton or customized SkillsDiscovery instance."""
    global _discovery_instance
    if registry is not None:
        return SkillsDiscovery(registry)
    if _discovery_instance is None:
        _discovery_instance = SkillsDiscovery()
    return _discovery_instance


def discover_skills(
    category: Optional[str] = None,
    source: Optional[SkillSource | str] = None,
    include_disabled: bool = False,
    platform_compatible_only: bool = True,
    force_refresh: bool = False,
) -> list[SkillEntry]:
    """Top-level convenience function for discovering skills."""
    return get_skills_discovery().discover(
        category=category,
        source=source,
        include_disabled=include_disabled,
        platform_compatible_only=platform_compatible_only,
        force_refresh=force_refresh,
    )


def list_skills_summary(
    category: Optional[str] = None,
    source: Optional[SkillSource | str] = None,
) -> list[dict[str, Any]]:
    """Top-level convenience function for Progressive Disclosure Phase 1."""
    return get_skills_discovery().list_progressive_summary(category=category, source=source)
