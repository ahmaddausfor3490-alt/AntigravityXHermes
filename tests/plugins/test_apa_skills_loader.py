"""Unit and integration tests for APA Skills Loader (Phase 4.3)."""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
import pytest

from hermes_cli.plugins import PluginManifest, PluginManager


def _load_apa_skills():
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "apa-skills"
    manifest = PluginManifest(name="apa-skills", path=str(plugin_dir), key="apa-skills", source="bundled")
    return PluginManager()._load_directory_module(manifest, module_name="hermes_plugins.apa_skills_test")


apa_skills = _load_apa_skills()
APASkillsRegistry = apa_skills.APASkillsRegistry
AmbiguousSkillError = apa_skills.AmbiguousSkillError
LoadedFile = apa_skills.LoadedFile
LoadedSkill = apa_skills.LoadedSkill
SkillLoader = apa_skills.SkillLoader
SkillNotFoundError = apa_skills.SkillNotFoundError
SkillSecurityError = apa_skills.SkillSecurityError


def _create_skill_bundle(
    base_dir: Path,
    rel_path: str,
    name: str,
    instructions: str = "Standard operational guide.",
    references: dict[str, str] | None = None,
    assets: dict[str, bytes] | None = None,
) -> Path:
    skill_dir = base_dir / rel_path
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_md = skill_dir / "SKILL.md"
    content = f"---\nname: {name}\ndescription: Test skill {name}.\n---\n# {name}\n\n{instructions}\n"
    skill_md.write_text(content, encoding="utf-8")

    if references:
        ref_dir = skill_dir / "references"
        ref_dir.mkdir(parents=True, exist_ok=True)
        for fname, text in references.items():
            (ref_dir / fname).write_text(text, encoding="utf-8")

    if assets:
        asset_dir = skill_dir / "assets"
        asset_dir.mkdir(parents=True, exist_ok=True)
        for fname, data in assets.items():
            (asset_dir / fname).write_bytes(data)

    return skill_dir


def test_on_demand_skill_loading(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _create_skill_bundle(
        root,
        "dev/code-review",
        "code-review",
        instructions="Perform code review according to clean architecture.",
        references={"checklist.md": "- Check imports\n- Check security\n"},
    )

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    loader = SkillLoader(reg)

    loaded = loader.load_skill("code-review")

    assert isinstance(loaded, LoadedSkill)
    assert loaded.name == "code-review"
    assert "Perform code review" in loaded.instructions
    assert "references" in loaded.linked_files
    assert "references/checklist.md" in loaded.linked_files["references"]


def test_load_linked_files_text_and_binary(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _create_skill_bundle(
        root,
        "media/editor",
        "editor",
        references={"guide.md": "# Editing Guide\nStep 1: Open file."},
        assets={"icon.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"},
    )

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    loader = SkillLoader(reg)

    # 1. Text linked file
    text_file = loader.load_linked_file("editor", "references/guide.md")
    assert isinstance(text_file, LoadedFile)
    assert text_file.is_binary is False
    assert "# Editing Guide" in text_file.content

    # 2. Binary linked file
    bin_file = loader.load_linked_file("editor", "assets/icon.png")
    assert isinstance(bin_file, LoadedFile)
    assert bin_file.is_binary is True
    assert bin_file.size_bytes == 16


def test_ambiguous_name_refusal_and_disambiguation(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _create_skill_bundle(root, "audio/transcribe", "transcribe", instructions="Audio transcriber.")
    _create_skill_bundle(root, "video/transcribe", "transcribe", instructions="Video transcriber.")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    loader = SkillLoader(reg)

    # Bare name lookup must fail due to ambiguity
    with pytest.raises(AmbiguousSkillError) as exc_info:
        loader.load_skill("transcribe")
    assert "ambiguous" in str(exc_info.value).lower()

    # Qualified path lookup must succeed unambiguously
    loaded_audio = loader.load_skill("audio/transcribe")
    assert loaded_audio.name == "transcribe"
    assert "Audio transcriber" in loaded_audio.instructions

    loaded_video = loader.load_skill("video/transcribe")
    assert loaded_video.name == "transcribe"
    assert "Video transcriber" in loaded_video.instructions


def test_reload_behavior(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_dir = _create_skill_bundle(root, "ops/deploy", "deploy", instructions="Initial v1 guide.")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    loader = SkillLoader(reg)

    # Initial load
    v1 = loader.load_skill("deploy")
    assert "Initial v1 guide" in v1.instructions

    # Modify file on disk
    (skill_dir / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Updated deploy.\n---\n# deploy\nUpdated v2 guide.\n",
        encoding="utf-8",
    )

    # Reload skill
    v2 = loader.reload_skill("deploy")
    assert "Updated v2 guide" in v2.instructions


def test_path_traversal_and_device_rejection(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _create_skill_bundle(root, "sec/checker", "checker")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    loader = SkillLoader(reg)

    # 1. Traversal in skill name
    with pytest.raises(SkillSecurityError):
        loader.load_skill("../outside")

    # 2. Traversal in linked file
    with pytest.raises(SkillSecurityError):
        loader.load_linked_file("checker", "../../../secret.txt")

    # 3. Windows NT device namespace
    with pytest.raises(SkillSecurityError):
        loader.load_linked_file("checker", r"\\.\PhysicalDrive0")

    # 4. Absolute drive paths
    with pytest.raises(SkillSecurityError):
        loader.load_linked_file("checker", r"C:\Windows\System32\cmd.exe")


def test_sensitive_file_protection(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_dir = _create_skill_bundle(root, "sec/sensitive", "sensitive")

    # Drop an illegal .env into references
    ref_dir = skill_dir / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / ".env").write_text("SECRET=my_key\n", encoding="utf-8")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    loader = SkillLoader(reg)

    with pytest.raises(SkillSecurityError) as exc_info:
        loader.load_linked_file("sensitive", "references/.env")
    assert "sensitive file" in str(exc_info.value).lower()


def test_missing_files_error_handling(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _create_skill_bundle(root, "tools/helper", "helper")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    loader = SkillLoader(reg)

    # Non-existent skill
    with pytest.raises(SkillNotFoundError):
        loader.load_skill("ghost-skill")

    # Non-existent linked file
    with pytest.raises(SkillNotFoundError):
        loader.load_linked_file("helper", "references/missing_file.md")


def test_thread_safe_concurrent_loading(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _create_skill_bundle(root, "perf/tester", "tester", instructions="Thread testing instructions.")

    reg = APASkillsRegistry(custom_roots=[(2, root)])
    loader = SkillLoader(reg)

    def _worker(worker_id: int) -> str:
        if worker_id % 3 == 0:
            loader.clear_cache()
        loaded = loader.load_skill("tester")
        return loaded.instructions

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_worker, i) for i in range(24)]
        results = [f.result() for f in futures]

    assert len(results) == 24
    assert all("Thread testing instructions" in r for r in results)
