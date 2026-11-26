# APA Phase 4.1 — Skills Architecture Audit & Integration Analysis

## 1. Executive Summary

This document provides a comprehensive technical audit of the skills system within the Hermes Agent codebase (`C:\Users\firdaus\APA`) to establish the architectural foundation for **APA Phase 4: Skills Integration**.

The audit was conducted non-destructively on branch `apa-development` under Python `3.14.9` and Windows 10/11 x64. The primary finding is that **Hermes already possesses a highly sophisticated, multi-tiered skills subsystem** with progressive disclosure, static security scanning, frontmatter parsing, on-disk directory resolution, and CLI integration.

**Critical Architectural Decision**:
- **Zero Hermes Core Modification Required**: Phase 4 can be implemented completely within the existing plugin architecture by introducing `plugins/apa-skills/` and connecting with `plugins/apa-security/`, preserving complete core integrity (`git diff` on core tracked files remains zero).

---

## 2. Existing Hermes Skills Architecture & Conventions

### 2.1 File & Directory Layout

Hermes manages skills across five distinct directory tiers:

| Tier | Directory / Location | Description & Ownership | Precedence Level |
|---|---|---|---|
| **Tier 0 (Project)** | `<project_root>/.hermes/skills/`<br>`<project_root>/.agents/skills/` | Repo-local skills stored inside a Git workspace. Loaded **only** if the repository root is explicitly listed in `skills.trusted_project_dirs`. | Highest (0) |
| **Tier 1 (Local Profile)** | `~/.hermes/skills/` (`get_skills_dir()`) | Active user profile skills directory. Primary location for installed, customized, and agent-created skills. Profile-aware via `HERMES_HOME`. | High (1) |
| **Tier 2 (Custom Create)** | Configured `skills.create_dir` | Optional custom target path for newly created skills. | Medium (2) |
| **Tier 3 (External)** | Configured `skills.external_dirs` | Read-only external directories mounted in `config.yaml`. | Low (3) |
| **Bundled / Static** | `skills/<category>/<skill>/SKILL.md` | Shipped in repository tree; seeded into local profile upon initialization. | Baseline seed |
| **Optional Shipped** | `optional-skills/<category>/<skill>/` | Niche/heavy skills shipped in repo but inactive until installed via `hermes skills install official/...`. | Inactive catalog |
| **Plugin Skills** | `<plugin>/skills/<skill>/SKILL.md` | Skills registered dynamically by installed plugins via `PluginManager.register_plugin_skill()`. | Namespaced (`<plugin>:<skill>`) |

### 2.2 SKILL.md Frontmatter & Content Specification

Skills are defined as self-contained directory packages containing a required `SKILL.md` file plus optional supporting directories:
- `references/`: Supporting documentation (`*.md`).
- `templates/`: Jinja/text code templates.
- `assets/`: Static data or media.
- `scripts/`: Executable helper utilities (`*.py`, `*.sh`, `*.js`).

The `SKILL.md` file begins with YAML frontmatter fenced by `---`:

```yaml
---
name: sample-skill
description: Performs targeted data analysis.
version: 1.0.0
author: Developer Name
license: Apache-2.0
platforms: [windows, linux, macos]
environments: [kanban, docker]
requires_apps: []
deps: []
metadata:
  hermes:
    category: software-development
    tags: [data, python]
    related_skills: [codebase-inspection]
    config:
      - key: data_path
        description: Path to input datasets
        default: ~/data
        prompt: Where are datasets stored?
    requires_tools: [read_file, terminal]
---
# Sample Skill
## When to Use
...
## Procedure
...
```

**Authoring Rules Enforced by Hermes**:
1. `description`: Strict limit of $\le 60$ characters, single sentence ending in a period (`SKILL_PROMPT_DESC_LIMIT = 60`).
2. `name`: Must match `^[a-zA-Z0-9_-]+$`, max 64 characters.
3. `platforms`: Validated against `sys.platform` via `PLATFORM_MAP = {"macos": "darwin", "linux": "linux", "windows": "win32"}`. Incompatible skills are excluded from indexing.

---

## 3. Skill Discovery, Loading & Progressive Disclosure

Hermes implements a **two-phase progressive disclosure model** to maintain prompt caching invariants and prevent context bloat:

```
[Phase 1: Discovery & System Prompt Indexing]
  Directories Scanned (Depth-bounded signature + stat cache)
         │
         ▼
  `agent.skill_utils.resolve_skill_catalog()`
         │
         ├── Filter: Disabled skills (`skills.disabled` in config.yaml)
         ├── Filter: Platform mismatch (`platforms:` vs sys.platform)
         ├── Filter: Environment mismatch (`environments:` tag)
         ├── Filter: Security Quarantine (`is_quarantined_project_skill()`)
         │
         ▼
  System Prompt Skill Index (Names + 60-char descriptions only)
  (Cost: ~10-20 tokens per skill, immutable across turns)

[Phase 2: On-Demand Activation / Retrieval]
  Model observes skill index -> Calls `skill_view(name)`
         │
         ▼
  `tools.skills_tool.skill_view()`
         │
         ├── Path validation (no traversal, no Windows drive escape)
         ├── Read SKILL.md full instructions & procedure
         ├── Scan linked files (`references/`, `templates/`, `scripts/`)
         ├── Secret capture & readiness checks (`_skill_readiness()`)
         └── Return full structured JSON into model conversation
```

### Key Modules in Discovery & Loading:
1. `agent/skill_utils.py` (902 lines):
   - `iter_skill_index_files()`: Prunes VCS directories, caches, and support subdirectories.
   - `resolve_skill_catalog()`: Resolves cross-tier precedence (`TIER_PROJECT` > `TIER_LOCAL` > `TIER_CREATE_DIR` > `TIER_EXTERNAL`). Detects shadowed skills and refuses same-tier ambiguities (`AMBIGUOUS_SKILL_PREFIX = "Ambiguous skill name "`).
   - `parse_frontmatter()`: Tolerant YAML parsing with BOM stripping for Windows editors.
   - `is_quarantined_project_skill()`: Scan-time injection defense using `skills_guard`.
2. `tools/skills_tool.py` (807 lines):
   - Exposes agent tools: `skills_list()` and `skill_view()`.
   - `_SKILLS_CACHE`: In-memory stat-based signature cache with 30s TTL.
   - `_skill_readiness()`: Identifies missing required environment variables and credential files.
   - `_log_security_warnings()`: Emits warnings for prompt injection patterns and external file paths.

---

## 4. Skill Authoring & Mutation Architecture (`skill_manage`)

Hermes provides a dedicated tool `skill_manage` (`tools/skill_manager_tool.py`) allowing agents to author and modify skills:

- **Actions Supported**:
  - `create`: Create new skill folder and initial `SKILL.md`.
  - `edit`: Full rewrite of existing `SKILL.md`.
  - `patch`: Targeted fuzzy find-and-replace on `SKILL.md` or supporting files.
  - `delete`: Delete or archive a skill directory.
  - `write_file`: Add/overwrite supporting files (`references/`, `templates/`, `scripts/`, `assets/`).
  - `remove_file`: Delete supporting files.
  - `operations`: Atomic batch mutations.
- **Built-in Security Guards**:
  - `skill_manager_guards.py`: Path traversal validation, delete target sanitization, pinned skill protection (`_pinned_guard()`).
  - `skills_guard.py`: Static scanner checking for shell injections, malicious code, credential theft, and prompt escapes.
  - `skill_lock.py`: Per-skill concurrency locking.

---

## 5. Skills CLI Architecture

Hermes CLI commands are defined in `hermes_cli/subcommands/skills.py` and handled by `hermes_cli/skills_hub.py` and `hermes_cli/skills_config.py`:

```
hermes skills
  ├── list [--source all|hub|builtin|local] [--enabled-only]
  ├── search <query> [--source ...] [--limit N] [--json]
  ├── browse [--page N] [--size N]
  ├── install <identifier|url> [--category C] [--name N] [--force] [-y]
  ├── inspect <identifier>
  ├── uninstall <name> [-y]
  ├── config (interactive TTY checklist to toggle skills)
  ├── trust / untrust [path] (manage trusted project repositories)
  ├── check / update / audit (hub skill lifecycle)
  ├── reset / diff / opt-in / opt-out (bundled skill tracking)
  └── publish / snapshot / tap
```

---

## 6. Security Guard Interception & Skills Gap Analysis

APA Security Guard (`plugins/apa-security/`) intercepts tool calls via `pre_tool_call` and `post_tool_call` hooks in `tool_interceptor.py`.

### Identified Gaps in Current Policy Engine:
1. **Generic Fallback Routing**:
   In `plugins/apa-security/policy_engine.py:L325-331`, `skill_manage` and `skill_view` fall into the generic fallback. If `path` or `file_path` is present, it routes to `_evaluate_read_or_search()`.
   - *Risk*: `skill_manage(action="delete")` would be evaluated as a `READ` operation rather than `DELETE`, bypassing confirmation requirements.
   - *Risk*: `skill_manage(action="create"|"edit"|"patch"|"write_file")` would be evaluated as `READ` rather than `WRITE`.
2. **Workspace Boundary Misalignment**:
   Skills are stored in `~/.hermes/skills/` (`C:\Users\firdaus\.hermes\skills\`), which sits outside the active workspace root (`C:\Users\firdaus\APA`).
   - `PathValidator` blocks operations outside `workspace_root` unless the path is in `allowed_roots`.
   - Skills operations require the skills directory to be an authorized skill root while strictly preventing path traversal into parent directory `~/.hermes/` (protecting `.env`, `config.yaml`, `state.db`).
3. **Skill Script Execution Safety**:
   Skills may contain helper scripts (`scripts/*.py`, `scripts/*.sh`). Hermes agents invoke these scripts via the `terminal` tool.
   - The security guard must ensure that running a skill script does not bypass command validation or access controls.
   - `CommandValidator` and `FilesystemGuard` must inspect all parameters passed to skill scripts.
4. **Untrusted Skill Installation**:
   External skills installed via URLs or repositories must be treated as untrusted input. Installation must never automatically execute arbitrary setup scripts or hooks.

---

## 7. Comparative Assessment & Architectural Strategy

| Requirement Component | Hermes Existing Capability | Reusable As-Is? | APA Customization / New Component Required |
|---|---|---|---|
| **Skill Discovery** | Multi-tier directory scan, stat cache, precedence ranking | **YES** (`agent.skill_utils`) | Wrap into unified APA registry with refresh and status APIs. |
| **Frontmatter & Metadata** | Full YAML frontmatter parser, platform/env filter | **YES** (`parse_frontmatter`) | Extend with strict structural validator and integrity check. |
| **Progressive Disclosure** | `skills_list` and `skill_view` tools | **YES** (`tools.skills_tool`) | Reuse directly; keep system prompt prefix cache intact. |
| **Skill Management Tool** | `skill_manage` (create, edit, patch, delete) | **YES** (`tools.skill_manager_tool`) | Route through Security Guard `pre_tool_call` with proper `OperationType`. |
| **Skill Security Scanning** | Heuristic scanner in `skills_guard.py` | **YES** | Enhance with APA Security Guard audit logging and path verification. |
| **CLI Interface** | `hermes skills` commands | **PARTIAL** | Provide dedicated `apa skills ...` CLI interface matching specification. |
| **Security Interception** | `pre_tool_call` hook in `tool_interceptor.py` | **PARTIAL** | Add dedicated `_evaluate_skill_manage()` and `_evaluate_skill_view()` to `PolicyEngine`. |
| **Antigravity Transport** | ACP stdio streaming + tool lookahead | **YES** | Antigravity provider passes tool calls through Hermes core; 100% compatible. |

---

## 8. Answers to Core Audit Questions

### 1. Which features already exist?
- Multi-tier skill discovery (`skills/`, `optional-skills/`, `~/.hermes/skills/`, project skills).
- Two-phase progressive disclosure (`skills_list`, `skill_view`).
- Comprehensive metadata parser with platform and environment gating.
- Full agent authoring engine (`skill_manage` for creating, editing, patching, and deleting skills).
- Static security scanning (`skills_guard.py`) for prompt injection, sensitive files, and destructive commands.
- Config-based enabling/disabling via `skills.disabled` in `config.yaml`.
- Slash command invocation (`/<skill-name>`).

### 2. Which features can be reused?
- All underlying discovery, catalog resolution, and loading logic in `agent/skill_utils.py` and `tools/skills_tool.py`.
- The atomic mutation and batch operation engine in `tools/skill_manager_tool.py`.
- The existing security scan heuristics in `tools/skills_guard.py`.
- The `pre_tool_call` and `post_tool_call` plugin hook infrastructure in `hermes_cli/plugins.py`.
- The Antigravity ACP client and tool call parser in `plugins/model-providers/antigravity/`.

### 3. Which features need APA customization?
- **Security Policy Engine**: Add explicit handling for `skill_manage` and `skill_view` in `plugins/apa-security/policy_engine.py`:
  - `action="create"|"edit"|"patch"|"write_file"` -> `OperationType.WRITE`.
  - `action="delete"|"remove_file"` -> `OperationType.DELETE` (requires confirmation).
  - Enforce skill path confinement within approved skill roots.
- **Audit Logging**: Ensure skill mutations and views are logged with structured risk levels in `apa_audit.jsonl`.
- **Skill Script Safety**: Guard skill script execution via `terminal` to prevent boundary escapes.

### 4. Which features require new modules?
A new plugin `plugins/apa-skills/` containing:
1. `registry.py`: Unified APA Skills Registry providing thread-safe caching, status inspection, metadata tracking, and reload.
2. `validator.py`: Comprehensive skill validator verifying frontmatter, description rules, platform declarations, and script safety.
3. `manager.py`: High-level lifecycle manager handling safe installation, removal, enabling, and disabling.
4. `cli.py`: The `apa skills` command suite (`list`, `info`, `search`, `enable`, `disable`, `reload`, `validate`, `install`, `remove`).
5. `plugin.yaml` & `__init__.py`: Plugin manifest registering the capabilities into Hermes.

### 5. Is any Hermes Core modification necessary?
**NO.** Hermes Core requires **zero modifications**.
All capabilities integrate cleanly through:
1. The official Hermes plugin extension system (`plugins/`).
2. Plugin hooks (`pre_tool_call`, `post_tool_call`).
3. Reusing existing Hermes internal utilities (`agent.skill_utils`, `tools.skills_tool`, `hermes_cli.config`).

---

## 9. Conclusion & Phase 4 Roadmap

With the audit complete, Phase 4 will proceed incrementally:
- **Phase 4.2**: Skills Registry Design & Implementation (`plugins/apa-skills/registry.py`).
- **Phase 4.3**: Discovery, Loader & Validation (`plugins/apa-skills/validator.py`, `loader.py`).
- **Phase 4.4**: Security Guard Policy Integration (`plugins/apa-security/policy_engine.py`).
- **Phase 4.5**: Skills Management CLI (`apa skills ...`).
- **Phase 4.6**: Antigravity Provider Compatibility Verification.
- **Phase 4.7**: Test Suite Implementation (Unit, Integration, Security, Regression).
- **Phase 4.8**: Comprehensive Documentation & Readiness Report.
