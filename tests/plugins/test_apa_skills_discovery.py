"""Unit and integration tests for APA Skills Discovery layer (Phase 4.3)."""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from hermes_cli.plugins import PluginManifest, PluginManager


def _load_apa_skills():
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "apa-skills"
    manifest = PluginManifest(name="apa-skills", path=str(plugin_dir), key="apa-skills", source="bundled")
    return PluginManager()._load_directory_module(manifest, module_name="hermes_plugins.apa_skills_test")


apa_skills = _load_apa_skills()
APASkillsRegistry = apa_skills.APASkillsRegistry
SkillEntry = apa_skills.SkillEntry
SkillSource = apa_skills.SkillSource
SkillsDiscovery = apa_skills.SkillsDiscovery
discover_skills = apa_skills.discover_skills
get_skills_discovery = apa_skills.get_skills_discovery
list_skills_summary = apa_skills.list_skills_summary


def _create_skill(
    base_dir: Path,
    rel_path: str,
    name: str,
    description: str = "Test skill description.",
    extra_yaml: str = "",
) -> Path:
    skill_dir = base_dir / rel_path
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    extra = extra_yaml.strip() + "\n" if extra_yaml.strip() else ""
    content = f"---\nname: {name}\ndescription: {description}\n{extra}---\n# {name}\nFull instructions body here.\n"
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


def test_discovery_filtering_and_tiers(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _create_skill(root, "web/crawler", "crawler", "Web scraping skill.", extra_yaml="tags: [web, data]")
    _create_skill(root, "audio/transcriber", "transcriber", "Speech-to-text skill.", extra_yaml="tags: [audio]")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    discovery = SkillsDiscovery(reg)

    # 1. Total discovery
    all_skills = discovery.discover()
    assert len(all_skills) == 2
    names = {s.name for s in all_skills}
    assert "crawler" in names
    assert "transcriber" in names

    # 2. Filter by category
    web_skills = discovery.discover(category="web")
    assert len(web_skills) == 1
    assert web_skills[0].name == "crawler"

    # 3. Filter by source
    local_skills = discovery.discover(source=SkillSource.LOCAL)
    assert len(local_skills) == 2


def test_discovery_disabled_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "skills"
    _create_skill(root, "cat/active-skill", "active-skill", "Active skill.")
    _create_skill(root, "cat/disabled-skill", "disabled-skill", "Disabled skill.")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    monkeypatch.setattr(apa_skills.registry, "get_disabled_skill_names", lambda: {"disabled-skill"})

    discovery = SkillsDiscovery(reg)

    # By default, disabled skills are excluded
    enabled_only = discovery.discover(include_disabled=False)
    assert len(enabled_only) == 1
    assert enabled_only[0].name == "active-skill"

    # With include_disabled=True, both are returned
    all_skills = discovery.discover(include_disabled=True)
    assert len(all_skills) == 2


def test_discovery_platform_compatibility(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _create_skill(root, "tools/any-os", "any-os", "Runs everywhere.", extra_yaml="platforms: [windows, linux, macos]")
    # Deliberately incompatible platform declaration for current OS
    incompatible = "linux" if os.name == "nt" else "windows"
    _create_skill(root, "tools/other-os", "other-os", "Runs on other OS.", extra_yaml=f"platforms: [{incompatible}]")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    discovery = SkillsDiscovery(reg)

    # Compatible only (default)
    compat = discovery.discover(platform_compatible_only=True)
    assert len(compat) == 1
    assert compat[0].name == "any-os"

    # All platforms
    all_os = discovery.discover(platform_compatible_only=False)
    assert len(all_os) == 2


def test_progressive_disclosure_phase_1_summary(tmp_path: Path) -> None:
    """Verify that Phase 1 returns ONLY metadata summaries, NEVER full instructions."""
    root = tmp_path / "skills"
    _create_skill(
        root,
        "dev/linter",
        "linter",
        "Code linter.",
        extra_yaml="tags: [python, lint]\nmetadata:\n  hermes:\n    requires_tools: [terminal]\n",
    )

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    discovery = SkillsDiscovery(reg)

    summaries = discovery.list_progressive_summary()
    assert len(summaries) == 1
    s = summaries[0]

    # Required Phase 1 fields
    assert s["name"] == "linter"
    assert s["description"] == "Code linter."
    assert s["category"] == "dev"
    assert s["tags"] == ["python", "lint"]
    assert s["requires_tools"] == ["terminal"]
    assert s["is_loadable"] is True

    # Critical Prompt Cache Invariant: Full instruction body must NOT be exposed
    assert "instructions" not in s
    assert "content" not in s
    assert "Full instructions body here" not in str(s)


def test_search_roots_filesystem_inspection(tmp_path: Path) -> None:
    valid_root = tmp_path / "valid_skills"
    _create_skill(valid_root, "cat/valid-skill", "valid-skill", "Valid skill.")

    missing_md_dir = valid_root / "cat" / "empty-skill-dir"
    missing_md_dir.mkdir(parents=True, exist_ok=True)

    non_existent_root = tmp_path / "non_existent_dir"

    reg = APASkillsRegistry(custom_roots=[(2, valid_root), (3, non_existent_root)])
    discovery = SkillsDiscovery(reg)

    report = discovery.inspect_search_roots()

    assert report["total_roots"] == 2
    assert any(r["path"] == str(valid_root) for r in report["valid_roots"])
    assert any(r["path"] == str(non_existent_root) for r in report["missing_roots"])

    # Verify missing SKILL.md directory detection
    missing_paths = [Path(d["path"]).name for d in report["missing_skill_md_dirs"]]
    assert "empty-skill-dir" in missing_paths


def test_precedence_chain_resolution(tmp_path: Path) -> None:
    proj_root = tmp_path / "project_skills"
    local_root = tmp_path / "local_skills"

    _create_skill(proj_root, "testing/runner", "runner", "Project-level runner.")
    _create_skill(local_root, "tools/runner", "runner", "Local-level runner.")

    reg = APASkillsRegistry(custom_roots=[(0, proj_root), (2, local_root)])
    discovery = SkillsDiscovery(reg)

    chain = discovery.get_precedence_chain("runner")
    assert len(chain) == 2

    # Tier 0 (PROJECT) wins over Tier 2 (LOCAL)
    winner = chain[0]
    shadowed = chain[1]

    assert winner["tier"] == 0
    assert winner["is_winner"] is True
    assert winner["validation_status"] == "valid"

    assert shadowed["tier"] == 2
    assert shadowed["is_winner"] is False
    assert shadowed["validation_status"] == "shadowed"
