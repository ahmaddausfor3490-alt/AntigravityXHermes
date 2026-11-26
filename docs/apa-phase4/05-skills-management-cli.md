# APA Phase 4.5: Skills Management CLI Specification & Implementation

## 1. Executive Summary

Phase 4.5 implements the **APA Skills Management CLI** for the Antigravity Personal Agent (APA), providing a unified, secure, and user-friendly command-line interface for managing and inspecting skills without modifying Hermes Agent core.

The CLI exposes 9 essential subcommands under `apa skills` (`list`, `search`, `info`, `enable`, `disable`, `validate`, `reload`, `install`, `remove`), supports both Rich human-readable terminal rendering and machine-parsable `--json` outputs, enforces full integration with the APA Security Guard (Phase 4.4), and respects Hermes Agent prompt caching and progressive disclosure invariants.

---

## 2. Architecture & Design Principles

### 2.1 Zero Hermes Core Modification Policy
In strict accordance with the project policy, Phase 4.5 introduces zero modifications to Hermes Core files:
- Agent execution loop untouched.
- Conversation and turn orchestration untouched.
- Memory and session subsystems untouched.
- Hermes core skills engine untouched.

All CLI capabilities operate via the `plugins/apa-skills/` plugin boundary and public Hermes configuration mechanisms (`hermes_cli.config`, `hermes_constants`).

### 2.2 Boundary Isolation & Dynamic Resolution
To maintain strict architectural boundary compliance, `plugins/apa-skills/cli.py` avoids direct static imports of private core modules. Module discovery and interceptor retrieval resolve dynamically through published plugin interfaces or runtime lookups, ensuring 0 new crossings against the `boundary_lint_baseline.json`:
- `module_to_core`: 839 (0 new crossings)
- `core_to_module`: 20 (0 new crossings)

### 2.3 Layered Architecture Diagram

```
+-----------------------------------------------------------------+
|                     User / Terminal / CI                        |
+-----------------------------------------------------------------+
        | (apa skills <cmd> / apa.cmd / apa.ps1 / apa.py)
        v
+-----------------------------------------------------------------+
|                  APA Root CLI & Launchers                       |
|         (apa.py, apa.cmd, apa.ps1, apa bash script)             |
+-----------------------------------------------------------------+
        |
        v
+-----------------------------------------------------------------+
|               APA Skills CLI (plugins/apa-skills/cli.py)        |
|  - Subcommand Router (list, search, info, enable, disable, etc) |
|  - Rich Table & Panel Rendering / JSON Output Mode              |
+-----------------------------------------------------------------+
        |                           |
        v                           v
+-------------------------+  +------------------------------------+
|   APA Security Guard    |  |  APA Skills Subsystems             |
|   (plugins/apa-security)|  |  - Registry (Phase 4.2)            |
| - PolicyEngine          |  |  - Discovery (Phase 4.3)           |
| - SkillsSecurityGuard   |  |  - Loader (Phase 4.3)              |
| - AuditLogger           |  |  - Validator (Phase 4.3)           |
| - PermissionManager     |  +------------------------------------+
+-------------------------+                 |
                                            v
                             +------------------------------------+
                             |  Hermes Configuration & Storage    |
                             |  - config.yaml (skills.disabled)   |
                             |  - Prompt Cache Invalidation       |
                             +------------------------------------+
```

---

## 3. Subcommand Specifications

### 3.1 `list`
Lists installed skills across all discovered roots with their category, source, precedence tier, enabled status, and validation outcome.
- **Flags**:
  - `--enabled-only`: Filter output to active skills only.
  - `--source <source>`: Filter by source type (`project`, `profile`, `custom`, `all`).
  - `--json`: Output as a structured JSON array.
- **Security**: Classifies as `READ`.

### 3.2 `search <query>`
Searches skill names, descriptions, categories, and tags using case-insensitive substring matching.
- **Flags**:
  - `--json`: Output results as JSON.
- **Security**: Classifies as `READ`.

### 3.3 `info <name>`
Displays exhaustive metadata for a specific skill: declared name, unique load name, version, author, directory, source tier, platform compatibility, validation status with detailed findings, required tools, tags, and related skills.
- **Flags**:
  - `--json`: Output schema-compliant JSON dictionary.
- **Security**: Classifies as `READ`.

### 3.4 `enable <name>`
Enables a skill by removing it from `config.yaml` (`skills.disabled`), clears prompt cache, and triggers registry refresh.
- **Flags**:
  - `--json`: Output operation result in JSON.
- **Security**: Classifies as `WRITE`. Evaluated through `PolicyEngine.evaluate_tool_call`.

### 3.5 `disable <name>`
Disables a skill by adding it to `config.yaml` (`skills.disabled`), clears prompt cache, and refreshes the registry.
- **Protection**:
  - Strictly refuses to disable pinned skills (`rule: SKILL_PINNED_PROTECTED`).
  - Strictly refuses to disable essential Hermes skills (`hermes-agent`, `skills`, `manage_skills`, `skill_manager`).
- **Flags**:
  - `--json`: Output operation result in JSON.
- **Security**: Classifies as `WRITE`.

### 3.6 `validate <name>`
Executes structural and frontmatter validation without executing any scripts or code in the skill.
- **Checks**:
  - Directory structure and `SKILL.md` presence.
  - Frontmatter delimiters and YAML syntax.
  - Reserved character constraints and length limits.
  - Dangerous shell scripts or obfuscated code patterns.
  - Sensitive files within skill bundles.
- **Flags**:
  - `--json`: Output validation report in JSON.
- **Security**: Classifies as `READ`.

### 3.7 `reload`
Forces an immediate rescan of all configured skill roots, re-indexes metadata, purges outdated cache entries, and clears the prompt cache.
- **Flags**:
  - `--json`: Output refresh statistics in JSON.
- **Security**: Classifies as `READ` / operational sync.

### 3.8 `install <source>`
Installs a skill from a local path or repository directory into an approved root.
- **Protections**:
  - Rejects path traversal (`..`), null bytes (`\x00`), and Windows device namespaces (`\\.\PhysicalDrive0`).
  - Validates source contains a compliant `SKILL.md`.
  - Rejects duplicate installations unless `--force` is specified.
- **Flags**:
  - `--name <name>`: Override declared skill name.
  - `--category <cat>`: Place skill in a specific category subdirectory.
  - `--force`: Overwrite existing skill directory if present.
  - `--json`: Output operation status in JSON.
- **Security**: Classifies as `WRITE`. Evaluated via `PolicyEngine`.

### 3.9 `remove <name>`
Permanently deletes an installed skill directory from an approved root.
- **Protections**:
  - Strictly refuses to delete pinned skills (`SKILL_PINNED_PROTECTED`).
  - Strictly refuses to delete skills outside approved roots or approved root directories themselves.
  - Interactive confirmation prompt (`[y/N]`) required unless `--yes` / `-y` flag is supplied.
- **Flags**:
  - `-y`, `--yes`: Skip interactive confirmation prompt.
  - `--json`: Output deletion status in JSON.
- **Security**: Classifies as `DELETE`. Requires confirmation gate and logs destructive action to `AuditLogger`.

---

## 4. Root Entrypoint & Multi-Platform Launchers

The following executable wrappers provide seamless CLI invocations across PowerShell, Windows Command Prompt, and Bash:

1. **`apa.py`**:
   Root Python entrypoint handling global options (`--version`, `--help`) and dispatching `skills` subcommands to `plugins/apa-skills/cli.py`.
2. **`apa.cmd`**:
   Windows Command Prompt batch launcher:
   ```cmd
   @echo off
   setlocal
   uv run python "%~dp0apa.py" %*
   ```
3. **`apa.ps1`**:
   Windows PowerShell launcher:
   ```powershell
   #!/usr/bin/env pwsh
   $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
   & uv run python "$ScriptDir\apa.py" @args
   ```
4. **`apa`**:
   POSIX bash wrapper:
   ```bash
   #!/usr/bin/env bash
   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
   exec uv run python "${SCRIPT_DIR}/apa.py" "$@"
   ```

---

## 5. Security & Audit Verification

All CLI operations are fully integrated into APA Security Guard:
- **Audit Logging**: Destructive (`remove`), mutating (`install`, `enable`, `disable`), and maintenance (`reload`) actions are logged to `apa_skills_audit.jsonl` with timestamps, operation names, target identifiers, and decision status.
- **Secret Scrubbing**: Sensitive strings (API tokens, private keys, passwords) are automatically scrubbed from logged arguments and metadata.
- **Windows Path Defense**: Full compatibility with backslash paths (`\`), drive letters (`C:\`), spaced directories, and Windows UNC/device namespace neutralization.

---

## 6. Test Suite & Verification Results

### 6.1 Test Suite Breakdown (`tests/plugins/test_apa_skills_cli.py`)
| Test ID | Test Name | Target Behavior | Result |
|---|---|---|---|
| 1 | `test_command_registration` | All 9 subcommands and flags registered | PASS |
| 2 | `test_list_output` | Human-readable table rendering | PASS |
| 3 | `test_list_json_output` | JSON list schema validation | PASS |
| 4 | `test_list_filters` | `--enabled-only` and `--source` filters | PASS |
| 5 | `test_search` | Search by name, description, tags, category | PASS |
| 6 | `test_info` | Metadata, compatibility, tools, related skills | PASS |
| 7 | `test_info_missing_skill` | Error code 1 on missing skill | PASS |
| 8 | `test_enable_and_disable` | Config persistence, cache invalidation | PASS |
| 9 | `test_pinned_skill_protection` | Refusal to disable/remove pinned skills | PASS |
| 10 | `test_validate` | Non-executing structural safety inspection | PASS |
| 11 | `test_reload` | Cache invalidation and registry refresh | PASS |
| 12 | `test_install` | Source validation, copying, duplicate refusal | PASS |
| 13 | `test_install_path_traversal_rejected` | Traversal, null bytes, device prefixes blocked | PASS |
| 14 | `test_remove_with_confirmation` | Prompt cancellation preserves skill | PASS |
| 15 | `test_remove_with_yes_flag` | Destructive removal with `-y` | PASS |
| 16 | `test_remove_unauthorized_root_rejected` | Refusal to delete outside approved roots | PASS |
| 17 | `test_audit_logging` | Audit events recorded with scrubbed secrets | PASS |
| 18 | `test_windows_compatibility` | Backslashes, drive letters, spaced paths | PASS |
| 19 | `test_apa_root_cli_dispatch` | Root `apa.py` `--version`, `--help`, dispatch | PASS |

### 6.2 Full Regression Suite Results
```
tests/plugins/test_apa_skills_cli.py ...................               [ 16%]
tests/plugins/test_apa_skills_security.py ...................          [ 32%]
tests/plugins/test_apa_skills_loader.py ........                       [ 39%]
tests/plugins/test_apa_skills_discovery.py .....                       [ 44%]
tests/plugins/test_apa_skills_validator.py ..........                  [ 53%]
tests/plugins/test_apa_skills_registry.py ..................           [ 65%]
tests/plugins/test_apa_security.py ......................              [ 84%]
tests/agent/test_antigravity_provider.py ...........                   [ 95%]
tests/agent/test_acp_client.py .....                                   [100%]

============================ 117 passed in 35.88s =============================
```

### 6.3 Quality Gates
- **Ruff**: `scripts/check --only ruff` -> **PASSED** (0 errors).
- **Code Health**: `scripts/check --only health` -> **PASSED** (0 blocking, 0 advisory).
- **Boundary Lint**: `python scripts/boundary_lint.py --check` -> **PASSED** (`module_to_core: 839`, `core_to_module: 20`, 0 new crossings).
