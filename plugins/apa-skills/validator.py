"""Comprehensive Skill Validator for APA (Phase 4.3).

Performs strict, non-executing structural, metadata, and security validation
of skills across all tiers.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
from typing import Any, Mapping, Optional, Sequence

from .models import (
    ValidationFinding,
    ValidationLevel,
    ValidationReport,
)
from .registry import parse_frontmatter
from .security import (
    check_symlink_or_junction_escape,
    is_sensitive_file,
    validate_skill_path_safety,
)

logger = logging.getLogger(__name__)

# Valid skill name format: 1-64 chars, alphanumeric, hyphens, underscores, dots
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-\.]{1,64}$")

# Suspicious binary and script extensions that are blocked in scripts/
_BLOCKED_SCRIPT_EXTENSIONS = frozenset({
    ".exe",
    ".dll",
    ".bat",
    ".cmd",
    ".vbs",
    ".msi",
    ".bin",
    ".scr",
    ".com",
    ".pif",
})

# High-risk command execution patterns in script bodies
_DANGEROUS_PAYLOAD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"powershell(\.exe)?\s+.*(-enc|-encodedcommand)", re.IGNORECASE), "Encoded PowerShell command"),
    (re.compile(r"\b(iex|invoke-expression)\b", re.IGNORECASE), "PowerShell Invoke-Expression"),
    (re.compile(r"curl\s+.*\|\s*(sh|bash)", re.IGNORECASE), "Piped curl-to-shell execution"),
    (re.compile(r"wget\s+.*\|\s*(sh|bash)", re.IGNORECASE), "Piped wget-to-shell execution"),
    (re.compile(r"rm\s+-rf\s+/(?!\S)", re.IGNORECASE), "Destructive root filesystem deletion"),
    (re.compile(r"del\s+/[fF]\s+/[sS]\s+/[qQ]\s+[cC]:", re.IGNORECASE), "Destructive Windows system deletion"),
    (re.compile(r"format\s+[cC]:", re.IGNORECASE), "Drive format command"),
)

_VALID_PLATFORMS = frozenset({"windows", "linux", "darwin", "macos", "all"})


class SkillValidator:
    """Validates skill structure, frontmatter metadata, and security boundaries without code execution."""

    def validate(
        self,
        skill_dir: Path,
        search_root: Optional[Path] = None,
        *,
        is_external: bool = False,
    ) -> ValidationReport:
        """Validate a skill directory and return a structured ValidationReport."""
        findings: list[ValidationFinding] = []
        name = skill_dir.name

        # 1. Structural and file safety checks
        content = self._validate_structure_and_read(skill_dir, search_root, findings)
        if content is None:
            return self._build_report(name, skill_dir, findings)

        # 2. Frontmatter parsing
        parsed = self._parse_and_validate_frontmatter(content, findings)
        if parsed is None:
            return self._build_report(name, skill_dir, findings)

        frontmatter, instructions = parsed

        # 3. Metadata fields validation
        name = self._validate_metadata_fields(frontmatter, name, findings)

        # 4. Audit supporting subdirectories
        self._validate_supporting_subdirs(skill_dir, findings)

        # 5. External caution & empty instructions
        if is_external:
            findings.append(
                ValidationFinding(
                    code="WARN_EXTERNAL_UNTRUSTED",
                    message="Skill originates from an external source directory; inspect before running.",
                    level=ValidationLevel.WARNING,
                )
            )

        if not instructions.strip():
            findings.append(
                ValidationFinding(
                    code="WARN_EMPTY_INSTRUCTIONS",
                    message="SKILL.md has no instruction body below frontmatter.",
                    level=ValidationLevel.WARNING,
                )
            )

        return self._build_report(name, skill_dir, findings)

    def _validate_structure_and_read(
        self,
        skill_dir: Path,
        search_root: Optional[Path],
        findings: list[ValidationFinding],
    ) -> Optional[str]:
        """Validate directory structure, symlinks, and read SKILL.md."""
        if not skill_dir.exists():
            findings.append(
                ValidationFinding(
                    code="ERR_DIR_NOT_FOUND",
                    message=f"Skill directory does not exist: {skill_dir}",
                    level=ValidationLevel.INVALID,
                )
            )
            return None

        if not skill_dir.is_dir():
            findings.append(
                ValidationFinding(
                    code="ERR_NOT_A_DIRECTORY",
                    message=f"Path is not a directory: {skill_dir}",
                    level=ValidationLevel.INVALID,
                )
            )
            return None

        if search_root is not None and check_symlink_or_junction_escape(skill_dir, search_root):
            findings.append(
                ValidationFinding(
                    code="SEC_SYMLINK_ESCAPE",
                    message=f"Skill directory '{skill_dir}' escapes approved root '{search_root}' via symlink or junction.",
                    level=ValidationLevel.BLOCKED,
                )
            )

        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            findings.append(
                ValidationFinding(
                    code="ERR_MISSING_SKILL_MD",
                    message="Required 'SKILL.md' file is missing from skill directory.",
                    level=ValidationLevel.INVALID,
                )
            )
            return None

        if not skill_md.is_file():
            findings.append(
                ValidationFinding(
                    code="ERR_INVALID_SKILL_MD",
                    message="'SKILL.md' is a directory, not a file.",
                    level=ValidationLevel.INVALID,
                )
            )
            return None

        if check_symlink_or_junction_escape(skill_md, skill_dir):
            findings.append(
                ValidationFinding(
                    code="SEC_SKILL_MD_ESCAPE",
                    message="'SKILL.md' symlink resolves outside the skill directory.",
                    level=ValidationLevel.BLOCKED,
                )
            )

        return self._read_skill_md_content(skill_md, findings)

    @staticmethod
    def _read_skill_md_content(skill_md: Path, findings: list[ValidationFinding]) -> Optional[str]:
        """Safely read SKILL.md UTF-8 content."""
        try:
            content = skill_md.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            findings.append(
                ValidationFinding(
                    code="ERR_ENCODING",
                    message=f"SKILL.md contains invalid UTF-8 bytes: {exc}",
                    level=ValidationLevel.INVALID,
                )
            )
            return None
        except OSError as exc:
            findings.append(
                ValidationFinding(
                    code="ERR_READ_FAILED",
                    message=f"Failed to read SKILL.md: {exc}",
                    level=ValidationLevel.INVALID,
                )
            )
            return None

        if not content.strip():
            findings.append(
                ValidationFinding(
                    code="ERR_EMPTY_SKILL_MD",
                    message="SKILL.md is completely empty.",
                    level=ValidationLevel.INVALID,
                )
            )
            return None

        return content

    @staticmethod
    def _parse_and_validate_frontmatter(
        content: str,
        findings: list[ValidationFinding],
    ) -> Optional[tuple[dict[str, Any], str]]:
        """Verify YAML delimiter and parse frontmatter."""
        stripped = content.lstrip()
        if not stripped.startswith("---"):
            findings.append(
                ValidationFinding(
                    code="ERR_MISSING_FRONTMATTER_DELIMITER",
                    message="SKILL.md must begin with YAML frontmatter delimiters ('---').",
                    level=ValidationLevel.INVALID,
                )
            )

        # Strict YAML syntax validation
        end_match = re.search(r"\n---\s*(\n|$)", stripped[3:])
        if end_match:
            yaml_block = stripped[3 : end_match.start() + 3]
            try:
                from hermes_yaml import safe_load
                parsed = safe_load(yaml_block)
                if parsed is not None and not isinstance(parsed, dict):
                    findings.append(
                        ValidationFinding(
                            code="ERR_INVALID_YAML",
                            message="YAML frontmatter must be a key-value mapping.",
                            level=ValidationLevel.INVALID,
                        )
                    )
                    return None
            except Exception as exc:  # health: allow BLE001 -- strict yaml check
                findings.append(
                    ValidationFinding(
                        code="ERR_INVALID_YAML",
                        message=f"YAML frontmatter syntax error: {exc}",
                        level=ValidationLevel.INVALID,
                    )
                )
                return None

        try:
            frontmatter, instructions = parse_frontmatter(content)
            return frontmatter, instructions
        except Exception as exc:  # health: allow BLE001 -- fail-soft parsing report
            findings.append(
                ValidationFinding(
                    code="ERR_INVALID_YAML",
                    message=f"Failed to parse YAML frontmatter: {exc}",
                    level=ValidationLevel.INVALID,
                )
            )
            return None

    def _validate_metadata_fields(
        self,
        frontmatter: Mapping[str, Any],
        fallback_name: str,
        findings: list[ValidationFinding],
    ) -> str:
        """Validate declared name, description, and platform requirements."""
        declared_name = str(frontmatter.get("name") or fallback_name).strip()

        if not _SKILL_NAME_RE.match(declared_name):
            if ".." in declared_name or "/" in declared_name or "\\" in declared_name:
                findings.append(
                    ValidationFinding(
                        code="SEC_PATH_TRAVERSAL_IN_NAME",
                        message=f"Skill name '{declared_name}' contains illegal path traversal or separator characters.",
                        level=ValidationLevel.BLOCKED,
                    )
                )
            else:
                findings.append(
                    ValidationFinding(
                        code="ERR_INVALID_NAME_FORMAT",
                        message=f"Skill name '{declared_name}' must be 1-64 characters (alphanumeric, hyphens, underscores).",
                        level=ValidationLevel.INVALID,
                    )
                )

        self._check_description(frontmatter.get("description"), findings)
        self._check_platforms(frontmatter.get("platforms"), findings)
        return declared_name

    @staticmethod
    def _check_description(desc: Any, findings: list[ValidationFinding]) -> None:
        """Validate description presence, length, and content."""
        if not desc or not str(desc).strip():
            findings.append(
                ValidationFinding(
                    code="ERR_MISSING_DESCRIPTION",
                    message="Frontmatter must contain a non-empty 'description' field.",
                    level=ValidationLevel.INVALID,
                )
            )
            return

        desc_str = str(desc).strip()
        if len(desc_str) > 1024:
            findings.append(
                ValidationFinding(
                    code="WARN_LONG_DESCRIPTION",
                    message=f"Description is {len(desc_str)} characters (recommended <= 1024).",
                    level=ValidationLevel.WARNING,
                )
            )
        if any(ord(c) < 32 and c not in "\t\n\r" for c in desc_str):
            findings.append(
                ValidationFinding(
                    code="WARN_CONTROL_CHARS",
                    message="Description contains non-printable control characters.",
                    level=ValidationLevel.WARNING,
                )
            )

    @staticmethod
    def _check_platforms(platforms: Any, findings: list[ValidationFinding]) -> None:
        """Validate platform list declarations."""
        if platforms is None:
            return

        if isinstance(platforms, str):
            platforms = [platforms]

        if isinstance(platforms, (list, tuple)):
            for p in platforms:
                p_clean = str(p).strip().lower()
                if p_clean not in _VALID_PLATFORMS:
                    findings.append(
                        ValidationFinding(
                            code="WARN_UNKNOWN_PLATFORM",
                            message=f"Unknown platform declaration '{p}' in 'platforms' list.",
                            level=ValidationLevel.WARNING,
                        )
                    )
        else:
            findings.append(
                ValidationFinding(
                    code="WARN_INVALID_PLATFORMS_TYPE",
                    message="'platforms' field should be a list of platform names.",
                    level=ValidationLevel.WARNING,
                )
            )

    def _validate_supporting_subdirs(self, skill_dir: Path, findings: list[ValidationFinding]) -> None:
        """Scan supporting files and scripts for security hazards without code execution."""
        support_dirs = ("references", "templates", "assets", "scripts")
        for sub in support_dirs:
            sub_path = skill_dir / sub
            if not sub_path.is_dir():
                continue

            try:
                for root, _, files in os.walk(str(sub_path)):
                    root_path = Path(root)
                    for fname in files:
                        file_path = root_path / fname
                        self._audit_support_file(file_path, fname, sub, skill_dir, findings)
            except OSError as exc:
                logger.debug("Failed traversing %s: %s", sub_path, exc)

    def _audit_support_file(
        self,
        file_path: Path,
        fname: str,
        sub: str,
        skill_dir: Path,
        findings: list[ValidationFinding],
    ) -> None:
        """Audit an individual file in a support subdirectory."""
        if check_symlink_or_junction_escape(file_path, skill_dir):
            findings.append(
                ValidationFinding(
                    code="SEC_SYMLINK_ESCAPE",
                    message=f"File '{fname}' in '{sub}' escapes skill directory via symlink.",
                    level=ValidationLevel.BLOCKED,
                    path=str(file_path),
                )
            )
            return

        if is_sensitive_file(file_path):
            findings.append(
                ValidationFinding(
                    code="SEC_SENSITIVE_FILE_EXPOSURE",
                    message=f"Skill bundle contains forbidden sensitive file '{fname}'.",
                    level=ValidationLevel.BLOCKED,
                    path=str(file_path),
                )
            )
            return

        if sub == "scripts":
            self._audit_script_payload(file_path, fname, findings)

    @staticmethod
    def _audit_script_payload(
        file_path: Path,
        fname: str,
        findings: list[ValidationFinding],
    ) -> None:
        """Perform static payload and extension inspection on a script file."""
        ext = file_path.suffix.lower()
        if ext in _BLOCKED_SCRIPT_EXTENSIONS:
            findings.append(
                ValidationFinding(
                    code="SEC_FORBIDDEN_EXECUTABLE_EXT",
                    message=f"Script '{fname}' uses forbidden binary/batch extension '{ext}'.",
                    level=ValidationLevel.BLOCKED,
                    path=str(file_path),
                )
            )
            return

        try:
            script_text = file_path.read_text(encoding="utf-8", errors="ignore")
            for pattern, desc in _DANGEROUS_PAYLOAD_PATTERNS:
                if pattern.search(script_text):
                    findings.append(
                        ValidationFinding(
                            code="SEC_DANGEROUS_COMMAND_PATTERN",
                            message=f"Script '{fname}' contains hazardous pattern: {desc}.",
                            level=ValidationLevel.BLOCKED,
                            path=str(file_path),
                        )
                    )
        except OSError:
            pass

    @staticmethod
    def _build_report(name: str, skill_dir: Path, findings: list[ValidationFinding]) -> ValidationReport:
        """Calculate overall level from findings and construct report."""
        if any(f.level == ValidationLevel.BLOCKED for f in findings):
            level = ValidationLevel.BLOCKED
            is_safe = False
        elif any(f.level == ValidationLevel.INVALID for f in findings):
            level = ValidationLevel.INVALID
            is_safe = False
        elif any(f.level == ValidationLevel.WARNING for f in findings):
            level = ValidationLevel.WARNING
            is_safe = True
        else:
            level = ValidationLevel.VALID
            is_safe = True

        return ValidationReport(
            skill_name=name,
            skill_dir=skill_dir,
            level=level,
            findings=tuple(findings),
            is_safe_to_load=is_safe,
        )


_validator_instance: Optional[SkillValidator] = None


def get_skill_validator() -> SkillValidator:
    """Return the global process-wide SkillValidator instance."""
    global _validator_instance
    if _validator_instance is None:
        _validator_instance = SkillValidator()
    return _validator_instance


def validate_skill(
    skill_dir: Path,
    search_root: Optional[Path] = None,
    *,
    is_external: bool = False,
) -> ValidationReport:
    """Top-level convenience function for validating a skill."""
    return get_skill_validator().validate(skill_dir, search_root=search_root, is_external=is_external)
