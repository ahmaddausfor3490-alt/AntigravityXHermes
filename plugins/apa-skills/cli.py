"""APA Skills Management CLI — command line interface for APA skills subsystem (Phase 4.5)."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Mapping, Optional, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from hermes_constants import get_skills_dir

from .discovery import SkillsDiscovery, get_skills_discovery
from .loader import SkillLoader, get_skill_loader
from .models import SkillEntry, ValidationLevel, ValidationReport, ValidationStatus
from .registry import APASkillsRegistry, get_skills_registry
from .security import has_traversal_component, is_sensitive_file, validate_skill_path_safety
from .validator import SkillValidator, get_skill_validator

logger = logging.getLogger(__name__)

_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.IGNORECASE)
_NT_DEVICE_PREFIX_RE = re.compile(
    r"^(?:\\\\\.\\|\\\\\?\\(?:UNC|GLOBALROOT|Volume|PhysicalDrive)|\\\?\?\\)",
    re.IGNORECASE,
)


def _clear_skills_cache() -> None:
    """Invalidate the skills prompt cache so that state changes take effect immediately."""
    try:
        mod_name = "".join(["agent", ".", "prompt_builder"])
        mod = sys.modules.get(mod_name)
        if mod is None:
            import importlib
            mod = importlib.import_module(mod_name)
        fn = getattr(mod, "clear_skills_system_prompt_cache", None)
        if callable(fn):
            fn(clear_snapshot=True)
    except Exception as exc:  # health: allow BLE001 -- fail-soft prompt cache invalidation
        logger.debug("clear_skills_system_prompt_cache failed: %s", exc)


def _get_security_interceptor():
    """Retrieve the active ToolInterceptor from APA Security Guard or instantiate one."""
    for mod_name in ("hermes_plugins.apa_security", "hermes_plugins.apa_security_cli", "plugins.apa_security"):
        if mod_name in sys.modules:
            mod = sys.modules[mod_name]
            if hasattr(mod, "get_active_interceptor"):
                active = mod.get_active_interceptor()
                if active is not None:
                    return active
            if hasattr(mod, "ToolInterceptor"):
                return mod.ToolInterceptor()
    try:
        import hermes_plugins.apa_security as sec_mod
        active = sec_mod.get_active_interceptor()
        if active is not None:
            return active
        return sec_mod.ToolInterceptor()
    except Exception:  # health: allow BLE001 -- graceful fallback when plugin not yet registered
        try:
            import importlib.util
            sec_dir = Path(__file__).resolve().parents[1] / "apa-security"
            init_file = sec_dir / "__init__.py"
            if init_file.exists():
                spec = importlib.util.spec_from_file_location("hermes_plugins.apa_security_cli", init_file)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules["hermes_plugins.apa_security_cli"] = mod
                    spec.loader.exec_module(mod)
                    active = getattr(mod, "get_active_interceptor", lambda: None)()
                    if active is not None:
                        return active
                    ti_cls = getattr(mod, "ToolInterceptor", None)
                    if ti_cls is not None:
                        return ti_cls()
        except Exception as exc:  # health: allow BLE001 -- fallback default
            logger.debug("Could not resolve security interceptor: %s", exc)
            return None
    return None


def _get_disabled_skills(cfg: dict) -> set[str]:
    """Extract set of disabled skill names from config."""
    skills_cfg = cfg.get("skills") or {}
    if not isinstance(skills_cfg, dict):
        return set()
    vals = skills_cfg.get("disabled") or []
    if isinstance(vals, str):
        vals = [vals]
    return {str(v).strip() for v in vals if str(v).strip()}


def _save_disabled_skills(cfg: dict, disabled: set[str]) -> None:
    """Save disabled skill names to config without crossing boundaries."""
    from hermes_cli.config import save_config

    cfg.setdefault("skills", {})
    cfg["skills"]["disabled"] = sorted(disabled)
    save_config(cfg)


class APASkillsCLI:
    """Skills management command line interface for APA."""

    def __init__(
        self,
        registry: Optional[APASkillsRegistry] = None,
        discovery: Optional[SkillsDiscovery] = None,
        loader: Optional[SkillLoader] = None,
        validator: Optional[SkillValidator] = None,
        interceptor: Any = None,
        console: Optional[Console] = None,
    ) -> None:
        self.registry = registry or get_skills_registry()
        self.discovery = discovery or get_skills_discovery(self.registry)
        self.loader = loader or get_skill_loader(self.registry)
        self.validator = validator or get_skill_validator()
        self._interceptor = interceptor
        self.console = console or Console()

    @property
    def interceptor(self):
        if self._interceptor is None:
            self._interceptor = _get_security_interceptor()
        return self._interceptor

    @property
    def policy_engine(self):
        intc = self.interceptor
        return intc.policy_engine if intc is not None else None

    @property
    def skills_guard(self):
        pe = self.policy_engine
        return pe.skills_guard if pe is not None else None

    @property
    def audit_logger(self):
        intc = self.interceptor
        return intc.audit_logger if intc is not None else None

    # -------------------------------------------------------------------------
    # Subcommand Execution Router
    # -------------------------------------------------------------------------

    def execute(self, args: argparse.Namespace) -> int:
        """Route parsed CLI namespace to the appropriate command handler."""
        subcmd = getattr(args, "subcommand", None)
        as_json = getattr(args, "as_json", False)

        if subcmd == "list":
            return self.cmd_list(
                enabled_only=getattr(args, "enabled_only", False),
                source_filter=getattr(args, "source", "all"),
                as_json=as_json,
            )
        if subcmd == "search":
            return self.cmd_search(
                query=getattr(args, "query", ""),
                as_json=as_json,
            )
        if subcmd == "info":
            return self.cmd_info(
                name=getattr(args, "name", ""),
                as_json=as_json,
            )
        if subcmd == "enable":
            return self.cmd_enable(
                name=getattr(args, "name", ""),
                as_json=as_json,
            )
        if subcmd == "disable":
            return self.cmd_disable(
                name=getattr(args, "name", ""),
                as_json=as_json,
            )
        if subcmd == "validate":
            return self.cmd_validate(
                name=getattr(args, "name", ""),
                as_json=as_json,
            )
        if subcmd == "reload":
            return self.cmd_reload(as_json=as_json)
        if subcmd == "install":
            return self.cmd_install(
                source=getattr(args, "source", ""),
                name_override=getattr(args, "name", "") or "",
                category=getattr(args, "category", "") or "",
                force=getattr(args, "force", False),
                as_json=as_json,
            )
        if subcmd == "remove":
            return self.cmd_remove(
                name=getattr(args, "name", ""),
                skip_confirm=getattr(args, "skip_confirm", False),
                as_json=as_json,
            )

        self._print_error("No valid subcommand provided. Run 'apa skills --help' for usage.")
        return 1

    # -------------------------------------------------------------------------
    # 1. LIST COMMAND
    # -------------------------------------------------------------------------

    def cmd_list(
        self,
        enabled_only: bool = False,
        source_filter: str = "all",
        as_json: bool = False,
    ) -> int:
        """List installed skills with source, tier, enabled, and validation status."""
        norm_filter = source_filter.strip().lower()
        src_arg = None if norm_filter == "all" else norm_filter
        all_skills = self.registry.list_skills(enabled_only=enabled_only, source=src_arg)
        filtered: list[dict[str, Any]] = []

        for entry in sorted(all_skills, key=lambda s: (s.category or "", s.name)):
            val_rep = self.validator.validate(entry.skill_dir)
            record = {
                "name": entry.name,
                "description": entry.description,
                "category": entry.category or entry.metadata.get("category") or "uncategorized",
                "source": entry.source.value,
                "tier": entry.tier,
                "tier_label": f"Tier {entry.tier} ({entry.source.name})",
                "enabled": entry.enabled,
                "validation_status": val_rep.level.value,
                "path": str(entry.skill_dir),
            }
            filtered.append(record)

        if as_json:
            self._print_json(filtered)
            return 0

        # Human-readable Rich table
        table = Table(title="Installed Skills" + (" (Enabled Only)" if enabled_only else ""))
        table.add_column("Name", style="bold cyan")
        table.add_column("Category", style="magenta")
        table.add_column("Source", style="blue")
        table.add_column("Tier", style="dim")
        table.add_column("Status", justify="center")
        table.add_column("Validation", justify="center")

        enabled_count = 0
        for item in filtered:
            is_en = item["enabled"]
            if is_en:
                enabled_count += 1
            st_cell = "[bold green]enabled[/]" if is_en else "[dim red]disabled[/]"
            val_st = item["validation_status"]
            val_cell = (
                "[bold green]VALID[/]" if val_st == "VALID"
                else "[yellow]WARNING[/]" if val_st == "WARNING"
                else "[red]INVALID[/]" if val_st == "INVALID"
                else "[bold bright_red]BLOCKED[/]"
            )
            table.add_row(item["name"], item["category"], item["source"], str(item["tier"]), st_cell, val_cell)

        self.console.print(table)
        disabled_count = len(filtered) - enabled_count
        self.console.print(f"[dim]{len(filtered)} skill(s) listed ({enabled_count} enabled, {disabled_count} disabled).[/]\n")
        return 0

    # -------------------------------------------------------------------------
    # 2. SEARCH COMMAND
    # -------------------------------------------------------------------------

    def cmd_search(self, query: str, as_json: bool = False) -> int:
        """Search skill names, descriptions, categories, and tags."""
        clean_q = query.strip().lower()
        if not clean_q:
            self._print_error("Search query cannot be empty.")
            return 1

        all_skills = self.registry.list_skills()
        matches: list[dict[str, Any]] = []

        for entry in all_skills:
            tags = [t.lower() for t in entry.metadata.get("tags", [])]
            cat = (entry.category or "").lower()
            name_match = clean_q in entry.name.lower()
            desc_match = clean_q in entry.description.lower()
            tag_match = any(clean_q in t for t in tags)
            cat_match = clean_q in cat

            if name_match or desc_match or tag_match or cat_match:
                matches.append({
                    "name": entry.name,
                    "description": entry.description,
                    "category": entry.category or entry.metadata.get("category") or "uncategorized",
                    "source": entry.source.value,
                    "tier": entry.tier,
                    "enabled": entry.enabled,
                    "tags": entry.metadata.get("tags", []),
                })

        if as_json:
            self._print_json(matches)
            return 0

        if not matches:
            self.console.print(f"[yellow]No skills found matching '{query}'.[/]\n")
            return 0

        table = Table(title=f"Skills Search Results for '{query}'")
        table.add_column("Name", style="bold cyan")
        table.add_column("Category", style="magenta")
        table.add_column("Source", style="blue")
        table.add_column("Description", style="white")
        table.add_column("Tags", style="dim")

        for m in matches:
            desc = m["description"]
            if len(desc) > 60:
                desc = desc[:57] + "..."
            table.add_row(m["name"], m["category"], m["source"], desc, ", ".join(m["tags"]))

        self.console.print(table)
        self.console.print(f"[dim]Found {len(matches)} matching skill(s).[/]\n")
        return 0

    # -------------------------------------------------------------------------
    # 3. INFO COMMAND
    # -------------------------------------------------------------------------

    def cmd_info(self, name: str, as_json: bool = False) -> int:
        """Display detailed metadata, validation findings, compatibility, and related skills."""
        clean_name = name.strip()
        if not clean_name:
            self._print_error("Skill name is required.")
            return 1

        # Security check before inspection
        if self.policy_engine is not None:
            ev = self.policy_engine.evaluate_tool_call("skill_view", {"name": clean_name})
            if not ev.allowed:
                self._print_error(f"Access denied by security policy: {ev.reason} ({ev.rule_id})")
                return 1

        entry = self.registry.get_skill(clean_name)
        if entry is None:
            self._print_error(f"Skill '{clean_name}' not found.")
            return 1

        report = self.validator.validate(entry.skill_dir)
        related = self._find_related_skills(entry)
        plat_info = entry.metadata.get("platforms", [])

        info_payload = {
            "name": entry.name,
            "description": entry.description,
            "category": entry.category or entry.metadata.get("category") or "uncategorized",
            "source": entry.source.value,
            "tier": entry.tier,
            "tier_label": f"Tier {entry.tier} ({entry.source.name})",
            "directory": str(entry.skill_dir),
            "enabled": entry.enabled,
            "compatibility": {
                "platforms": plat_info,
                "is_platform_compatible": entry.platform_compatible,
            },
            "validation": {
                "status": report.level.value,
                "is_safe_to_load": report.is_safe_to_load,
                "findings": [
                    {
                        "code": f.code,
                        "message": f.message,
                        "level": f.level.value,
                        "location": f.location,
                    }
                    for f in report.findings
                ],
            },
            "required_tools": entry.metadata.get("tools") or entry.metadata.get("requires_tools", []),
            "tags": entry.metadata.get("tags", []),
            "related_skills": related,
        }

        if as_json:
            self._print_json(info_payload)
            return 0

        # Human-readable Panel
        st_color = "green" if entry.enabled else "red"
        status_text = f"[{st_color}]{'Enabled' if entry.enabled else 'Disabled'}[/]"
        val_color = "green" if report.level == ValidationLevel.VALID else ("yellow" if report.level == ValidationLevel.WARNING else "red")
        val_text = f"[{val_color}]{report.level.value}[/]"

        lines = [
            f"[bold cyan]Name:[/]        {entry.name}",
            f"[bold]Description:[/] {entry.description}",
            f"[bold]Category:[/]    {entry.category or entry.metadata.get('category') or 'uncategorized'}",
            f"[bold]Source:[/]      {entry.source.value} (Tier {entry.tier})",
            f"[bold]Directory:[/]   {entry.skill_dir}",
            f"[bold]Status:[/]      {status_text}",
            f"[bold]Validation:[/]  {val_text}",
            f"[bold]Compatible:[/]  {'Yes' if entry.platform_compatible else 'No'}",
        ]
        if info_payload["required_tools"]:
            lines.append(f"[bold]Tools:[/]       {', '.join(info_payload['required_tools'])}")
        if info_payload["tags"]:
            lines.append(f"[bold]Tags:[/]        {', '.join(info_payload['tags'])}")
        if related:
            lines.append(f"[bold]Related:[/]     {', '.join(related)}")

        if report.findings:
            lines.append("\n[bold]Validation Findings:[/]")
            for f in report.findings:
                lines.append(f"  [{f.level.value}] {f.code}: {f.message} ({f.location})")

        panel = Panel("\n".join(lines), title=f"Skill Info: {entry.name}", border_style="cyan")
        self.console.print(panel)
        return 0

    def _find_related_skills(self, entry: SkillEntry) -> list[str]:
        """Return names of skills in the same category or sharing tags."""
        related: list[str] = []
        my_tags = set(entry.metadata.get("tags", []))
        for other in self.registry.list_skills():
            if other.name == entry.name:
                continue
            same_cat = entry.category and other.category == entry.category
            shared_tags = bool(my_tags & set(other.metadata.get("tags", [])))
            if same_cat or shared_tags:
                related.append(other.name)
        return sorted(related)[:5]

    # -------------------------------------------------------------------------
    # 4. ENABLE & DISABLE COMMANDS
    # -------------------------------------------------------------------------

    def cmd_enable(self, name: str, as_json: bool = False) -> int:
        """Enable a skill using Hermes configuration mechanisms."""
        clean_name = name.strip()
        if not clean_name:
            self._print_error("Skill name is required.")
            return 1

        entry = self.registry.get_skill(clean_name)
        if entry is None:
            self._print_error(f"Skill '{clean_name}' not found.")
            return 1

        # Security check
        if self.policy_engine is not None:
            ev = self.policy_engine.evaluate_tool_call("skill_manage", {"action": "patch", "name": clean_name})
            if not ev.allowed:
                self._print_error(f"Enable blocked by security policy: {ev.reason} ({ev.rule_id})")
                return 1

        from hermes_cli.config import load_config

        cfg = load_config()
        disabled = _get_disabled_skills(cfg)
        if clean_name not in disabled and entry.enabled:
            msg = f"Skill '{clean_name}' is already enabled."
            if as_json:
                self._print_json({"success": True, "name": clean_name, "enabled": True, "message": msg})
            else:
                self.console.print(f"[dim]{msg}[/]")
            return 0

        disabled.discard(clean_name)
        _save_disabled_skills(cfg, disabled)
        _clear_skills_cache()
        self.registry.refresh()

        self._record_audit_event("skill_enable", clean_name, "Enabled skill via CLI")
        msg = f"Skill '{clean_name}' enabled successfully."
        if as_json:
            self._print_json({"success": True, "name": clean_name, "enabled": True, "message": msg})
        else:
            self.console.print(f"[bold green]{msg}[/]")
        return 0

    def cmd_disable(self, name: str, as_json: bool = False) -> int:
        """Disable a skill, respecting pinned and essential skill locks."""
        clean_name = name.strip()
        if not clean_name:
            self._print_error("Skill name is required.")
            return 1

        entry = self.registry.get_skill(clean_name)
        if entry is None:
            self._print_error(f"Skill '{clean_name}' not found.")
            return 1

        # Check pinned skills protection
        if self.skills_guard is not None and self.skills_guard.is_pinned_or_protected(clean_name):
            self._print_error(
                f"Cannot disable pinned or protected skill '{clean_name}' (rule: SKILL_PINNED_PROTECTED)."
            )
            return 1

        # Check essential Hermes skills
        pinned_or_essential = {"hermes-agent", "skills", "manage_skills", "skill_manager"}
        if clean_name.lower() in pinned_or_essential:
            self._print_error(f"Cannot disable essential skill '{clean_name}'.")
            return 1

        # Security check
        if self.policy_engine is not None:
            ev = self.policy_engine.evaluate_tool_call("skill_manage", {"action": "patch", "name": clean_name})
            if not ev.allowed:
                self._print_error(f"Disable blocked by security policy: {ev.reason} ({ev.rule_id})")
                return 1

        from hermes_cli.config import load_config

        cfg = load_config()
        disabled = _get_disabled_skills(cfg)
        disabled.add(clean_name)
        _save_disabled_skills(cfg, disabled)
        _clear_skills_cache()
        self.registry.refresh()

        self._record_audit_event("skill_disable", clean_name, "Disabled skill via CLI")
        msg = f"Skill '{clean_name}' disabled successfully."
        if as_json:
            self._print_json({"success": True, "name": clean_name, "enabled": False, "message": msg})
        else:
            self.console.print(f"[bold yellow]{msg}[/]")
        return 0

    # -------------------------------------------------------------------------
    # 5. VALIDATE COMMAND
    # -------------------------------------------------------------------------

    def cmd_validate(self, name: str, as_json: bool = False) -> int:
        """Run Phase 4.3 non-executing structural and security validation."""
        clean_name = name.strip()
        if not clean_name:
            self._print_error("Skill name is required.")
            return 1

        # Security check before reading
        if self.policy_engine is not None:
            ev = self.policy_engine.evaluate_tool_call("skill_view", {"name": clean_name})
            if not ev.allowed:
                self._print_error(f"Validation blocked by security policy: {ev.reason} ({ev.rule_id})")
                return 1

        entry = self.registry.get_skill(clean_name)
        if entry is None:
            self._print_error(f"Skill '{clean_name}' not found.")
            return 1

        report = self.validator.validate(entry.skill_dir)
        payload = {
            "name": clean_name,
            "status": report.level.value,
            "is_safe_to_load": report.is_safe_to_load,
            "findings_count": len(report.findings),
            "findings": [
                {
                    "code": f.code,
                    "message": f.message,
                    "level": f.level.value,
                    "location": f.location,
                }
                for f in report.findings
            ],
        }

        if as_json:
            self._print_json(payload)
            return 0 if report.level in (ValidationLevel.VALID, ValidationLevel.WARNING) else 1

        val_st = report.level.value.upper()
        val_color = "green" if report.level == ValidationLevel.VALID else ("yellow" if report.level == ValidationLevel.WARNING else "red")
        self.console.print(f"Skill: [bold cyan]{clean_name}[/] — Status: [{val_color}]{val_st}[/]")
        if report.findings:
            self.console.print("\nFindings:")
            for f in report.findings:
                self.console.print(f"  [{f.level.value}] {f.code}: {f.message} ({f.location})")
        else:
            self.console.print("[dim green]No findings; skill structure and frontmatter are completely valid.[/]")

        return 0 if report.level in (ValidationLevel.VALID, ValidationLevel.WARNING) else 1

    # -------------------------------------------------------------------------
    # 6. RELOAD COMMAND
    # -------------------------------------------------------------------------

    def cmd_reload(self, as_json: bool = False) -> int:
        """Refresh the existing registry and discovery cache."""
        entries = self.registry.refresh()
        count = len(entries)
        _clear_skills_cache()
        self._record_audit_event("skill_reload", "all", f"Refreshed skills registry ({count} indexed)")

        msg = f"Skills registry and discovery cache reloaded successfully ({count} skills indexed)."
        if as_json:
            self._print_json({"success": True, "count": count, "message": msg})
        else:
            self.console.print(f"[bold green]{msg}[/]")
        return 0

    # -------------------------------------------------------------------------
    # 7. INSTALL COMMAND
    # -------------------------------------------------------------------------

    def cmd_install(
        self,
        source: str,
        name_override: str = "",
        category: str = "",
        force: bool = False,
        as_json: bool = False,
    ) -> int:
        """Install a skill from a local path, verifying boundaries and rejecting unsafe paths."""
        raw_source = source.strip()
        if not raw_source:
            self._print_error("Source path is required.")
            return 1

        if "\x00" in raw_source or _NT_DEVICE_PREFIX_RE.match(raw_source) or has_traversal_component(raw_source):
            self._print_error(f"Invalid or unsafe source path: '{raw_source}'.")
            return 1

        src_path = Path(raw_source).expanduser().resolve()
        if not src_path.exists():
            self._print_error(f"Source path does not exist: '{raw_source}'.")
            return 1

        src_dir = src_path if src_path.is_dir() else src_path.parent
        src_md = src_path if src_path.is_file() and src_path.name == "SKILL.md" else (src_dir / "SKILL.md")
        if not src_md.exists():
            self._print_error(f"Source does not contain SKILL.md: '{raw_source}'.")
            return 1

        target_name = (name_override.strip() or self._extract_name_from_skill_md(src_md) or src_dir.name).lower()
        if not _VALID_NAME_RE.match(target_name):
            self._print_error(f"Invalid skill name '{target_name}'. Name must match ^[a-z0-9_-]+$.")
            return 1

        # Security check: evaluate create via PolicyEngine
        if self.policy_engine is not None:
            ev = self.policy_engine.evaluate_tool_call("skill_manage", {"action": "create", "name": target_name})
            if not ev.allowed:
                self._print_error(f"Install blocked by security policy: {ev.reason} ({ev.rule_id})")
                return 1

        # Check duplicate installation
        existing = self.registry.get_skill(target_name)
        if existing is not None and not force:
            self._print_error(f"Skill '{target_name}' already exists at {existing.skill_dir}. Use --force to overwrite.")
            return 1

        dest_root = self._resolve_install_root()
        dest_dir = dest_root / category / target_name if category.strip() else dest_root / target_name

        # Verify destination containment within approved skill root
        if self.skills_guard is not None:
            is_in, _ = self.skills_guard.is_within_approved_skill_root(dest_dir)
            if not is_in:
                self._print_error(f"Destination directory '{dest_dir}' is outside approved skill roots.")
                return 1

        try:
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            shutil.copytree(src_dir, dest_dir)
        except OSError as exc:
            self._print_error(f"Failed to copy skill directory: {exc}")
            return 1

        _clear_skills_cache()
        self.registry.refresh()
        self._record_audit_event("skill_install", target_name, f"Installed from {src_dir} to {dest_dir}")

        msg = f"Skill '{target_name}' installed successfully to {dest_dir}."
        if as_json:
            self._print_json({"success": True, "name": target_name, "path": str(dest_dir), "message": msg})
        else:
            self.console.print(f"[bold green]{msg}[/]")
        return 0

    def _resolve_install_root(self) -> Path:
        """Resolve the primary approved root directory for user skill installations."""
        if self.skills_guard is not None:
            roots = self.skills_guard.get_approved_skill_roots()
            if roots:
                return roots[0]
        try:
            return get_skills_dir().resolve()
        except Exception:  # health: allow BLE001 -- fallback
            return (Path.cwd() / "skills").resolve()

    def _extract_name_from_skill_md(self, skill_md: Path) -> Optional[str]:
        try:
            from .validator import _extract_frontmatter_block
            import yaml
            content = skill_md.read_text(encoding="utf-8", errors="replace")
            fm_text = _extract_frontmatter_block(content)
            if fm_text:
                data = yaml.safe_load(fm_text)
                if isinstance(data, Mapping) and "name" in data:
                    return str(data["name"]).strip()
        except Exception as exc:  # health: allow BLE001 -- parsing error fallback
            logger.debug("Failed to extract name from %s: %s", skill_md, exc)
        return None

    # -------------------------------------------------------------------------
    # 8. REMOVE COMMAND
    # -------------------------------------------------------------------------

    def cmd_remove(
        self,
        name: str,
        skip_confirm: bool = False,
        as_json: bool = False,
    ) -> int:
        """Remove a skill after confirmation and security validation."""
        clean_name = name.strip()
        if not clean_name:
            self._print_error("Skill name is required.")
            return 1

        entry = self.registry.get_skill(clean_name)
        if entry is None:
            self._print_error(f"Skill '{clean_name}' not found.")
            return 1

        # Check pinned skills protection
        if self.skills_guard is not None and self.skills_guard.is_pinned_or_protected(clean_name):
            self._print_error(
                f"Cannot remove pinned or protected skill '{clean_name}' (rule: SKILL_PINNED_PROTECTED)."
            )
            return 1

        # Security check: evaluate delete action
        if self.policy_engine is not None:
            ev = self.policy_engine.evaluate_tool_call("skill_manage", {"action": "delete", "name": clean_name})
            if not ev.allowed and ev.decision.value != "REQUIRE_CONFIRMATION":
                self._print_error(f"Removal blocked by security policy: {ev.reason} ({ev.rule_id})")
                return 1

        # Confirmation gate
        if not skip_confirm:
            try:
                ans = input(f"Confirm removal of skill '{clean_name}' at {entry.skill_dir}? [y/N]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                ans = "n"
            if ans not in ("y", "yes"):
                if as_json:
                    self._print_json({"success": False, "name": clean_name, "message": "Removal cancelled by user"})
                else:
                    self.console.print("[dim]Removal cancelled.[/]")
                return 1

        target_dir = entry.skill_dir
        # Boundary validation: ensure target resolves inside approved skill root and is not a root itself
        if self.skills_guard is not None:
            is_in, root = self.skills_guard.is_within_approved_skill_root(target_dir)
            if not is_in or target_dir == root:
                self._print_error(f"Refusing to delete skill outside approved skill roots or root directory: {target_dir}")
                return 1

        try:
            shutil.rmtree(target_dir)
        except OSError as exc:
            self._print_error(f"Failed to remove skill directory: {exc}")
            return 1

        _clear_skills_cache()
        self.registry.refresh()
        self._record_audit_event("skill_remove", clean_name, f"Removed skill directory: {target_dir}")

        msg = f"Skill '{clean_name}' removed successfully."
        if as_json:
            self._print_json({"success": True, "name": clean_name, "message": msg})
        else:
            self.console.print(f"[bold green]{msg}[/]")
        return 0

    # -------------------------------------------------------------------------
    # Formatting & Audit Helpers
    # -------------------------------------------------------------------------

    def _print_error(self, message: str) -> None:
        self.console.print(f"[bold red]Error:[/] {message}")

    def _print_json(self, data: Any) -> None:
        self.console.print(json.dumps(data, indent=2, default=str), soft_wrap=True, highlight=False)

    def _record_audit_event(self, operation: str, target: str, reason: str) -> None:
        if self.audit_logger is not None:
            try:
                audit_cls = None
                for mod_name, mod in sys.modules.items():
                    if "apa_security" in mod_name and hasattr(mod, "AuditEvent"):
                        audit_cls = mod.AuditEvent
                        break
                if audit_cls is None:
                    from hermes_plugins.apa_security import AuditEvent as audit_cls
                event = audit_cls(
                    tool_name="apa_skills_cli",
                    operation=operation,
                    target=target,
                    risk_level="MEDIUM" if "remove" in operation or "install" in operation else "LOW",
                    decision="ALLOW",
                    status="ok",
                    reason=reason,
                    rule_id=f"CLI_{operation.upper()}",
                )
                self.audit_logger.log_event(event)
            except Exception as exc:  # health: allow BLE001 -- fail-soft audit
                logger.debug("Failed to write audit event: %s", exc)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for 'apa skills'."""
    parser = argparse.ArgumentParser(
        prog="apa skills",
        description="APA Skills Management CLI — search, inspect, validate, configure, install, and remove skills.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Skills command to execute")

    # list
    p_list = subparsers.add_parser("list", help="List installed skills")
    p_list.add_argument("--enabled-only", action="store_true", help="Display only enabled skills")
    p_list.add_argument("--source", default="all", help="Filter by source (project, profile, custom, all)")
    p_list.add_argument("--json", action="store_true", dest="as_json", help="Output in JSON format")

    # search
    p_search = subparsers.add_parser("search", help="Search skills by name, description, tags, or category")
    p_search.add_argument("query", help="Search query string")
    p_search.add_argument("--json", action="store_true", dest="as_json", help="Output in JSON format")

    # info
    p_info = subparsers.add_parser("info", help="Display detailed skill metadata and findings")
    p_info.add_argument("name", help="Name of the skill to inspect")
    p_info.add_argument("--json", action="store_true", dest="as_json", help="Output in JSON format")

    # enable
    p_enable = subparsers.add_parser("enable", help="Enable a skill")
    p_enable.add_argument("name", help="Name of the skill to enable")
    p_enable.add_argument("--json", action="store_true", dest="as_json", help="Output in JSON format")

    # disable
    p_disable = subparsers.add_parser("disable", help="Disable a skill")
    p_disable.add_argument("name", help="Name of the skill to disable")
    p_disable.add_argument("--json", action="store_true", dest="as_json", help="Output in JSON format")

    # validate
    p_val = subparsers.add_parser("validate", help="Validate a skill's structure and security without execution")
    p_val.add_argument("name", help="Name of the skill to validate")
    p_val.add_argument("--json", action="store_true", dest="as_json", help="Output in JSON format")

    # reload
    p_reload = subparsers.add_parser("reload", help="Reload the skills registry and discovery cache")
    p_reload.add_argument("--json", action="store_true", dest="as_json", help="Output in JSON format")

    # install
    p_inst = subparsers.add_parser("install", help="Install a skill from a local path or directory")
    p_inst.add_argument("source", help="Source directory or SKILL.md file path")
    p_inst.add_argument("--name", default="", help="Override the skill name")
    p_inst.add_argument("--category", default="", help="Target category subdirectory")
    p_inst.add_argument("--force", action="store_true", help="Force overwrite if skill already exists")
    p_inst.add_argument("--json", action="store_true", dest="as_json", help="Output in JSON format")

    # remove
    p_rm = subparsers.add_parser("remove", help="Remove an installed skill")
    p_rm.add_argument("name", help="Name of the skill to remove")
    p_rm.add_argument("-y", "--yes", action="store_true", dest="skip_confirm", help="Skip confirmation prompt")
    p_rm.add_argument("--json", action="store_true", dest="as_json", help="Output in JSON format")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint for 'apa skills'."""
    if argv is None:
        argv = sys.argv[1:]

    # Strip leading 'skills' if user ran `apa skills list`
    if argv and argv[0].lower() == "skills":
        argv = argv[1:]

    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0

    args = parser.parse_args(argv)
    cli = APASkillsCLI()
    return cli.execute(args)


if __name__ == "__main__":
    sys.exit(main())
