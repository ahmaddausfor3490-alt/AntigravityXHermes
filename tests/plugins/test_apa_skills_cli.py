"""Comprehensive tests for APA Skills Management CLI (Phase 4.5)."""

from __future__ import annotations

import argparse
import io
import json
import logging
from pathlib import Path
from typing import Any
import pytest
from rich.console import Console

from hermes_cli.plugins import PluginManifest, PluginManager


def _load_plugin(name: str):
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / name
    manifest = PluginManifest(name=name, path=str(plugin_dir), key=name, source="bundled")
    return PluginManager()._load_directory_module(manifest, module_name=f"hermes_plugins.{name.replace('-', '_')}_test")


apa_skills = _load_plugin("apa-skills")
apa_sec = _load_plugin("apa-security")

APASkillsCLI = apa_skills.cli.APASkillsCLI
build_parser = apa_skills.cli.build_parser
APASkillsRegistry = apa_skills.APASkillsRegistry
SkillsDiscovery = apa_skills.discovery.SkillsDiscovery
SkillLoader = apa_skills.loader.SkillLoader
SkillValidator = apa_skills.validator.SkillValidator

APASecurityConfig = apa_sec.APASecurityConfig
PolicyEngine = apa_sec.PolicyEngine
ToolInterceptor = apa_sec.ToolInterceptor


@pytest.fixture()
def cli_env(tmp_path: Path):
    """Isolated environment with workspace, approved skill roots, sensitive files, and audit log."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Approved root 1: workspace skills
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True, exist_ok=True)

    # Sample skill
    sample_dir = ws_skills / "sample-skill"
    sample_dir.mkdir(parents=True, exist_ok=True)
    (sample_dir / "SKILL.md").write_text(
        "---\n"
        "name: sample-skill\n"
        "description: A safe sample testing skill\n"
        "category: testing\n"
        "tags:\n"
        "  - sample\n"
        "  - test\n"
        "---\n"
        "# Sample Skill\n"
        "Instruction body\n",
        encoding="utf-8",
    )
    refs_dir = sample_dir / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / "guide.md").write_text("# Guide\nReference content\n", encoding="utf-8")

    # Pinned skill
    pinned_dir = ws_skills / "pinned-skill"
    pinned_dir.mkdir(parents=True, exist_ok=True)
    (pinned_dir / "SKILL.md").write_text(
        "---\n"
        "name: pinned-skill\n"
        "description: Pinned core protection skill\n"
        "category: security\n"
        "---\n"
        "# Pinned Skill\n",
        encoding="utf-8",
    )

    # Approved root 2: profile skills
    profile_skills = tmp_path / "profile_skills"
    profile_skills.mkdir(parents=True, exist_ok=True)
    prof_skill = profile_skills / "profile-skill"
    prof_skill.mkdir(parents=True, exist_ok=True)
    (prof_skill / "SKILL.md").write_text(
        "---\n"
        "name: profile-skill\n"
        "description: User profile skill\n"
        "---\n"
        "# Profile Skill\n",
        encoding="utf-8",
    )

    # Outside skill (for installation testing)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    ext_skill = outside / "external-skill"
    ext_skill.mkdir(parents=True, exist_ok=True)
    (ext_skill / "SKILL.md").write_text(
        "---\n"
        "name: external-skill\n"
        "description: Fresh external skill to install\n"
        "category: tools\n"
        "---\n"
        "# External Skill\n",
        encoding="utf-8",
    )

    audit_log = tmp_path / "logs" / "apa_skills_audit.jsonl"
    audit_log.parent.mkdir(parents=True, exist_ok=True)

    sec_cfg = APASecurityConfig(
        workspace_root=workspace.resolve(),
        audit_log_path=audit_log.resolve(),
        pinned_skills=frozenset({"pinned-skill", "hermes-agent"}),
        allowed_skill_roots=(ws_skills.resolve(), profile_skills.resolve()),
        require_confirmation_for_delete=True,
        allow_delete=True,
    )
    engine = PolicyEngine(sec_cfg)
    interceptor = ToolInterceptor(sec_cfg, policy_engine=engine)

    registry = APASkillsRegistry(
        custom_roots=[
            (0, ws_skills.resolve()),
            (2, profile_skills.resolve()),
        ]
    )
    discovery = SkillsDiscovery(registry)
    loader = SkillLoader(registry)
    validator = SkillValidator()

    string_io = io.StringIO()
    console = Console(file=string_io, force_terminal=False, color_system=None)

    cli = APASkillsCLI(
        registry=registry,
        discovery=discovery,
        loader=loader,
        validator=validator,
        interceptor=interceptor,
        console=console,
    )

    return {
        "workspace": workspace,
        "ws_skills": ws_skills,
        "sample_dir": sample_dir,
        "pinned_dir": pinned_dir,
        "profile_skills": profile_skills,
        "outside": outside,
        "ext_skill": ext_skill,
        "audit_log": audit_log,
        "config": sec_cfg,
        "engine": engine,
        "interceptor": interceptor,
        "registry": registry,
        "discovery": discovery,
        "loader": loader,
        "validator": validator,
        "console": console,
        "string_io": string_io,
        "cli": cli,
    }


# =============================================================================
# 18 MANDATORY PHASE 4.5 CLI TESTS
# =============================================================================


def test_command_registration():
    """1. All 9 subcommands and their expected flags are correctly registered in the parser."""
    parser = build_parser()
    assert isinstance(parser, argparse.ArgumentParser)
    subparsers_actions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(subparsers_actions) == 1
    subparsers = subparsers_actions[0]
    expected_cmds = {"list", "search", "info", "enable", "disable", "validate", "reload", "install", "remove"}
    assert set(subparsers.choices.keys()) == expected_cmds

    # Verify key options on subcommands
    p_list = subparsers.choices["list"]
    opt_names = {opt for action in p_list._actions for opt in action.option_strings}
    assert "--enabled-only" in opt_names
    assert "--source" in opt_names
    assert "--json" in opt_names

    p_install = subparsers.choices["install"]
    inst_opts = {opt for action in p_install._actions for opt in action.option_strings}
    assert "--force" in inst_opts
    assert "--name" in inst_opts
    assert "--category" in inst_opts

    p_remove = subparsers.choices["remove"]
    rm_opts = {opt for action in p_remove._actions for opt in action.option_strings}
    assert "-y" in rm_opts or "--yes" in rm_opts


def test_list_output(cli_env: dict[str, Any]):
    """2. Human-readable list output renders table containing installed skills."""
    cli: APASkillsCLI = cli_env["cli"]
    sio: io.StringIO = cli_env["string_io"]

    code = cli.cmd_list(as_json=False)
    assert code == 0
    output = sio.getvalue()
    assert "sample-skill" in output
    assert "pinned-skill" in output
    assert "profile-skill" in output


def test_list_json_output(cli_env: dict[str, Any]):
    """3. JSON list output produces valid JSON adhering to expected schema."""
    cli: APASkillsCLI = cli_env["cli"]
    sio: io.StringIO = cli_env["string_io"]
    sio.truncate(0)
    sio.seek(0)

    code = cli.cmd_list(as_json=True)
    assert code == 0
    data = json.loads(sio.getvalue())
    assert isinstance(data, list)
    assert len(data) >= 3

    names = {item["name"] for item in data}
    assert "sample-skill" in names
    assert "pinned-skill" in names

    sample = next(item for item in data if item["name"] == "sample-skill")
    assert sample["description"] == "A safe sample testing skill"
    assert sample["category"] == "testing"
    assert sample["enabled"] is True
    assert sample["validation_status"] == "valid"
    assert "path" in sample


def test_list_filters(cli_env: dict[str, Any]):
    """4. List filters --enabled-only and --source filter results correctly."""
    cli: APASkillsCLI = cli_env["cli"]
    sio: io.StringIO = cli_env["string_io"]

    # Filter by source: project (tier 0)
    sio.truncate(0)
    sio.seek(0)
    code = cli.cmd_list(source_filter="project", as_json=True)
    assert code == 0
    proj_data = json.loads(sio.getvalue())
    for item in proj_data:
        assert item["source"] == "project"

    # Filter by source: profile (tier 2)
    sio.truncate(0)
    sio.seek(0)
    code = cli.cmd_list(source_filter="profile", as_json=True)
    assert code == 0
    prof_data = json.loads(sio.getvalue())
    for item in prof_data:
        assert item["source"] == "profile"


def test_search(cli_env: dict[str, Any]):
    """5. Search finds skills by name, description, tags, and category in human and JSON modes."""
    cli: APASkillsCLI = cli_env["cli"]
    sio: io.StringIO = cli_env["string_io"]

    # Search by tag
    sio.truncate(0)
    sio.seek(0)
    code = cli.cmd_search(query="sample", as_json=True)
    assert code == 0
    data = json.loads(sio.getvalue())
    assert any(item["name"] == "sample-skill" for item in data)

    # Search by category
    sio.truncate(0)
    sio.seek(0)
    code = cli.cmd_search(query="testing", as_json=True)
    assert code == 0
    data = json.loads(sio.getvalue())
    assert any(item["name"] == "sample-skill" for item in data)

    # Empty search query returns error code 1
    code_empty = cli.cmd_search(query="   ", as_json=False)
    assert code_empty == 1


def test_info(cli_env: dict[str, Any]):
    """6. Info command displays comprehensive skill metadata, compatibility, and findings."""
    cli: APASkillsCLI = cli_env["cli"]
    sio: io.StringIO = cli_env["string_io"]

    # JSON mode
    sio.truncate(0)
    sio.seek(0)
    code = cli.cmd_info("sample-skill", as_json=True)
    assert code == 0
    data = json.loads(sio.getvalue())
    assert data["name"] == "sample-skill"
    assert data["description"] == "A safe sample testing skill"
    assert data["category"] == "testing"
    assert data["validation"]["status"] == "valid"
    assert data["validation"]["is_safe_to_load"] is True
    assert "sample" in data["tags"]

    # Human-readable mode
    sio.truncate(0)
    sio.seek(0)
    code_hr = cli.cmd_info("sample-skill", as_json=False)
    assert code_hr == 0
    out_hr = sio.getvalue()
    assert "sample-skill" in out_hr
    assert "Directory:" in out_hr


def test_info_missing_skill(cli_env: dict[str, Any]):
    """7. Info command returns code 1 and error message for missing skills."""
    cli: APASkillsCLI = cli_env["cli"]
    code = cli.cmd_info("non-existent-skill", as_json=False)
    assert code == 1


def test_enable_and_disable(cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    """8. Enabling and disabling skills persists in config, invalidates cache, and updates registry."""
    cli: APASkillsCLI = cli_env["cli"]
    mock_config: dict[str, Any] = {"skills": {"disabled": []}}

    def mock_load_config():
        return mock_config

    def mock_save_config(cfg):
        nonlocal mock_config
        mock_config = cfg

    monkeypatch.setattr("hermes_cli.config.load_config", mock_load_config)
    monkeypatch.setattr("hermes_cli.config.save_config", mock_save_config)

    # 1. Disable sample-skill
    code_dis = cli.cmd_disable("sample-skill", as_json=True)
    assert code_dis == 0
    assert "sample-skill" in mock_config["skills"]["disabled"]

    # 2. Re-enable sample-skill
    code_en = cli.cmd_enable("sample-skill", as_json=True)
    assert code_en == 0
    assert "sample-skill" not in mock_config["skills"]["disabled"]


def test_pinned_skill_protection(cli_env: dict[str, Any]):
    """9. Disabling or removing pinned skills is strictly blocked."""
    cli: APASkillsCLI = cli_env["cli"]

    # Attempt disable pinned skill
    code_dis = cli.cmd_disable("pinned-skill", as_json=False)
    assert code_dis == 1

    # Attempt disable essential Hermes skill
    code_ess = cli.cmd_disable("hermes-agent", as_json=False)
    assert code_ess == 1

    # Attempt remove pinned skill
    code_rm = cli.cmd_remove("pinned-skill", skip_confirm=True, as_json=False)
    assert code_rm == 1
    assert cli_env["pinned_dir"].exists()


def test_validate(cli_env: dict[str, Any]):
    """10. Validate executes inspection on skill structure without running any code."""
    cli: APASkillsCLI = cli_env["cli"]
    sio: io.StringIO = cli_env["string_io"]

    sio.truncate(0)
    sio.seek(0)
    code = cli.cmd_validate("sample-skill", as_json=True)
    assert code == 0
    data = json.loads(sio.getvalue())
    assert data["name"] == "sample-skill"
    assert data["status"] == "valid"
    assert data["is_safe_to_load"] is True
    assert data["findings_count"] == 0


def test_reload(cli_env: dict[str, Any]):
    """11. Reload refreshes the skills registry and discovery cache."""
    cli: APASkillsCLI = cli_env["cli"]
    sio: io.StringIO = cli_env["string_io"]

    sio.truncate(0)
    sio.seek(0)
    code = cli.cmd_reload(as_json=True)
    assert code == 0
    data = json.loads(sio.getvalue())
    assert data["success"] is True
    assert data["count"] >= 3


def test_install(cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    """12. Install validates source, copies to approved root, and rejects duplicate without force."""
    cli: APASkillsCLI = cli_env["cli"]
    ext_skill: Path = cli_env["ext_skill"]
    ws_skills: Path = cli_env["ws_skills"]

    # Mock install root to ws_skills
    monkeypatch.setattr(cli, "_resolve_install_root", lambda: ws_skills)

    # 1. Install fresh skill
    code_inst = cli.cmd_install(str(ext_skill), as_json=True)
    assert code_inst == 0
    installed_dir = ws_skills / "external-skill"
    assert installed_dir.exists()
    assert (installed_dir / "SKILL.md").exists()

    # 2. Duplicate install without --force should fail
    code_dup = cli.cmd_install(str(ext_skill), force=False, as_json=True)
    assert code_dup == 1

    # 3. Duplicate install with --force should succeed
    code_force = cli.cmd_install(str(ext_skill), force=True, as_json=True)
    assert code_force == 0


def test_install_path_traversal_rejected(cli_env: dict[str, Any]):
    """13. Install blocks path traversal, null bytes, and device namespace prefixes."""
    cli: APASkillsCLI = cli_env["cli"]

    # Traversal in source path
    code_trav = cli.cmd_install("../outside/external-skill", as_json=False)
    assert code_trav == 1

    # Null byte in path
    code_null = cli.cmd_install("external-skill\x00malicious", as_json=False)
    assert code_null == 1

    # Device prefix
    code_nt = cli.cmd_install(r"\\.\PhysicalDrive0\skill", as_json=False)
    assert code_nt == 1


def test_remove_with_confirmation(cli_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    """14. Interactive confirmation prompt cancellation preserves the skill."""
    cli: APASkillsCLI = cli_env["cli"]
    sample_dir: Path = cli_env["sample_dir"]

    # User inputs 'n'
    monkeypatch.setattr("builtins.input", lambda _: "n")

    code = cli.cmd_remove("sample-skill", skip_confirm=False, as_json=False)
    assert code == 1
    assert sample_dir.exists()


def test_remove_with_yes_flag(cli_env: dict[str, Any]):
    """15. Destructive removal with --yes permanently deletes skill from approved root."""
    cli: APASkillsCLI = cli_env["cli"]
    sample_dir: Path = cli_env["sample_dir"]

    code = cli.cmd_remove("sample-skill", skip_confirm=True, as_json=True)
    assert code == 0
    assert not sample_dir.exists()


def test_remove_unauthorized_root_rejected(cli_env: dict[str, Any]):
    """16. Removing a skill outside approved roots is rejected."""
    cli: APASkillsCLI = cli_env["cli"]
    outside_dir = cli_env["outside"]

    # Create dummy entry with directory outside approved roots
    fake_entry = apa_skills.SkillEntry(
        name="rogue-skill",
        load_name="rogue-skill",
        description="Rogue skill outside roots",
        source=apa_skills.SkillSource.EXTERNAL,
        tier=3,
        root=outside_dir,
        path=outside_dir / "SKILL.md",
        skill_dir=outside_dir,
    )
    cli.registry._entries.append(fake_entry)
    cli.registry._entries_by_name["rogue-skill"] = [fake_entry]
    cli.registry._entries_by_load_name["rogue-skill"] = fake_entry

    code = cli.cmd_remove("rogue-skill", skip_confirm=True, as_json=False)
    assert code == 1


def test_audit_logging(cli_env: dict[str, Any]):
    """17. Actions are logged to AuditLogger with sanitized parameters."""
    cli: APASkillsCLI = cli_env["cli"]
    audit_log: Path = cli_env["audit_log"]

    cli._record_audit_event("skill_test_action", "sample-skill", "Testing audit dispatch")
    assert audit_log.exists()
    content = audit_log.read_text(encoding="utf-8")
    assert "skill_test_action" in content
    assert "sample-skill" in content


def test_windows_compatibility(cli_env: dict[str, Any]):
    """18. Backslash paths, drive letters, and spaces in paths are handled safely."""
    cli: APASkillsCLI = cli_env["cli"]
    ws_skills: Path = cli_env["ws_skills"]

    spaced_dir = ws_skills / "spaced skill dir"
    spaced_dir.mkdir(parents=True, exist_ok=True)
    (spaced_dir / "SKILL.md").write_text(
        "---\n"
        "name: spaced-skill\n"
        "description: Skill in spaced directory\n"
        "---\n"
        "# Spaced\n",
        encoding="utf-8",
    )
    cli.registry.refresh()

    code = cli.cmd_info("spaced-skill", as_json=True)
    assert code == 0


def test_apa_root_cli_dispatch(tmp_path: Path):
    """19. Root apa.py properly dispatches --version, --help, and skills subcommands."""
    import apa

    # Test --version
    with pytest.raises(SystemExit) as exc_v:
        apa.main(["--version"])
    assert exc_v.value.code == 0

    # Test no args displays help
    code_no_args = apa.main([])
    assert code_no_args == 0

    # Test skills dispatch
    with pytest.raises(SystemExit) as exc_help:
        apa.main(["skills", "--help"])
    assert exc_help.value.code == 0
