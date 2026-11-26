"""Comprehensive unit and integration tests for APA Skills Registry (Phase 4.2)."""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
import textwrap
import pytest

from hermes_cli.plugins import PluginManifest, PluginManager


def _load_apa_skills():
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "apa-skills"
    manifest = PluginManifest(name="apa-skills", path=str(plugin_dir), key="apa-skills", source="bundled")
    return PluginManager()._load_directory_module(manifest, module_name="hermes_plugins.apa_skills_test")


apa_skills = _load_apa_skills()
APASkillsRegistry = apa_skills.APASkillsRegistry
RegistryStatus = apa_skills.RegistryStatus
SkillEntry = apa_skills.SkillEntry
SkillSource = apa_skills.SkillSource
ValidationStatus = apa_skills.ValidationStatus
get_skills_registry = apa_skills.get_skills_registry
reset_skills_registry = apa_skills.reset_skills_registry


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
    content = f"---\nname: {name}\ndescription: {description}\n{extra}---\n# {name}\nInstructions here.\n"
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


# 1. Registry Initialization
def test_registry_initialization() -> None:
    reg = APASkillsRegistry()
    assert reg.last_refresh == 0.0
    status = reg.get_status()
    assert isinstance(status, RegistryStatus)
    assert status.total_skills >= 0


# 2. Skill Discovery
def test_skill_discovery(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _create_skill(root, "software/lint-tool", "lint-tool", "Runs code linting.")
    _create_skill(root, "devops/deploy-app", "deploy-app", "Deploys application.")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    entries = reg.discover()

    assert len(entries) == 2
    names = {e.name for e in entries}
    assert "lint-tool" in names
    assert "deploy-app" in names

    lint = reg.get_skill("lint-tool")
    assert lint is not None
    assert lint.name == "lint-tool"
    assert lint.category == "software"
    assert lint.is_loadable is True
    assert lint.source == SkillSource.LOCAL


# 3. Metadata Extraction
def test_metadata_extraction(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    extra = textwrap.dedent("""\
        version: "2.1.0"
        author: "Agent Architect"
        tags: [analysis, data]
        metadata:
          hermes:
            requires_tools: [read_file, terminal]
    """)
    _create_skill(root, "data/data-parser", "data-parser", "Parses structured data.", extra_yaml=extra)

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    entry = reg.get_skill("data-parser")

    assert entry is not None
    assert entry.version == "2.1.0"
    assert entry.author == "Agent Architect"
    assert "analysis" in entry.tags
    assert "terminal" in entry.requires_tools
    assert entry.validation_status == ValidationStatus.VALID


# 4. Missing Optional Metadata
def test_missing_optional_metadata(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    # Bare minimum frontmatter
    _create_skill(root, "misc/minimal-skill", "minimal-skill", "Minimal skill description.")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    entry = reg.get_skill("minimal-skill")

    assert entry is not None
    assert entry.version is None
    assert entry.author is None
    assert entry.tags == ()
    assert entry.requires_tools == ()
    assert entry.is_loadable is True


# 5. Invalid Metadata Handling
def test_invalid_metadata_handling(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    bad_dir = root / "broken/bad-skill"
    bad_dir.mkdir(parents=True, exist_ok=True)
    bad_md = bad_dir / "SKILL.md"
    bad_md.write_text("No frontmatter here at all! Just raw markdown.", encoding="utf-8")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    entries = reg.discover()

    assert len(entries) == 1
    assert entries[0].validation_status == ValidationStatus.INVALID
    assert entries[0].validation_error is not None
    assert entries[0].is_loadable is False


# 6. Duplicate Detection
def test_duplicate_detection(tmp_path: Path) -> None:
    root1 = tmp_path / "root1"
    root2 = tmp_path / "root2"
    _create_skill(root1, "cat1/shared-tool", "shared-tool", "Version in root 1.")
    _create_skill(root2, "cat2/shared-tool", "shared-tool", "Version in root 2.")
    _create_skill(root1, "cat1/unique-tool", "unique-tool", "Unique tool in root 1.")

    reg = APASkillsRegistry(custom_roots=[(1, root1), (1, root2)])
    duplicates = reg.find_duplicates()

    assert "shared-tool" in duplicates
    assert len(duplicates["shared-tool"]) == 2
    assert "unique-tool" not in duplicates


# 7. Shadowed Skills Precedence (Project Tier 0 vs Local Tier 1)
def test_shadowed_skills_precedence(tmp_path: Path) -> None:
    project_root = tmp_path / "project_skills"
    local_root = tmp_path / "local_skills"

    _create_skill(project_root, "common-skill", "common-skill", "Project override version.")
    _create_skill(local_root, "common-skill", "common-skill", "Profile local version.")

    # Tier 0 (Project) beats Tier 1 (Local)
    reg = APASkillsRegistry(custom_roots=[(0, project_root), (1, local_root)])
    entries = reg.discover()

    assert len(entries) == 2
    proj_entry = next(e for e in entries if e.tier == 0)
    local_entry = next(e for e in entries if e.tier == 1)

    assert proj_entry.validation_status == ValidationStatus.VALID
    assert proj_entry.source == SkillSource.PROJECT
    assert proj_entry.is_loadable is True

    assert local_entry.validation_status == ValidationStatus.SHADOWED
    assert local_entry.is_loadable is False

    # get_skill resolves to the winning higher-tier copy
    winner = reg.get_skill("common-skill")
    assert winner is not None
    assert winner.tier == 0


# 8. Enable / Disable Status Tracking
def test_enable_disable_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "skills"
    _create_skill(root, "active/tool-active", "tool-active", "Active tool.")
    _create_skill(root, "disabled/tool-disabled", "tool-disabled", "Disabled tool.")

    # Mock get_disabled_skill_names to return tool-disabled
    monkeypatch.setattr(
        apa_skills.registry,
        "get_disabled_skill_names",
        lambda: {"tool-disabled"},
    )

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    active = reg.get_skill("tool-active")
    disabled = reg.get_skill("tool-disabled")

    assert active is not None
    assert active.enabled is True
    assert active.is_loadable is True

    assert disabled is not None
    assert disabled.enabled is False
    assert disabled.is_loadable is False

    status = reg.get_status()
    assert status.enabled_count == 1
    assert status.disabled_count == 1


# 9. Registry Refresh
def test_registry_refresh(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _create_skill(root, "cat/first-skill", "first-skill", "Initial skill.")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    assert len(reg.discover()) == 1

    # Add second skill on disk
    _create_skill(root, "cat/second-skill", "second-skill", "Dynamically added skill.")

    # Without refresh, discover returns cached list
    assert len(reg.discover()) == 1

    # Refresh picks up filesystem change
    refreshed = reg.refresh()
    assert len(refreshed) == 2
    names = {s.name for s in refreshed}
    assert "second-skill" in names


# 10. Invalid Path Validation
def test_invalid_path_validation(tmp_path: Path) -> None:
    reg = APASkillsRegistry(custom_roots=[(1, tmp_path)])

    with pytest.raises(ValueError, match="empty"):
        reg.validate_path_safety("")

    with pytest.raises(ValueError, match="Null byte"):
        reg.validate_path_safety("skill\x00path")

    with pytest.raises(ValueError, match="device namespace"):
        reg.validate_path_safety(r"\\.\PhysicalDrive0\skill")


# 11. Path Traversal & Escape Rejection
def test_path_traversal_rejection(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed_skills"
    allowed_root.mkdir()
    reg = APASkillsRegistry(custom_roots=[(1, allowed_root)])

    with pytest.raises(ValueError, match="Path traversal sequence"):
        reg.validate_path_safety(str(allowed_root / "../outside/SKILL.md"))

    outside_dir = tmp_path / "unauthorized"
    outside_dir.mkdir()
    outside_file = outside_dir / "SKILL.md"
    outside_file.write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError, match="escapes all approved skill roots"):
        reg.validate_path_safety(outside_file)


# 12. Thread-Safe Concurrent Access
def test_thread_safe_access(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    for i in range(10):
        _create_skill(root, f"cat/skill-{i}", f"skill-{i}", f"Skill description {i}.")

    reg = APASkillsRegistry(custom_roots=[(2, root)])

    def _worker(worker_id: int) -> int:
        if worker_id % 3 == 0:
            return len(reg.refresh())
        if worker_id % 2 == 0:
            skill = reg.get_skill("skill-5")
            return 1 if skill else 0
        return len(reg.list_skills())

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_worker, i) for i in range(24)]
        results = [f.result() for f in futures]

    assert all(r > 0 for r in results)


# 13. Compatibility with Hermes Catalog Resolution (Same-Tier Ambiguity Refusal)
def test_same_tier_ambiguity_refusal(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    # Two different subdirectories in the same tier declaring the same name
    _create_skill(root, "audio/transcribe", "transcribe", "Audio transcription.")
    _create_skill(root, "text/transcribe", "transcribe", "Text transcription.")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    entries = reg.discover()

    assert len(entries) == 2
    # Hermes resolve_skill_catalog flags both as ambiguous with load_name = exact relative path
    for e in entries:
        assert e.validation_status == ValidationStatus.AMBIGUOUS

    # Bare lookup by "transcribe" refuses to guess and returns None
    assert reg.get_skill("transcribe") is None

    # Explicit lookup by exact load_name succeeds
    audio_skill = reg.get_skill("audio/transcribe")
    text_skill = reg.get_skill("text/transcribe")
    assert audio_skill is not None
    assert text_skill is not None
    assert audio_skill.category == "audio"
    assert text_skill.category == "text"


# Singleton management
def test_global_singleton_management() -> None:
    reset_skills_registry()
    r1 = get_skills_registry()
    r2 = get_skills_registry()
    assert r1 is r2
    reset_skills_registry()
    r3 = get_skills_registry()
    assert r3 is not r1
    reset_skills_registry()
