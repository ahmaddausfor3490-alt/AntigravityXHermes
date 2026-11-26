# 09 — APA Legacy Migration Analysis

**Project:** APA (Antigravity Personal Agent) — Phase 1 Audit  
**Base Repository (Hermes):** `C:\Users\firdaus\APA`  
**Legacy APA Repository:** `D:\Antigravity-Hermes`

---

## 1. Executive Migration Matrix

We inspected the complete Legacy APA codebase at `D:\Antigravity-Hermes` (`security/`, `antigravity/`, `memory/`, `skills/`, `config/`, `core/`, `agents/`, `workers/`, `tests/`) and compared each subsystem against Hermes Agent (`C:\Users\firdaus\APA`):

| Legacy Component | Legacy Path (`D:\Antigravity-Hermes`) | Hermes Counterpart (`C:\Users\firdaus\APA`) | Verdict | Integration Target in APA |
|---|---|---|---|---|
| **1. Security Guard** | [security/manager.py](file:///D:/Antigravity-Hermes/security/manager.py), [boundary.py](file:///D:/Antigravity-Hermes/security/boundary.py), [policy.py](file:///D:/Antigravity-Hermes/security/policy.py), [backup.py](file:///D:/Antigravity-Hermes/security/backup.py), [audit.py](file:///D:/Antigravity-Hermes/security/audit.py) | [tools/approval.py](file:///C:/Users/firdaus/APA/tools/approval.py), [agent/file_safety.py](file:///C:/Users/firdaus/APA/agent/file_safety.py), [agent/redact.py](file:///C:/Users/firdaus/APA/agent/redact.py) | **ADAPT** | Package as `plugins/apa-security/` (`pre_tool_call` + `post_tool_call` hooks + `/security` slash command). |
| **2. ACP Adapter** | [antigravity/adapter.py](file:///D:/Antigravity-Hermes/antigravity/adapter.py), [antigravity/acp_client.py](file:///D:/Antigravity-Hermes/antigravity/acp_client.py), [antigravity/models.py](file:///D:/Antigravity-Hermes/antigravity/models.py) | [agent/acp_client.py](file:///C:/Users/firdaus/APA/agent/acp_client.py), [agent/acp_openai_bridge.py](file:///C:/Users/firdaus/APA/agent/acp_openai_bridge.py), [providers/base.py](file:///C:/Users/firdaus/APA/providers/base.py) | **ADAPT** | Wrap Legacy `AcpClient` (`agy acp --stdio` + `session/set_config_option`) inside `plugins/model-providers/antigravity/` implementing `ProviderProfile.create_client()` + `acp_openai_bridge.py`. |
| **3. Persistent Memory** | [memory/manager.py](file:///D:/Antigravity-Hermes/memory/manager.py), [memory/db.py](file:///D:/Antigravity-Hermes/memory/db.py), [memory/retrieval.py](file:///D:/Antigravity-Hermes/memory/retrieval.py), [memory/consolidation.py](file:///D:/Antigravity-Hermes/memory/consolidation.py) | [hermes_state.py](file:///C:/Users/firdaus/APA/hermes_state.py) (WAL + FTS5), [agent/memory_manager.py](file:///C:/Users/firdaus/APA/agent/memory_manager.py), [tools/memory_tool.py](file:///C:/Users/firdaus/APA/tools/memory_tool.py) | **REPLACE** (Optional `MemoryProvider` plugin for legacy DB) | Use Hermes's built-in `MEMORY.md`/`USER.md` + `SessionDB` FTS5 as primary; optionally expose Legacy APA's versioned SQLite memory (`database/agent.db`) as a `MemoryProvider` plugin if legacy memory records need migration. |
| **4. Skills Management** | [skills/manager.py](file:///D:/Antigravity-Hermes/skills/manager.py), [skills/discovery.py](file:///D:/Antigravity-Hermes/skills/discovery.py), [skills/sync.py](file:///D:/Antigravity-Hermes/skills/sync.py), `D:\Antigravity-Hermes\skills\*` | [tools/skills_tool.py](file:///C:/Users/firdaus/APA/tools/skills_tool.py), [tools/skill_manager_tool.py](file:///C:/Users/firdaus/APA/tools/skill_manager_tool.py), `C:\Users\firdaus\APA\skills\*` | **REPLACE Engine / REUSE Skill Files** | Replace Legacy `SkillsManager` Python code with Hermes's native progressive-disclosure skill system; **reuse** the 12 domain `SKILL.md` files from `D:\Antigravity-Hermes\skills\` via `skills.external_dirs` or `$HERMES_HOME/skills/`. |
| **5. Configuration System** | [config/settings.py](file:///D:/Antigravity-Hermes/config/settings.py) (`AppConfig`, Pydantic `config.yaml`) | [hermes_cli/config.py](file:///C:/Users/firdaus/APA/hermes_cli/config.py), `get_hermes_home() / "config.yaml"` | **REPLACE** (Move APA keys to plugin config schemas) | Use Hermes's profile-aware `config.yaml` (`hermes_cli/config.py`); declare APA-specific settings (`strict_boundary`, `auto_backup_on_delete`, `timeouts`) in `plugin.yaml` `config_schema`. |
| **6. Timeout Recovery** | [antigravity/timeout.py](file:///D:/Antigravity-Hermes/antigravity/timeout.py), [antigravity/recovery.py](file:///D:/Antigravity-Hermes/antigravity/recovery.py), [antigravity/verification.py](file:///D:/Antigravity-Hermes/antigravity/verification.py) | [hermes_cli/timeouts.py](file:///C:/Users/firdaus/APA/hermes_cli/timeouts.py), [agent/process_transport.py](file:///C:/Users/firdaus/APA/agent/process_transport.py) | **ADAPT** (`timeout.py`) / **DISCARD** (`recovery.py` side-effect verifier) | Reuse `classify_prompt_complexity()` and `resolve_prompt_timeout()` from `timeout.py` inside the Antigravity provider client. Discard `recovery.py` because Hermes executes tools locally in `model_tools.py` (not inside `agy`), so tool completion state is already known deterministically. |
| **7. Test Suite** | `D:\Antigravity-Hermes\tests\test_*.py` (55 test files) | `C:\Users\firdaus\APA\tests\` (`scripts/run_tests.sh`) | **ADAPT** (Provider & Security tests only) | Port `test_acp_client.py`, `test_antigravity_adapter.py`, `test_timeout_recovery.py`, and `test_security.py` into Hermes behaviour-contract tests; discard tests for replaced legacy subsystems. |
| **8. Orchestration & Workers** | [agents/orchestrator.py](file:///D:/Antigravity-Hermes/agents/orchestrator.py), [agents/planner.py](file:///D:/Antigravity-Hermes/agents/planner.py), [workers/manager.py](file:///D:/Antigravity-Hermes/workers/manager.py) | [run_agent.py](file:///C:/Users/firdaus/APA/run_agent.py), [tools/delegate_tool.py](file:///C:/Users/firdaus/APA/tools/delegate_tool.py), [hermes_cli/kanban.py](file:///C:/Users/firdaus/APA/hermes_cli/kanban.py), [tools/todo_tool.py](file:///C:/Users/firdaus/APA/tools/todo_tool.py) | **REPLACE** | Replace with Hermes Core (`AIAgent` turn loop, `todo` planning tool, `delegate_task` multi-agent subagents, and SQLite Kanban worker dispatcher). |

---

## 2. Detailed Component Evaluations

### 2.1 Security Guard (`D:\Antigravity-Hermes\security\`) — **ADAPT**
- **What Legacy APA Has:**
  - `BoundaryValidator` ([security/boundary.py](file:///D:/Antigravity-Hermes/security/boundary.py#L8-L53)): Strictly confines file paths to `workspace_root` (`self.root_dir not in resolved_target.parents`), blocks poison null bytes (`\0`), and rejects Windows UNC paths (`\\server\share`, `//`).
  - `PolicyChecker` ([security/policy.py](file:///D:/Antigravity-Hermes/security/policy.py#L14-L51)): Evaluates commands into `SAFE`, `CONFIRMATION_REQUIRED`, or `BLOCKED` (`format`, `rmdir /s /q c:`, `diskpart`, `rm -rf /`).
  - `BackupManager` ([security/backup.py](file:///D:/Antigravity-Hermes/security/backup.py)): Creates automatic pre-deletion backups before destructive file/memory deletions.
  - `AuditLogger` ([security/audit.py](file:///D:/Antigravity-Hermes/security/audit.py)): Writes structured `audit.jsonl` records with credential masking.
- **How It Complements Hermes:**
  - Hermes's `agent/file_safety.py` is a *credential-denylist* guard (blocks `~/.ssh`, `~/.hermes/auth.json`, `.env`), not a *strict workspace containment* jail.
  - By adapting `BoundaryValidator`, `PolicyChecker`, `BackupManager`, and `AuditLogger` into a Hermes plugin (`plugins/apa-security/`) hooked into `pre_tool_call` and `post_tool_call`, APA gains strict workspace boundary enforcement, pre-deletion backups, and a dedicated JSONL audit log without modifying Hermes core.

### 2.2 ACP Adapter (`D:\Antigravity-Hermes\antigravity\`) — **ADAPT**
- **What Legacy APA Has:**
  - `AcpClient` ([antigravity/acp_client.py](file:///D:/Antigravity-Hermes/antigravity/acp_client.py#L44-L520)): Spawns `["agy", "acp", "--stdio"]`, manages NDJSON request/response queues, handles `session/update` (`agent_message_chunk`, `tool_call`), supports `session/set_config_option` (`model`, `thought_level`), and detects `AcpAuthRequiredError` (`-32001`, `401`, `403`).
  - `OfficialAntigravityAdapter` ([antigravity/adapter.py](file:///D:/Antigravity-Hermes/antigravity/adapter.py#L227-L600)): Caches `agy --version` probes (`_GLOBAL_BINARY_PROBE_CACHE`), validates state transitions (`VALID_STATE_TRANSITIONS`), and manages session context.
- **How to Adapt into Hermes:**
  - Combine Legacy `AcpClient`'s `agy.exe acp --stdio` process management and `session/set_config_option` support with Hermes's `agent/acp_openai_bridge.py` (`<tool_call>` XML schema bridge) inside `plugins/model-providers/antigravity/client.py`.

### 2.3 Persistent Memory (`D:\Antigravity-Hermes\memory\`) — **REPLACE (with Optional Migration Script/Plugin)**
- **Comparison:**
  - Legacy APA uses `database/agent.db` (`memories`, `memory_versions`, `memory_sources`, `memories_fts`) with manual CRUD commands (`apa memory add/list/search`).
  - Hermes uses `state.db` (`sessions`, `messages`, `messages_fts` via `session_search`), bounded `MEMORY.md` / `USER.md` injected into the cached system prompt (`tools/memory_tool.py`), and `MemoryManager` (`agent/memory_manager.py`) which automatically scrubs secrets (`_redact_for_provider`) and fans out to `MemoryProvider` plugins.
- **Recommendation:** Use Hermes's native memory architecture as primary. If existing items in `D:\Antigravity-Hermes\database\agent.db` are needed, import high-value facts into `$HERMES_HOME/memories/MEMORY.md` or wrap `memory/db.py` in a lightweight `MemoryProvider` plugin.

### 2.4 Skills Management (`D:\Antigravity-Hermes\skills\`) — **REPLACE Engine / REUSE Skill Content**
- **Comparison:**
  - Legacy APA stores skills in `D:\Antigravity-Hermes\skills\<category>\<name>\SKILL.md` (`coding/python-clean-code`, `debugging/systematic-debugging`, `testing/pytest-automation`, `security/safe-refactoring`, etc.) and indexes them in SQLite.
  - Hermes natively discovers `<category>/<name>/SKILL.md` directories, supports progressive disclosure (`skills_list`, `skill_view`), slash invocation (`/<skill-name>`), and autonomous skill creation (`skill_manage`).
- **Recommendation:** Discard the legacy `SkillsManager` Python class, and mount `D:\Antigravity-Hermes\skills` directly into Hermes via `skills.external_dirs` in `config.yaml` (or copy the skill folders into `$HERMES_HOME/skills/`).

### 2.5 Timeout Recovery (`D:\Antigravity-Hermes\antigravity\timeout.py` & `recovery.py`) — **ADAPT `timeout.py` / DISCARD `recovery.py`**
- **Rationale:** Legacy APA needed `TaskStateRecoveryEngine` (`recovery.py`) and `ResultVerifier` (`verification.py`) because `agy acp` executed file/terminal tools *inside* the `agy.exe` black-box process, so when `agy` timed out, Legacy APA had to stat disk files to guess whether `agy` had finished writing them.
- In the new APA architecture on Hermes, **Hermes Agent Engine** executes all tools in-process via `model_tools.handle_function_call()`. Every tool call's start, completion, and output are tracked deterministically by Hermes. Only `timeout.py`'s adaptive prompt timeout calculation (`90s` simple / `180s` normal coding / `300s` complex coding) should be adapted into the Antigravity provider plugin.
