"""APA Skills Registry — Hermes Agent skills orchestration and management plugin entrypoint."""

from __future__ import annotations

import logging
from typing import Optional

from hermes_cli.plugins import PluginContext

from .cli import APASkillsCLI, build_parser, main as skills_cli_main
from .discovery import SkillsDiscovery, discover_skills, get_skills_discovery, list_skills_summary
from .loader import SkillLoader, get_skill_loader, load_linked_file, load_skill
from .models import (
    AmbiguousSkillError,
    LoadedFile,
    LoadedSkill,
    RegistryStatus,
    SkillEntry,
    SkillError,
    SkillLoadError,
    SkillNotFoundError,
    SkillSecurityError,
    SkillSource,
    SkillValidationError,
    ValidationFinding,
    ValidationLevel,
    ValidationReport,
    ValidationStatus,
)
from .registry import APASkillsRegistry, get_skills_registry, reset_skills_registry
from .security import (
    check_symlink_or_junction_escape,
    has_traversal_component,
    is_sensitive_file,
    record_skill_security_audit,
    validate_skill_path_safety,
)
from .validator import SkillValidator, get_skill_validator, validate_skill

__all__ = [
    "APASkillsCLI",
    "APASkillsRegistry",
    "AmbiguousSkillError",
    "LoadedFile",
    "LoadedSkill",
    "RegistryStatus",
    "SkillEntry",
    "SkillError",
    "SkillLoadError",
    "SkillLoader",
    "SkillNotFoundError",
    "SkillSecurityError",
    "SkillSource",
    "SkillValidationError",
    "SkillValidator",
    "SkillsDiscovery",
    "ValidationFinding",
    "ValidationLevel",
    "ValidationReport",
    "ValidationStatus",
    "check_symlink_or_junction_escape",
    "discover_skills",
    "get_skill_loader",
    "get_skill_validator",
    "get_skills_discovery",
    "get_skills_registry",
    "has_traversal_component",
    "is_sensitive_file",
    "list_skills_summary",
    "load_linked_file",
    "load_skill",
    "record_skill_security_audit",
    "register",
    "reset_skills_registry",
    "validate_skill",
    "validate_skill_path_safety",
]

logger = logging.getLogger(__name__)


def register(ctx: PluginContext) -> None:
    """Register APA Skills Registry plugin with Hermes Agent."""
    enabled = ctx.get_config("enabled", default=True)
    if not enabled:
        logger.info("APA Skills Registry plugin is disabled via config.")
        return

    # Initialize the global registry singleton
    get_skills_registry()

    def _on_unload() -> None:
        reset_skills_registry()

    ctx.on_unload(_on_unload)
    logger.info("APA Skills Registry plugin registered successfully (manifest=%s).", ctx.manifest.name)
