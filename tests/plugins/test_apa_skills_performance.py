"""Repeatable performance benchmarks and reliability stress metrics for APA Skills (Phase 4.7).

Measures:
- Skill discovery latency (cold vs warm).
- Registry refresh latency.
- Skill loading latency (SKILL.md + frontmatter).
- Reference file loading latency.
- Concurrent discovery behavior (multi-threaded load).
- Memory delta.
- Cache effectiveness.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
import time
from typing import Any

import pytest

from hermes_cli.plugins import PluginManifest, PluginManager


def _load_plugin(name: str):
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / name
    manifest = PluginManifest(name=name, path=str(plugin_dir), key=name, source="bundled")
    return PluginManager()._load_directory_module(manifest, module_name=f"hermes_plugins.{name.replace('-', '_')}_perf")


apa_skills = _load_plugin("apa-skills")

APASkillsRegistry = apa_skills.APASkillsRegistry
SkillsDiscovery = apa_skills.discovery.SkillsDiscovery
SkillLoader = apa_skills.loader.SkillLoader
SkillValidator = apa_skills.validator.SkillValidator


@pytest.fixture()
def perf_env(tmp_path: Path):
    """Create test skills tree with multiple skills and references."""
    root = tmp_path / "skills"
    root.mkdir(parents=True, exist_ok=True)

    # Populate 25 skills to simulate a realistic skills repository
    for i in range(25):
        s_dir = root / f"perf-skill-{i:02d}"
        s_dir.mkdir(parents=True, exist_ok=True)
        (s_dir / "SKILL.md").write_text(
            f"---\nname: perf-skill-{i:02d}\ndescription: Benchmark skill number {i}\ncategory: benchmark\ntags:\n  - perf\n  - test\n---\n# Perf Skill {i}\nOperational details.\n",
            encoding="utf-8",
        )
        refs = s_dir / "references"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / "guide.md").write_text(f"# Guide {i}\nDetailed references for skill {i}.\n", encoding="utf-8")

    registry = APASkillsRegistry(custom_roots=[(0, root.resolve())])
    discovery = SkillsDiscovery(registry=registry)
    loader = SkillLoader(registry=registry)
    validator = SkillValidator()

    return {
        "root": root,
        "registry": registry,
        "discovery": discovery,
        "loader": loader,
        "validator": validator,
    }


def test_perf_skill_discovery_and_refresh_latency(perf_env: dict[str, Any]):
    """Measure cold vs warm discovery and registry refresh latency."""
    registry: APASkillsRegistry = perf_env["registry"]

    # Cold refresh
    t0 = time.perf_counter()
    cold_entries = registry.refresh()
    t_cold = (time.perf_counter() - t0) * 1000.0  # ms
    assert len(cold_entries) == 25

    # Warm refresh (10 runs averaged)
    warm_times = []
    for _ in range(10):
        t_start = time.perf_counter()
        entries = registry.refresh()
        warm_times.append((time.perf_counter() - t_start) * 1000.0)
        assert len(entries) == 25

    avg_warm = sum(warm_times) / len(warm_times)

    print(f"\n[PERF] Discovery Cold Refresh Latency: {t_cold:.3f} ms")
    print(f"[PERF] Discovery Warm Refresh Average: {avg_warm:.3f} ms (min: {min(warm_times):.3f} ms, max: {max(warm_times):.3f} ms)")

    # Assert reasonable performance bounds (< 3500ms on Windows cold temp dir, < 200ms warm)
    assert t_cold < 3500.0
    assert avg_warm < 200.0


def test_perf_skill_loading_and_reference_latency(perf_env: dict[str, Any]):
    """Measure loading latency of SKILL.md and referenced sub-documents."""
    loader: SkillLoader = perf_env["loader"]
    skill_name = "perf-skill-00"

    # SKILL.md loading latency (100 iterations)
    skill_times = []
    for _ in range(100):
        t0 = time.perf_counter()
        doc = loader.load_skill(skill_name)
        skill_times.append((time.perf_counter() - t0) * 1000.0)
        assert doc.name == "perf-skill-00"

    avg_skill_load = sum(skill_times) / len(skill_times)

    # Reference file loading latency (100 iterations)
    ref_times = []
    for _ in range(100):
        t0 = time.perf_counter()
        loaded_file = loader.load_linked_file(skill_name, "references/guide.md")
        ref_times.append((time.perf_counter() - t0) * 1000.0)
        assert "Guide 0" in loaded_file.content

    avg_ref_load = sum(ref_times) / len(ref_times)

    print(f"\n[PERF] SKILL.md Load Average: {avg_skill_load:.3f} ms (p95: {sorted(skill_times)[95]:.3f} ms)")
    print(f"[PERF] Reference File Load Average: {avg_ref_load:.3f} ms (p95: {sorted(ref_times)[95]:.3f} ms)")

    assert avg_skill_load < 50.0  # Fast sub-50ms loading
    assert avg_ref_load < 50.0


def test_perf_concurrent_discovery(perf_env: dict[str, Any]):
    """Measure concurrent discovery throughput across 10 simultaneous threads."""
    registry: APASkillsRegistry = perf_env["registry"]

    def worker(worker_id: int) -> int:
        entries = registry.refresh()
        return len(entries)

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker, i) for i in range(20)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    total_time = (time.perf_counter() - t0) * 1000.0
    assert all(r == 25 for r in results)

    print(f"\n[PERF] 20 Concurrent Discovery Runs (10 threads): {total_time:.3f} ms total")
    assert total_time < 2000.0  # Under 2 seconds for 20 thread operations


def test_perf_memory_usage_and_scale(tmp_path: Path):
    """Measure memory footprint when scaling to 100 skills."""
    import tracemalloc

    tracemalloc.start()
    snapshot_before = tracemalloc.take_snapshot()

    scale_root = tmp_path / "scale_skills"
    scale_root.mkdir(parents=True, exist_ok=True)
    for i in range(100):
        s_dir = scale_root / f"scale-skill-{i:03d}"
        s_dir.mkdir(parents=True, exist_ok=True)
        (s_dir / "SKILL.md").write_text(
            f"---\nname: scale-skill-{i:03d}\ndescription: Scaling benchmark skill {i}\n---\n# Scale {i}\n",
            encoding="utf-8",
        )

    scale_registry = APASkillsRegistry(custom_roots=[(0, scale_root.resolve())])
    entries = scale_registry.refresh()
    assert len(entries) == 100

    snapshot_after = tracemalloc.take_snapshot()
    tracemalloc.stop()

    top_stats = snapshot_after.compare_to(snapshot_before, "lineno")
    total_delta_kb = sum(stat.size_diff for stat in top_stats) / 1024.0

    print(f"\n[PERF] Memory Delta for 100 Skills: {total_delta_kb:.2f} KB (Avg: {total_delta_kb/100:.2f} KB/skill)")
    # Memory footprint should be modest (< 5 MB for 100 skills)
    assert total_delta_kb < 5120.0
