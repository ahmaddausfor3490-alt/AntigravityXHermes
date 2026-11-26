# APA Phase 5 — Full Hermes Home Isolation Audit & Migration Plan

## 1. Executive Summary & Purpose

| Attribute | Details |
| :--- | :--- |
| **System** | Antigravity Personal Agent (APA) |
| **Repository Root** | `C:\Users\firdaus\APA` |
| **Global Hermes Data Directory** | `C:\Users\firdaus\AppData\Local\hermes` (`%LOCALAPPDATA%\hermes`) |
| **Target Isolated Data Directory** | `C:\Users\firdaus\APA\data\hermes-home` |
| **Objective** | Full isolation of APA runtime data from global Hermes without breaking native capabilities |
| **Hermes Core Status** | **ZERO HERMES CORE MODIFICATIONS** (Native `HERMES_HOME` mechanism) |
| **Current Stage** | **STAGE 1 & 2 & 3: Audit, Architecture Design, & Implementation Plan (No Production Changes)** |

The objective of this initiative is to establish **Full HERMES_HOME Isolation** for the APA project. Currently, APA executes using the global Hermes directory (`%LOCALAPPDATA%\hermes`), sharing sessions, logs, memory, skills caches, and gateway locks with any global Hermes instance. 

By isolating APA into `C:\Users\firdaus\APA\data\hermes-home`, APA becomes a self-contained, enterprise-grade, portable product while strictly preserving 100% of Hermes' native capabilities, session history, memory, tools, and Telegram gateway functionality.

---

## 2. Fundamental Constraints & Compliance Rules

1. **Do not delete or modify global Hermes**: `C:\Users\firdaus\AppData\Local\hermes` remains completely intact and undisturbed.
2. **Do not modify legacy project**: `D:\Antigravity-Hermes` is strictly off-limits.
3. **Zero Hermes Core Modification**: No files in `agent/`, `hermes_cli/`, `gateway/`, `tools/`, or core modules will be modified. The isolation leverages Hermes' native, first-class `HERMES_HOME` contract.
4. **Zero Boundary Lint Baseline Alteration**: `scripts/boundary_lint_baseline.json` will remain untouched.
5. **Zero Git Commit or Push**: No git commit or push will occur.
6. **Zero Data Loss**: All existing 41 sessions, 971 messages, memories (`USER.md`), skills, MCP credentials, and Telegram tokens will be preserved.
7. **Official AGY Authentication Preservation**: AGY OAuth token management remains anchored to official `agy` CLI (`~/.gemini/antigravity-cli`), completely independent of `HERMES_HOME`.

---

## 3. Tahap 1 — Comprehensive Runtime Audit

A systematic audit of all runtime data layers, constants, entry points, and storage mechanisms was conducted.

### 3.1 `hermes_constants.py` & `HERMES_HOME` Resolution Mechanism

In `hermes_constants.py`, the resolution order for `get_hermes_home()` is:

1. **Context-local override**: `get_hermes_home_override()` (managed by `hermes_scope.push_state`).
2. **Environment variable**: `os.environ.get("HERMES_HOME")` (expanded via `_expand_hermes_home()`).
3. **Platform Default**:
   - On Windows: `%LOCALAPPDATA%\hermes` (via `_platform_default_hermes_home()`).
   - On POSIX: `~/.hermes`.

**Crucial Root Resolution Behavior**:
`get_default_hermes_root()` evaluates `_hermes_root_of(native_home, env_path)`:
```python
def _hermes_root_of(native_home: Path, env_path: Path | None) -> Path:
    if env_path is None:
        return native_home
    try:
        env_path.resolve().relative_to(native_home.resolve())
    except ValueError:
        return env_path.parent.parent if env_path.parent.name == "profiles" else env_path
    return native_home
```
*Audit Finding*: When `HERMES_HOME` is set to `C:\Users\firdaus\APA\data\hermes-home`:
- `env_path.parent.name` is `"data"` (not `"profiles"`).
- `_hermes_root_of` returns `C:\Users\firdaus\APA\data\hermes-home` directly as the root.
- Therefore, `get_hermes_home()` and `get_default_hermes_root()` resolve to the **exact same directory**, preventing any fallback to global `%LOCALAPPDATA%\hermes`.

### 3.2 Configuration Loading (`config.yaml`)

- **Code Path**: `hermes_cli/config.py:get_config_path()` returns `get_hermes_home() / "config.yaml"`.
- **Current State**: Global `config.yaml` is version 53, containing 32 top-level sections (including custom providers, gateway policies, display settings, and tool loops).
- **Isolation Impact**: Under isolated `HERMES_HOME`, `config.yaml` will be loaded exclusively from `data\hermes-home\config.yaml`.

### 3.3 Secrets & Environment Loading (`.env`)

- **Code Path**: `hermes_cli/config.py:get_env_path()` returns `get_hermes_home() / ".env"`.
- **Invariants**: `LAUNCH_ONLY_ENV_KEYS = frozenset({"HERMES_MANAGED_DIR", "HERMES_HOME"})`. Hermes forbids setting `HERMES_HOME` inside `.env` to prevent recursive re-scoping. `HERMES_HOME` must be set at the process launcher level.
- **Current State**: Global `.env` contains 15 keys, including `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_HOME_CHANNEL`, and browser automation parameters.

### 3.4 Authentication & Credential Store (`auth.json`)

- **Code Path**: `hermes_cli/auth.py:_auth_file_path()` returns `get_hermes_home() / "auth.json"`.
- **Fallback Guard**: `_global_auth_file_path()` returns `None if _same_path(get_hermes_home(), global_root) else global_root / "auth.json"`.
- *Audit Finding*: Because `get_default_hermes_root()` equals `get_hermes_home()`, `_global_auth_file_path()` returns `None`. **There is zero credential leakage to or from global `auth.json`.**
- **Antigravity Authentication**: Google Antigravity OAuth tokens are NOT managed in `auth.json`. Instead, `AGYProcessManager` delegates directly to the official `agy` binary (`agy acp --stdio`, `agy login`, `agy logout`), which maintains its own credentials under `%USERPROFILE%\.gemini\antigravity-cli`.

### 3.5 Session Database & Transcripts (`state.db` & `sessions/`)

- **Code Path**: `hermes_state.py:_default_db_path()` returns `get_hermes_home() / "state.db"`.
- **Current State**:
  - Global `state.db` size: ~8.1 MB.
  - Active sessions count: **41 sessions**.
  - Total messages count: **971 messages**.
  - JSONL transcripts in `sessions/`: **28 files**.
- **WAL State**: Active `state.db-wal` (889 KB) and `state.db-shm` (32 KB). Migration must perform a clean WAL checkpoint before copying.

### 3.6 Memory System (`memories/`)

- **Code Path**: `agent/memory_manager.py` resolves `hermes_home = get_hermes_home()`.
- **Storage Location**: `get_hermes_home() / "memories" / "USER.md"`.
- **Current State**: Global `USER.md` stores user preferences (clean formatting without asterisks/dashes, deliverables path `D:\Tugas`).

### 3.7 Skills Subsystem (`skills/`)

- **Code Path**: `hermes_constants.py:get_skills_dir()` returns `get_hermes_home() / "skills"`.
- **Search Precedence**:
  1. Project skills: `C:\Users\firdaus\APA\skills\` (Tier 1).
  2. Local user skills: `get_hermes_home() / "skills"` (Tier 2).
  3. External skills (Tier 3).
- **Current State**: Global contains 16 categories, `.curator_ledger.jsonl`, and `.usage.json`.
- **APA Skills Registry**: `plugins/apa-skills/registry.py` already resolves `get_skills_dir()`.

### 3.8 Plugins & Plugin Storage (`plugin-data/`)

- **Code Path**: `plugins/plugin_storage.py:plugin_data_dir()` returns `get_hermes_home() / "plugin-data" / <name>`.
- **User Plugins**: `hermes_cli/plugins_discovery.py` scans `get_hermes_home() / "plugins"`.
- **Bundled Plugins**: APA plugins (`plugins/apa-security`, `plugins/apa-skills`, `plugins/model-providers/antigravity`) live in-tree within `C:\Users\firdaus\APA\plugins` and are loaded dynamically by `apa.py`.

### 3.9 Gateway Process Management, PID, Locks & State

- **Code Path**: `gateway/status.py`, `gateway/runtime_ownership.py`, `hermes_cli/gateway_runtime_start.py`.
- **Runtime Files**:
  - PID: `get_hermes_home() / "gateway.pid"`
  - Lock: `get_hermes_home() / "gateway.lock"`
  - State: `get_hermes_home() / "gateway_state.json"`
  - Stdout/Stderr: `get_hermes_home() / "logs" / "gateway-stdio.log"`
- *Critical Invariant Verified*: In `hermes_cli/gateway_runtime_start.py:106`:
  ```python
  env.update(HERMES_HOME=str(home), HERMES_GATEWAY_DETACHED="1", PYTHONIOENCODING="utf-8")
  ```
  The gateway daemon spawner **explicitly propagates `HERMES_HOME` to detached background gateway processes**. When APA runs with isolated `HERMES_HOME`, any background gateway daemon automatically binds to `data\hermes-home\gateway.pid` and `gateway.lock`. It will never conflict with or kill the global gateway daemon.

### 3.10 Cron & MCP Subsystems

- **Cron**: `cron/jobs.py` anchors to `get_hermes_home().resolve() / "cron"`. Stores `jobs.json`, `executions.db`, `ticker_heartbeat`.
- **MCP Tokens**: `tools/mcp_oauth.py` stores tokens in `get_hermes_home() / "mcp-tokens"`. Global currently holds tokens for `scite` MCP.

### 3.11 Logs & Security Audits

- **Log Directory**: `hermes_cli/logs.py:log_file_path()` anchors to `get_hermes_home() / "logs"`.
- **APA Security Guard**: `plugins/apa-security/config.py:_default_audit_log_path()` anchors to `get_hermes_home() / "logs" / "apa_security_audit.jsonl"`.

### 3.12 Audit of All References to `%LOCALAPPDATA%\hermes`

- **Finding**: Hardcoded references to `%LOCALAPPDATA%\hermes` exist only as platform fallbacks in `hermes_constants.py` and diagnostic strings in installer/uninstaller scripts.
- **Binary Distinction**: `uv.exe` is located at `C:\Users\firdaus\AppData\Local\hermes\bin\uv.exe` and is on the system PATH. This is the **package manager binary executable**, not runtime data. Isolating `HERMES_HOME` data directory does NOT alter or touch the binary executable.

---

## 4. Tahap 2 — Migration Design

### 4.1 Target Layout Specification

Target path: `C:\Users\firdaus\APA\data\hermes-home`

```text
C:\Users\firdaus\APA\data\hermes-home\
├── config.yaml                          # APA-tailored configuration (antigravity provider, apa skin)
├── .env                                 # Secrets, Telegram bot token, credentials (copied 1:1)
├── auth.json                            # Provider auth store & credential pool (copied 1:1)
├── SOUL.md                              # Agent persona & base prompt
├── state.db                             # SQLite session database (41 sessions, 971 messages)
├── kanban.db                            # Kanban tasks database
├── projects.db                          # Projects registry database
├── channel_directory.json               # Gateway channel routing registry
├── telegram_update_receipts_*.json      # Telegram deduplication receipts
│
├── sessions/                            # 28+ session transcripts (.jsonl)
├── memories/                            # Agent memory (USER.md)
├── skills/                              # User skills & metadata (.curator_ledger, .usage.json)
├── skins/                               # Visual customization (apa.yaml)
├── mcp-tokens/                          # MCP OAuth tokens (scite.json, etc.)
├── plugin-data/                         # Isolated plugin persistence
├── plugins/                             # User-installed plugins directory
├── cron/                                # Cron jobs & execution database
│   ├── output/
│   └── executions.db
├── cache/                               # Cache & scratch space
│   ├── scratch/
│   └── schema_columns.json
└── logs/                                # Logs & security audit trail
    ├── agent.log
    ├── gateway.log
    └── apa_security_audit.jsonl
```

### 4.2 Migration Component Mapping

| Category | Source File/Directory | Target Location | Strategy | Validation Check |
| :--- | :--- | :--- | :--- | :--- |
| **Config** | `config.yaml` | `data\hermes-home\config.yaml` | Copy & update defaults (`provider: antigravity`, `model: gemini-4-argon-medium`, `skin: apa`) | Valid YAML, default model verified |
| **Secrets** | `.env` | `data\hermes-home\.env` | Exact 1:1 Copy | File exists, hash match, permissions |
| **Auth** | `auth.json` | `data\hermes-home\auth.json` | Exact 1:1 Copy | Valid JSON, providers preserved |
| **Soul** | `SOUL.md` | `data\hermes-home\SOUL.md` | Exact 1:1 Copy | File exists |
| **Sessions DB** | `state.db` | `data\hermes-home\state.db` | Checkpointed Copy (WAL flush before copy) | 41 sessions, 971 messages verified |
| **Transcripts** | `sessions\*.jsonl` | `data\hermes-home\sessions\` | Recursive Copy | File count matches source (28 files) |
| **Memories** | `memories\USER.md` | `data\hermes-home\memories\` | Recursive Copy | `USER.md` content intact |
| **Skills** | `skills\` (excl `.locks`) | `data\hermes-home\skills\` | Recursive Copy (omit stale lockfiles) | Skill directory count matches |
| **MCP** | `mcp-tokens\` | `data\hermes-home\mcp-tokens\` | Recursive Copy | `scite.json` preserved |
| **Skins** | `skins\apa.yaml` | `data\hermes-home\skins\` | Copy + Ensure | `apa.yaml` present |
| **Databases** | `kanban.db`, `projects.db` | `data\hermes-home\` | Exact 1:1 Copy | Tables queryable |
| **Telegram** | `telegram_update_receipts_*.json` | `data\hermes-home\` | Exact 1:1 Copy | JSON valid |
| **Logs** | `logs\` | `data\hermes-home\logs\` | Initialize clean directory | Directory created |
| **Gateway State**| `gateway.pid`, `gateway.lock` | (Do NOT copy) | Fresh start | Created automatically on gateway start |

---

## 5. Tahap 3 — Implementation Plan

### 5.1 Files to Modify (Launcher Layer Only — ZERO Core Changes)

To ensure that every execution of APA is automatically isolated without requiring the user to remember manual environment flags:

1. **`apa.py`** (Entrypoint bootstrap):
   Add an early bootstrap block at the very top of `apa.py` before any Hermes imports:
   ```python
   DEFAULT_APA_HERMES_HOME = (Path(__file__).resolve().parent / "data" / "hermes-home").resolve()
   if not os.environ.get("HERMES_HOME"):
       os.environ["HERMES_HOME"] = str(DEFAULT_APA_HERMES_HOME)
   ```
   Ensure `ensure_apa_skin()` and `ensure_apa_config()` explicitly pass `DEFAULT_APA_HERMES_HOME`.

2. **`apa.cmd`** (Windows Command Prompt Launcher):
   ```cmd
   @echo off
   setlocal
   if not defined HERMES_HOME set "HERMES_HOME=%~dp0data\hermes-home"
   uv run python "%~dp0apa.py" %*
   ```

3. **`apa.ps1`** (PowerShell Launcher):
   ```powershell
   #!/usr/bin/env pwsh
   $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
   if (-not $env:HERMES_HOME) { $env:HERMES_HOME = "$ScriptDir\data\hermes-home" }
   & uv run python "$ScriptDir\apa.py" @args
   ```

4. **`apa`** (Bash Launcher):
   ```bash
   #!/usr/bin/env bash
   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
   export HERMES_HOME="${HERMES_HOME:-${SCRIPT_DIR}/data/hermes-home}"
   exec uv run python "${SCRIPT_DIR}/apa.py" "$@"
   ```

### 5.2 Step-by-Step Migration Execution Sequence

```
[Step 1: Quiescence]
  → Gracefully stop running gateway daemon (`uv run python -m hermes_cli.main gateway stop`)
  → Checkpoint SQLite WAL on global state.db (`PRAGMA wal_checkpoint(TRUNCATE)`)

[Step 2: Destination Preparation]
  → Verify `C:\Users\firdaus\APA\data\hermes-home` exists
  → Ensure subdirectories: sessions, memories, skills, skins, mcp-tokens, cron/output, logs, cache/scratch

[Step 3: Atomic Data Copy]
  → Copy static files: .env, auth.json, SOUL.md, kanban.db, projects.db, channel_directory.json
  → Copy sessions/ (*.jsonl)
  → Copy memories/ (USER.md)
  → Copy skills/ (all categories and metadata)
  → Copy mcp-tokens/
  → Copy telegram_update_receipts_*.json
  → Copy state.db (clean, checkpointed database)

[Step 4: Configuration Tuning]
  → Copy config.yaml into `data\hermes-home\config.yaml`
  → Set default provider: `model.provider: antigravity`
  → Set default model: `model.default: gemini-4-argon-medium`
  → Set visual skin: `display.skin: apa`

[Step 5: Launcher Layer Isolation]
  → Update `apa.py`, `apa.cmd`, `apa.ps1`, `apa` with `HERMES_HOME` defaults

[Step 6: End-to-End Acceptance Testing]
  → Verify APA reports isolated home: `apa status`
  → Verify sessions list: `apa sessions list` (41 sessions present)
  → Verify memory recall: `apa memory show` (USER.md present)
  → Verify skills discovery: `apa skills list`
  → Verify AGY official auth: `apa auth status`
  → Execute live chat turn: `apa chat -q "test" --oneshot`
  → Verify logs written to `data\hermes-home\logs\agent.log`
  → Verify global `%LOCALAPPDATA%\hermes\logs\agent.log` was NOT touched
  → Run full regression test suite (58+ automated tests)
```

### 5.3 Risk Assessment & Mitigation Matrix

| Risk Scenario | Severity | Impact | Mitigation Strategy |
| :--- | :---: | :--- | :--- |
| **SQLite WAL Lock Contention** | Medium | Copying `state.db` while WAL is active could produce a torn read. | Issue `PRAGMA wal_checkpoint(TRUNCATE)` via `sqlite3` before copying; verify 0-byte WAL before copy. |
| **Gateway Process Clashing** | Medium | A running global gateway daemon might contest sockets or Telegram poller. | Stop the global gateway daemon cleanly (`gateway stop`) prior to migration; APA daemon uses distinct PID/lock in `data\hermes-home`. |
| **Subprocess Environment Loss** | Low | Subprocesses spawned by terminal tools losing `HERMES_HOME`. | Verified in `gateway_runtime_start.py` and `tools/environments/local.py`: `HERMES_HOME` is explicitly inherited and pinned in child environments. |
| **Telegram Poller Dual-Bind** | High | Two gateways polling the same Telegram bot token triggers 409 Conflict. | When starting APA Telegram Gateway, ensure global gateway is not actively polling the same token. |
| **Source Data Mutation** | High | Accidental deletion or overwrite of global Hermes data. | Migration uses strict **read-only copy operations** (`Copy-Item` / `shutil.copy2`). No delete, move, or truncate commands will be executed against global Hermes. |

### 5.4 Rollback Plan

Because the migration is 100% additive and non-destructive:
- Global `%LOCALAPPDATA%\hermes` is NEVER deleted, truncated, or relocated.
- If any anomaly arises, rolling back takes **less than 10 seconds**:
  1. Revert `apa.py`, `apa.cmd`, `apa.ps1`, `apa` (removing `HERMES_HOME` default injection).
  2. Stop any running gateway daemon in `data\hermes-home`.
  3. APA immediately returns to operating against global Hermes with zero data loss.

---

## 6. Acceptance Criteria Verification Checklist

- [ ] **Data Isolation**: APA commands read/write solely to `C:\Users\firdaus\APA\data\hermes-home`.
- [ ] **Global Integrity**: `C:\Users\firdaus\AppData\Local\hermes` remains untouched.
- [ ] **Official AGY Authentication**: `apa auth status` and chat execution succeed using official AGY CLI OAuth.
- [ ] **Session Continuity**: All 41 sessions and transcripts accessible via `apa sessions list`.
- [ ] **Memory Continuity**: `apa memory show` recalls `USER.md` content.
- [ ] **Skills Engine**: All APA and Hermes skills discoverable and manageable via `apa skills list`.
- [ ] **Security Guard**: `apa_security_audit.jsonl` logs written to `data\hermes-home\logs\`.
- [ ] **Telegram Gateway**: Token and update receipts functional without re-delivering historical messages.
- [ ] **Test Gates**: All unit and integration test suites pass (`pytest tests/plugins/`, Ruff, Health, Boundary Lint).
