"""Tests for APA Phase 5.5: CLI Visual Customization & Minimalist Professional Identity.

Validates that:
1. APA skin definition (`skins/apa.yaml`) is well-formed, complete, and conforms to Minimalist Professional specs.
2. Hermes Skin Engine natively loads and applies the skin without modifying Hermes core.
3. Color palette adheres to:
   - Canvas / Background: #0B0F14
   - Primary text: #E6EDF3
   - Accent: #38BDF8
   - Success: #22C55E
   - Warning: #F59E0B
   - Error: #EF4444
4. Branding tokens adhere to:
   - Product: APA — Antigravity Personal Agent
   - Subtitle: Powered by Hermes
   - Prompt: apa >
   - Response header: " APA "
5. Hero card, ASCII logo, modern tool prefix ('│'), and professional spinner verbs are present.
6. Banner rendering and prompt symbol resolution cleanly reflect APA identity.
7. Zero modifications made to Hermes Core files.
"""

from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

import apa
from hermes_cli.banner import build_welcome_banner
from hermes_cli.cli_render import _build_compact_banner
from hermes_cli.skin_engine import (
    SkinConfig,
    _build_skin_config,
    _load_skin_from_yaml,
    get_active_prompt_symbol,
    get_active_skin,
    get_active_skin_name,
    get_prompt_toolkit_style_overrides,
    init_skin_from_config,
    load_skin,
    set_active_skin,
)
import hermes_yaml as yaml


@pytest.fixture(autouse=True)
def _restore_active_skin():
    """Ensure the skin is cleanly reset after each test."""
    initial_skin = get_active_skin_name()
    yield
    try:
        set_active_skin(initial_skin or "default")
    except Exception:
        pass


# =============================================================================
# 1. Skin YAML File Structure & Syntax
# =============================================================================


def test_01_apa_skin_yaml_validity():
    """1. skins/apa.yaml exists and parses into a valid SkinConfig."""
    repo_root = Path(__file__).resolve().parents[2]
    skin_file = repo_root / "skins" / "apa.yaml"
    assert skin_file.is_file(), f"Expected skin definition at {skin_file}"

    with open(skin_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert isinstance(data, dict)
    assert data.get("name") == "apa"
    assert "description" in data
    assert "colors" in data
    assert "light_colors" in data
    assert "branding" in data
    assert "spinner" in data
    assert "tool_prefix" in data
    assert "banner_hero" in data
    assert "banner_logo" in data

    config = _build_skin_config(data)
    assert isinstance(config, SkinConfig)
    assert config.name == "apa"


# =============================================================================
# 2. Palette Specification Compliance
# =============================================================================


def test_02_minimalist_professional_palette():
    """2. Palette matches the required Minimalist Professional specifications."""
    repo_root = Path(__file__).resolve().parents[2]
    skin_file = repo_root / "skins" / "apa.yaml"
    data = _load_skin_from_yaml(skin_file)
    assert data is not None
    cfg = _build_skin_config(data)

    # Core background / canvas
    assert cfg.get_color("background").upper() == "#0B0F14"
    assert cfg.get_color("status_bar_bg").upper() == "#0B0F14"
    assert cfg.get_color("completion_menu_bg").upper() == "#0B0F14"

    # Primary text & typography
    assert cfg.get_color("banner_text").upper() == "#E6EDF3"
    assert cfg.get_color("ui_text").upper() == "#E6EDF3"
    assert cfg.get_color("status_bar_text").upper() == "#E6EDF3"

    # Accent (Sky Blue)
    assert cfg.get_color("banner_accent").upper() == "#38BDF8"
    assert cfg.get_color("ui_accent").upper() == "#38BDF8"
    assert cfg.get_color("banner_title").upper() == "#38BDF8"
    assert cfg.get_color("prompt").upper() == "#38BDF8"
    assert cfg.get_color("response_border").upper() == "#38BDF8"

    # Semantic states
    assert cfg.get_color("ui_ok").upper() == "#22C55E"
    assert cfg.get_color("status_bar_good").upper() == "#22C55E"
    assert cfg.get_color("ui_warn").upper() == "#F59E0B"
    assert cfg.get_color("status_bar_warn").upper() == "#F59E0B"
    assert cfg.get_color("ui_error").upper() == "#EF4444"
    assert cfg.get_color("status_bar_critical").upper() == "#EF4444"


# =============================================================================
# 3. Branding Tokens
# =============================================================================


def test_03_branding_tokens():
    """3. Branding tokens align with APA product identity."""
    repo_root = Path(__file__).resolve().parents[2]
    skin_file = repo_root / "skins" / "apa.yaml"
    data = _load_skin_from_yaml(skin_file)
    assert data is not None
    cfg = _build_skin_config(data)

    assert cfg.get_branding("agent_name") == "APA — Antigravity Personal Agent"
    assert "APA (Antigravity Personal Agent)" in cfg.get_branding("welcome")
    assert "Powered by Hermes" in cfg.get_branding("welcome")
    assert cfg.get_branding("goodbye") == "Goodbye from APA!"
    assert cfg.get_branding("response_label") == " APA "
    assert cfg.get_branding("prompt_symbol") == "apa >"
    assert cfg.get_branding("help_header") == "(APA) Available Commands"


# =============================================================================
# 4. Tool Prefix & Spinner Customization
# =============================================================================


def test_04_spinner_and_tool_prefix():
    """4. Tool prefix uses modern solid line and spinner contains professional verbs."""
    repo_root = Path(__file__).resolve().parents[2]
    skin_file = repo_root / "skins" / "apa.yaml"
    data = _load_skin_from_yaml(skin_file)
    assert data is not None
    cfg = _build_skin_config(data)

    assert cfg.tool_prefix == "│"

    verbs = cfg.spinner.get("thinking_verbs", [])
    assert "analyzing" in verbs
    assert "synthesizing" in verbs
    assert "reasoning" in verbs

    wings = cfg.get_spinner_wings()
    assert len(wings) >= 1
    assert any(w == ("[", "]") for w in wings) or any(w == ("⟦", "⟧") for w in wings)


# =============================================================================
# 5. Banner Hero Card & ASCII Logo
# =============================================================================


def test_05_hero_and_logo_art():
    """5. Banner hero card and ASCII logo are populated with APA branding."""
    repo_root = Path(__file__).resolve().parents[2]
    skin_file = repo_root / "skins" / "apa.yaml"
    data = _load_skin_from_yaml(skin_file)
    assert data is not None
    cfg = _build_skin_config(data)

    assert cfg.banner_hero, "Expected non-empty banner_hero"
    assert "Antigravity Agent" in cfg.banner_hero
    assert "Powered by Hermes" in cfg.banner_hero

    assert cfg.banner_logo, "Expected non-empty banner_logo"
    assert "Antigravity Personal Agent" in cfg.banner_logo


# =============================================================================
# 6. Dynamic Provisioning into Isolated HERMES_HOME
# =============================================================================


def test_06_provisioning_into_isolated_hermes_home(tmp_path: Path):
    """6. ensure_apa_skin provisions skins/apa.yaml into any profile HERMES_HOME."""
    target_skin = apa.ensure_apa_skin(target_home=tmp_path)
    assert target_skin.is_file()
    assert target_skin.parent == tmp_path / "skins"
    assert target_skin.name == "apa.yaml"

    # Verify content match
    repo_skin = Path(__file__).resolve().parents[2] / "skins" / "apa.yaml"
    assert target_skin.read_text(encoding="utf-8") == repo_skin.read_text(encoding="utf-8")


# =============================================================================
# 7. Config Provisioning
# =============================================================================


def test_07_config_provisioning(tmp_path: Path):
    """7. ensure_apa_config sets display.skin to 'apa' without clobbering user choice."""
    config_file = tmp_path / "config.yaml"

    # Case 1: Fresh config
    apa.ensure_apa_config(target_home=tmp_path)
    with open(config_file, "r", encoding="utf-8") as f:
        cfg1 = yaml.safe_load(f)
    assert cfg1.get("display", {}).get("skin") == "apa"

    # Case 2: User explicitly set another skin (e.g. 'mono')
    cfg1["display"]["skin"] = "mono"
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg1, f)

    apa.ensure_apa_config(target_home=tmp_path)
    with open(config_file, "r", encoding="utf-8") as f:
        cfg2 = yaml.safe_load(f)
    assert cfg2.get("display", {}).get("skin") == "mono"


# =============================================================================
# 8. Interactive Prompt Symbol Resolution
# =============================================================================


def test_08_prompt_symbol_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """8. Prompt symbol evaluates to 'apa >' when apa skin is active."""
    apa.ensure_apa_skin(target_home=tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    skin = set_active_skin("apa")
    assert skin.name == "apa"

    symbol = get_active_prompt_symbol("❯")
    assert symbol.strip() == "apa >"
    assert symbol == "apa > "


# =============================================================================
# 9. Compact and Full Banner Rendering
# =============================================================================


def test_09_banner_rendering_with_apa_skin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """9. Compact banner renders APA title and skin palette without errors."""
    apa.ensure_apa_skin(target_home=tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    set_active_skin("apa")

    compact = _build_compact_banner()
    assert "APA — Antigravity Personal Agent" in compact
    assert "#1E293B" in compact or "#38BDF8" in compact

    # Test full welcome banner construction without exception
    from rich.console import Console

    con = Console(file=io.StringIO(), force_terminal=True, width=120)
    build_welcome_banner(
        console=con,
        model="gemini-4-argon-medium",
        cwd=str(tmp_path),
        tools=[],
        enabled_toolsets=[],
        session_id="test-session-123",
        provider="antigravity",
    )
    output = con.file.getvalue()
    assert "Antigravity Agent" in output
    assert "Powered by Hermes" in output


# =============================================================================
# 10. Status Bar Style Overrides
# =============================================================================


def test_10_status_bar_style_classes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """10. Status bar styles correctly reflect the Minimalist Professional background and tokens."""
    apa.ensure_apa_skin(target_home=tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    set_active_skin("apa")

    overrides = get_prompt_toolkit_style_overrides()
    assert "status-bar" in overrides
    sb_style = overrides["status-bar"]
    assert "bg:#0B0F14" in sb_style
    assert "#E6EDF3" in sb_style

    sb_good = overrides["status-bar-good"]
    assert "bg:#0B0F14" in sb_good
    assert "#22C55E" in sb_good

    sb_crit = overrides["status-bar-critical"]
    assert "bg:#0B0F14" in sb_crit
    assert "#EF4444" in sb_crit


# =============================================================================
# 11. APA Status Command Visual Identity Output
# =============================================================================


def test_11_apa_status_visual_identity_output(monkeypatch: pytest.MonkeyPatch):
    """11. 'apa status' CLI includes Visual Identity section displaying 'apa' skin."""
    sio = io.StringIO()
    monkeypatch.setattr(sys, "stdout", sio)

    rc = apa.main(["status"])
    assert rc == 0
    val = sio.getvalue()
    assert "[Visual Identity]" in val
    assert "Active Skin:" in val
    assert "apa" in val
    assert "Brand Name:" in val
    assert "APA — Antigravity Personal Agent" in val
    assert "Prompt Symbol:" in val
    assert "apa >" in val
    assert "Theme Canvas:" in val
    assert "#0B0F14" in val
    assert "Accent Color:" in val
    assert "#38BDF8" in val


# =============================================================================
# 12. Zero Hermes Core Modification Verification
# =============================================================================


def test_12_zero_core_modifications():
    """12. Enforce strict Zero Hermes Core Modification invariant."""
    core_files = [
        "run_agent.py",
        "model_tools.py",
        "toolsets.py",
        "hermes_state.py",
        "hermes_constants.py",
        "hermes_cli/skin_engine.py",
        "hermes_cli/banner.py",
        "hermes_cli/cli_render.py",
        "hermes_cli/cli_tui_mixin.py",
        "hermes_cli/cli_status_bar_mixin.py",
        "ui-tui/src/theme.ts",
    ]
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", *core_files],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    changed = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert changed == [], f"Hermes core files must NEVER be modified: {changed}"
