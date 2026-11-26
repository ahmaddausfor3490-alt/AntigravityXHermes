"""APA (Antigravity Personal Agent) CLI Root Dispatcher.

Integrates APA as a complete product using the existing Hermes CLI/TUI as its
primary user interface and Hermes Agent Runtime, with Antigravity as the AI provider
through official AGY CLI and OAuth authentication (Phase 5).
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
from pathlib import Path
import sys
from typing import Any, Sequence

# Enforce isolated HERMES_HOME and UV_PROJECT_ENVIRONMENT for APA
APA_ROOT = Path(__file__).resolve().parent
DEFAULT_APA_HERMES_HOME = (APA_ROOT / "data" / "hermes-home").resolve()
os.environ["HERMES_HOME"] = str(DEFAULT_APA_HERMES_HOME)
os.environ["UV_PROJECT_ENVIRONMENT"] = str((APA_ROOT / ".venv").resolve())

# Prepend APA bin to PATH if it exists so local uv is picked up first
apa_bin = (APA_ROOT / "bin").resolve()
if apa_bin.is_dir():
    os.environ["PATH"] = f"{apa_bin}{os.pathsep}{os.environ.get('PATH', '')}"

logger = logging.getLogger("apa")


def _load_security_module() -> Any:
    """Dynamically load the apa-security module from plugins/apa-security."""
    pkg_dir = Path(__file__).resolve().parent / "plugins" / "apa-security"
    if "hermes_plugins.apa_security" not in sys.modules:
        init_file = pkg_dir / "__init__.py"
        spec = importlib.util.spec_from_file_location("hermes_plugins.apa_security", init_file)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["hermes_plugins.apa_security"] = mod
            spec.loader.exec_module(mod)
    return sys.modules.get("hermes_plugins.apa_security")


def _load_skills_cli() -> Any:
    """Dynamically load the apa-skills CLI module from plugins/apa-skills."""
    pkg_dir = Path(__file__).resolve().parent / "plugins" / "apa-skills"
    if "hermes_plugins.apa_skills" not in sys.modules:
        init_file = pkg_dir / "__init__.py"
        spec = importlib.util.spec_from_file_location("hermes_plugins.apa_skills", init_file)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["hermes_plugins.apa_skills"] = mod
            spec.loader.exec_module(mod)
    if "hermes_plugins.apa_skills.cli" in sys.modules:
        return sys.modules["hermes_plugins.apa_skills.cli"]
    cli_file = pkg_dir / "cli.py"
    spec = importlib.util.spec_from_file_location("hermes_plugins.apa_skills.cli", cli_file)
    if spec and spec.loader:
        cli_mod = importlib.util.module_from_spec(spec)
        sys.modules["hermes_plugins.apa_skills.cli"] = cli_mod
        spec.loader.exec_module(cli_mod)
        return cli_mod
    raise ImportError("Could not load plugins/apa-skills/cli.py")


def _load_skills_registry() -> Any:
    """Dynamically load the apa-skills registry from plugins/apa-skills."""
    _load_skills_cli()
    pkg_dir = Path(__file__).resolve().parent / "plugins" / "apa-skills"
    if "hermes_plugins.apa_skills.registry" in sys.modules:
        return sys.modules["hermes_plugins.apa_skills.registry"]
    reg_file = pkg_dir / "registry.py"
    spec = importlib.util.spec_from_file_location("hermes_plugins.apa_skills.registry", reg_file)
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hermes_plugins.apa_skills.registry"] = mod
        spec.loader.exec_module(mod)
        return mod
    return None


def _cmd_auth(argv: Sequence[str]) -> int:
    """Handle ``apa auth`` commands delegating to official AGY CLI."""
    sub = argv[0].lower() if argv else "status"
    valid_actions = {"status", "login", "logout", "refresh"}
    if sub not in valid_actions:
        print(f"Unknown auth action: '{sub}'. Supported: status, login, logout, refresh", file=sys.stderr)
        return 1

    try:
        from plugins.model_providers.antigravity import AGYProcessManager, handle_antigravity_auth
    except ImportError:
        from providers import get_provider_profile
        prof = get_provider_profile("antigravity")
        if not prof or not prof.auth_handler:
            print("Error: Antigravity provider is not available.", file=sys.stderr)
            return 1
        handle_antigravity_auth = prof.auth_handler
        from plugins.model_providers.antigravity.process_manager import AGYProcessManager

    pm = AGYProcessManager()

    if sub == "status":
        status = pm.check_auth_status()
        print("=== Official Antigravity Authentication Status ===")
        print(f"  CLI Installed: {'Yes' if status.installed else 'No'}")
        if status.version:
            print(f"  CLI Version:   {status.version}")
        if status.executable:
            print(f"  Executable:    {status.executable}")
        print(f"  Authenticated: {'Yes' if status.authenticated else 'No'}")
        print(f"  Detail:        {status.detail}")
        return 0 if (status.installed and status.authenticated) else 1

    if sub == "login":
        print("Delegating interactive authentication to official AGY CLI (`agy login`)...")
        success = handle_antigravity_auth("login")
        if success:
            print("Authentication completed successfully.")
            return 0
        print("Authentication failed or was cancelled.", file=sys.stderr)
        return 1

    if sub == "logout":
        print("Delegating sign-out to official AGY CLI (`agy logout`)...")
        success = handle_antigravity_auth("logout")
        if success:
            print("Logged out successfully.")
            return 0
        print("Sign-out encountered an error.", file=sys.stderr)
        return 1

    if sub == "refresh":
        success = handle_antigravity_auth("refresh")
        print(f"Authentication active: {'Yes' if success else 'No'}")
        return 0 if success else 1

    return 0


def _cmd_status() -> int:
    """Display comprehensive APA and Hermes integration status."""
    print("=======================================================")
    print("      APA (Antigravity Personal Agent) Status          ")
    print("=======================================================")

    # 1. Antigravity Provider & ACP Status
    try:
        from providers import get_provider_profile
        prof = get_provider_profile("antigravity")
        if prof:
            setup_ok, setup_detail = prof.setup_status() or (False, "Unavailable")
            models = prof.discover_models()
            print("\n[AI Provider]")
            print(f"  Provider Name:   {prof.display_name} ({prof.name})")
            print(f"  Status:          {'READY' if setup_ok else 'NOT READY'} — {setup_detail}")
            print(f"  Default Model:   {prof.resolve_model('')}")
            print(f"  Discovered:      {len(models)} model(s) available")
        else:
            print("\n[AI Provider]\n  Status: NOT REGISTERED")
    except Exception as exc:  # health: allow BLE001 -- graceful status degradation when provider offline
        print(f"\n[AI Provider]\n  Error checking provider: {exc}")

    # 2. APA Security Guard Status
    try:
        sec_mod = _load_security_module()
        if sec_mod and hasattr(sec_mod, "APASecurityConfig"):
            sec_cfg = sec_mod.APASecurityConfig()
            print("\n[Security Guard]")
            print(f"  Enabled:         {sec_cfg.enabled}")
            print(f"  Policy Mode:     {sec_cfg.policy_mode.value.upper()}")
            print(f"  Workspace Root:  {sec_cfg.workspace_root}")
            print(f"  Audit Log:       {sec_cfg.audit_log_path}")
            print(f"  Enforce Bounds:  {sec_cfg.enforce_workspace_boundary}")
        else:
            print("\n[Security Guard]\n  Status: Offline (Module not loaded)")
    except Exception as exc:  # health: allow BLE001 -- graceful status degradation when security guard offline
        print(f"\n[Security Guard]\n  Status: Offline ({exc})")

    # 3. APA Skills Registry Status
    try:
        skills_reg_mod = _load_skills_registry()
        if skills_reg_mod and hasattr(skills_reg_mod, "get_skills_registry"):
            reg = skills_reg_mod.get_skills_registry()
            entries = reg.list_skills()
            active_count = sum(1 for e in entries if getattr(e, "enabled", True))
            print("\n[Skills Engine]")
            print(f"  Registered:      {len(entries)} skill(s)")
            print(f"  Available:       {active_count} enabled")
        else:
            print("\n[Skills Engine]\n  Status: Offline (Registry module not loaded)")
    except Exception as exc:  # health: allow BLE001 -- graceful status degradation when skills offline
        print(f"\n[Skills Engine]\n  Status: Offline ({exc})")

    # 4. Hermes Runtime Environment
    try:
        from hermes_constants import get_hermes_home
        print("\n[Hermes Agent Runtime]")
        print(f"  Home / Profile:  {get_hermes_home()}")
        print(f"  Python Runtime:  {sys.version.split()[0]} ({sys.platform})")
        print(f"  Workspace:       {Path.cwd()}")
    except Exception as exc:  # health: allow BLE001 -- graceful status degradation when runtime inspect offline
        print(f"\n[Hermes Agent Runtime]\n  Error: {exc}")

    # 5. Visual Identity & Skin
    try:
        ensure_apa_skin()
        ensure_apa_config()
        from hermes_cli.config import load_config_readonly
        from hermes_cli.skin_engine import get_active_skin, get_active_skin_name, init_skin_from_config
        cfg = load_config_readonly() or {}
        init_skin_from_config(cfg)
        skin_name = get_active_skin_name()
        active_skin = get_active_skin()
        print("\n[Visual Identity]")
        print(f"  Active Skin:     {skin_name} ({active_skin.description or 'Custom Theme'})")
        print(f"  Brand Name:      {active_skin.get_branding('agent_name', 'APA')}")
        print(f"  Prompt Symbol:   {active_skin.get_branding('prompt_symbol', 'apa >')}")
        print(f"  Theme Canvas:    {active_skin.get_color('background', '#0B0F14')}")
        print(f"  Accent Color:    {active_skin.get_color('ui_accent', '#38BDF8')}")
    except Exception as exc:  # health: allow BLE001 -- graceful status degradation when skin inspect offline
        print(f"\n[Visual Identity]\n  Status: Default ({exc})")

    print("\n=======================================================\n")
    return 0


def _cmd_resume(argv: Sequence[str]) -> int:
    """Handle ``apa resume`` by launching the AGY-styled interactive conversation picker."""
    ensure_apa_skin()
    ensure_apa_config()

    # If an explicit session target was supplied, forward directly
    if argv:
        target = argv[0].strip()
        if target and not target.startswith("-"):
            return _forward_to_hermes(["--resume", target] + list(argv[1:]))

    try:
        from hermes_cli.agy_session_picker import show_agy_session_picker

        selected_id = show_agy_session_picker(default_tab="CLI")
    except Exception as exc:
        print(f"Error launching session picker: {exc}", file=sys.stderr)
        return 1

    if not selected_id:
        print("Cancelled.")
        return 0

    print(f"Resuming conversation: {selected_id}")
    return _forward_to_hermes(["--resume", selected_id])


def ensure_apa_skin(target_home: Path | None = None) -> Path:
    """Ensure the APA skin definition is provisioned in the Hermes home skins directory."""
    from hermes_constants import get_hermes_home

    home = target_home if target_home is not None else get_hermes_home()
    skins_dir = home / "skins"
    skins_dir.mkdir(parents=True, exist_ok=True)
    target_file = skins_dir / "apa.yaml"
    source_file = Path(__file__).resolve().parent / "skins" / "apa.yaml"
    if source_file.is_file():
        if not target_file.is_file() or target_file.stat().st_mtime < source_file.stat().st_mtime:
            import shutil

            shutil.copy2(source_file, target_file)
    return target_file


def ensure_apa_config(target_home: Path | None = None) -> None:
    """Ensure display.skin defaults to 'apa' in config.yaml if not configured."""
    from hermes_constants import get_hermes_home

    home = target_home if target_home is not None else get_hermes_home()
    config_file = home / "config.yaml"
    try:
        import hermes_yaml as yaml

        data: dict[str, Any] = {}
        if config_file.is_file():
            with open(config_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            data = {}
        display = data.setdefault("display", {})
        if not isinstance(display, dict):
            display = {}
            data["display"] = display
        current_skin = display.get("skin")
        if current_skin in (None, "", "default"):
            display["skin"] = "apa"
            with open(config_file, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f)
    except Exception as exc:  # health: allow BLE001 -- graceful fallback when config not writable
        logger.debug("Could not ensure APA config: %s", exc)


def _forward_to_hermes(argv: Sequence[str]) -> int:
    """Forward command to Hermes CLI/TUI while injecting APA default provider & model."""
    ensure_apa_skin()
    ensure_apa_config()
    args = list(argv)

    # If first argument is 'tui', replace with '--tui'
    if args and args[0].lower() == "tui":
        args = ["--tui"] + args[1:]

    non_chat_subcommands = {
        "sessions", "gateway", "doctor", "config", "logs", "model", "memory",
        "tools", "cron", "mcp", "plugins", "profile", "backup", "restore",
        "setup", "update", "uninstall", "status", "version", "help"
    }

    should_inject_model = not (args and args[0].lower() in non_chat_subcommands)

    if should_inject_model:
        # Inject default provider if not specified
        if not any(flag in args for flag in ("--provider",)):
            args.extend(["--provider", "antigravity"])

        # Inject default model if not specified
        if not any(flag in args for flag in ("-m", "--model")):
            args.extend(["-m", "gemini-4-argon-medium"])

    # Prepend process name
    sys.argv = ["hermes"] + args

    try:
        from hermes_cli.main import main as hermes_main
        return hermes_main() or 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Main CLI entrypoint for APA."""
    is_direct_cli = argv is None
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="apa",
        description="APA (Antigravity Personal Agent) — Complete AI Assistant with Antigravity & Hermes Core",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="APA 1.0.0 (Phase 5 - Product Integration)",
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="APA subsystem or Hermes command")

    # apa skills ...
    subparsers.add_parser(
        "skills",
        help="Manage APA skills (list, search, info, enable, disable, validate, reload, install, remove)",
        add_help=False,
    )

    # apa auth ...
    auth_parser = subparsers.add_parser(
        "auth",
        help="Official Antigravity OAuth authentication (status, login, logout, refresh)",
    )
    auth_parser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["status", "login", "logout", "refresh"],
        help="Authentication action to perform",
    )

    # apa status
    subparsers.add_parser(
        "status",
        help="Show comprehensive APA, Antigravity provider, security guard, and runtime status",
    )

    # apa chat
    subparsers.add_parser(
        "chat",
        help="Start interactive or single-query chat session with Antigravity provider",
        add_help=False,
    )

    # apa tui
    subparsers.add_parser(
        "tui",
        help="Launch modern TUI with Antigravity provider",
        add_help=False,
    )

    # apa resume
    subparsers.add_parser(
        "resume",
        help="Interactive AGY-style conversation resume picker",
        add_help=False,
    )

    if not argv:
        if is_direct_cli:
            # Default with no arguments from CLI: start Hermes interactive chat with Antigravity provider
            return _forward_to_hermes(["chat"])
        # Programmatic call main([]): display help and return 0
        parser.print_help()
        print("\nAll standard Hermes commands (sessions, memory, model, doctor, etc.) are supported.")
        return 0

    cmd_first = argv[0].lower()

    if cmd_first in ("--version", "-v"):
        print("APA 1.0.0 (Phase 5 - Product Integration)")
        sys.exit(0)

    if cmd_first in ("--help", "-h"):
        parser.print_help()
        print("\nAll standard Hermes commands (sessions, memory, model, doctor, etc.) are supported.")
        return 0

    if cmd_first == "skills":
        cli_mod = _load_skills_cli()
        return cli_mod.main(list(argv[1:]))

    if cmd_first == "auth":
        return _cmd_auth(argv[1:])

    if cmd_first == "status":
        return _cmd_status()

    if cmd_first in ("resume", "-r", "--resume"):
        return _cmd_resume(argv[1:])

    # Pass through to native Hermes CLI for chat, tui, model, sessions, doctor, etc.
    return _forward_to_hermes(argv)


if __name__ == "__main__":
    sys.exit(main())
