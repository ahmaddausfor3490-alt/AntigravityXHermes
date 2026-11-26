# 05 — Windows Compatibility Audit

**Project:** APA (Antigravity Personal Agent) — Phase 1 Audit  
**Repository:** `C:\Users\firdaus\APA` (`apa-development`)  
**Target Platform:** Windows 10/11 (PowerShell & Git Bash)

---

## 1. Executive Assessment

Hermes Agent already includes extensive first-class Windows engineering (`hermes_bootstrap.py`, `hermes_cli/_subprocess_compat.py`, `hermes_dir_acl.py`, `tools/environments/local.py`, and `@pytest.mark.platforms("windows")` / `wine2e` test suites). However, several POSIX-oriented defaults and edge cases require attention for a rock-solid Windows-first APA release.

---

## 2. Subsystem-by-Subsystem Windows Audit

| Area | Key Files & Lines | Current Windows Implementation | Potential Issues / Edge Cases |
|---|---|---|---|
| **UTF-8 & Stdio Bootstrap** | [run_agent.py](file:///C:/Users/firdaus/APA/run_agent.py#L9-L15), `hermes_bootstrap.py` | Forces UTF-8 encoding on `sys.stdin`, `sys.stdout`, `sys.stderr` and sets `PYTHONUTF8=1` before any module imports. | Subprocesses spawned without `encoding="utf-8", errors="replace"` may default to `cp1252` on non-English Windows locales. |
| **Shell & PowerShell Execution** | [tools/environments/local.py](file:///C:/Users/firdaus/APA/tools/environments/local.py#L140-L350) | Prefers Git Bash (`bash.exe`) on Windows so POSIX agent commands work transparently, translating `C:\...` ↔ `/c/...` via `_windows_to_msys_path()` and `_msys_to_windows_path()`. | When users or skills run native PowerShell cmdlets (`Get-Process`, `Set-ExecutionPolicy`), Git Bash wraps them unless invoked via `powershell.exe -NoProfile -Command`. |
| **Subprocess Creation & `.cmd` Shims** | [hermes_cli/_subprocess_compat.py](file:///C:/Users/firdaus/APA/hermes_cli/_subprocess_compat.py#L96-L250) | Provides `split_command_line()` (`posix=False` so `C:\path` backslashes aren't stripped), `resolve_node_command()` (resolves `.cmd`/`.bat` shims via `shutil.which` to avoid `WinError 193`), and `windows_hide_flags()` (`CREATE_NO_WINDOW`). | `agy` is installed both as `agy.exe` (`%LOCALAPPDATA%\agy\bin\agy.exe`) and sometimes wrapped by `.cmd` shims. Always resolve `agy` via `shutil.which("agy.exe") or shutil.which("agy")`. |
| **Process Groups & Termination** | [hermes_cli/process_identity.py](file:///C:/Users/firdaus/APA/hermes_cli/process_identity.py), [hermes_cli/_subprocess_compat.py](file:///C:/Users/firdaus/APA/hermes_cli/_subprocess_compat.py#L35-L40) | Replaces POSIX `start_new_session=True` / `os.killpg()` with `CREATE_NEW_PROCESS_GROUP \| CREATE_NO_WINDOW` and Windows Job Objects / `taskkill /F /T`. | Direct `subprocess.Popen.terminate()` in custom plugins only kills the immediate PID; child trees survive unless Job Objects or `taskkill /F /T /PID` are used. |
| **PTY / Interactive Terminals** | [tools/terminal_tool.py](file:///C:/Users/firdaus/APA/tools/terminal_tool.py), `tools/environments/local.py` | POSIX uses `pty.openpty()`; Windows falls back to pipe-based execution or `pywinpty` (`conpty`) when `pty=True` is requested. | `pty=True` on Windows requires `winpty`/`conpty` support; interactive CLI tools (like `agy login`) must inherit the real console (`stdin=None, stdout=None, stderr=None`). |
| **Environment Variable Filtering** | [tools/environments/local_env_policy.py](file:///C:/Users/firdaus/APA/tools/environments/local_env_policy.py) | Case-folds environment variable keys (`_ALWAYS_STRIP_FOLDED`) because Windows `os.environ` is case-insensitive (`Path` vs `PATH`, `SystemRoot`, `APPDATA`, `LOCALAPPDATA`, `USERPROFILE`, `PATHEXT`). | Stripping `SYSTEMROOT`, `COMSPEC`, `LOCALAPPDATA`, or `APPDATA` when spawning `agy.exe` breaks Windows Winsock initialization (`WinError 10106`) and `agy`'s credential lookup under `%APPDATA%` / `%USERPROFILE%\.gemini`. |
| **File Paths & Case Sensitivity** | [agent/file_safety.py](file:///C:/Users/firdaus/APA/agent/file_safety.py#L82-L95) | `deny_key()` uses `.casefold()` on `win32` (`hermes_platform.host.facts.os_family()`) so `~/.SSH` and `~/.ssh` are both blocked on case-insensitive NTFS. | Windows `Path.resolve()` can return `\\?\` extended-length prefixes on some Python versions or fail on reserved device names (`CON`, `NUL`, `AUX`). |
| **Directory ACLs & File Locking** | [hermes_dir_acl.py](file:///C:/Users/firdaus/APA/hermes_dir_acl.py), [hermes_state_wal.py](file:///C:/Users/firdaus/APA/hermes_state_wal.py) | Uses Windows `icacls` / ACL helpers in `make_user_dir()` where POSIX `chmod(0o700)` is a no-op; `SessionDB` handles Windows SQLite WAL file-locking semantics (`-wal` / `-shm` cannot be unlinked while any handle is open). | Deleting or moving a SQLite DB or log file on Windows while a background thread holds an open file descriptor raises `PermissionError: [WinError 32]`. |
| **Windows Credential Manager** | [hermes_cli/auth.py](file:///C:/Users/firdaus/APA/hermes_cli/auth.py) | Hermes stores its own provider state in `get_hermes_home() / "auth.json"`, while `agy.exe` manages its own Google OAuth credentials natively on Windows. | No direct `keyring` / `win32cred` dependency is required in APA if `agy.exe` manages its own auth store. |
| **Terminal UI & REPL** | [cli.py](file:///C:/Users/firdaus/APA/cli.py), [hermes_cli/tui.py](file:///C:/Users/firdaus/APA/hermes_cli/tui.py) | Uses `prompt_toolkit` + `rich` with Windows VT100 console mode enabled. | Legacy Windows Console (`conhost.exe` without VT support) can garble ANSI escape sequences; Windows Terminal (`wt.exe`) and PowerShell 7+ render cleanly. |
| **Browser Automation** | [tools/browser_tool.py](file:///C:/Users/firdaus/APA/tools/browser_tool.py), [tools/browser_cdp.py](file:///C:/Users/firdaus/APA/tools/browser_cdp.py) | Connects to Chrome/Edge via Chrome DevTools Protocol (CDP) WebSocket or `agent-browser` CLI. | Needs Windows Chrome/Edge executable discovery (`%ProgramFiles%\Google\Chrome\Application\chrome.exe` or Edge fallback) when `agent-browser` is not installed. |
| **Package & Env Management** | [activate.ps1](file:///C:/Users/firdaus/APA/activate.ps1), `pm/` | Ships `activate.ps1` alongside `activate` (bash) and `activate.fish` for provisioning the `uv`-managed PM environment on Windows PowerShell. | `scripts/run_tests.sh` is a bash script (`#!/usr/bin/env bash`), requiring Git Bash on Windows to run the test wrapper. |

---

## 3. Linux-Specific Assumptions Identified & Required Safeguards for APA

1. **Test Runner Script (`scripts/run_tests.sh`):**
   - *Assumption:* Written in Bash (`scripts/run_tests.sh`).
   - *Safeguard:* On Windows PowerShell, invoke via `"$env:ProgramFiles\Git\bin\bash.exe" scripts/run_tests.sh` or provide a thin `scripts/run_tests.ps1` wrapper for APA developers.
2. **Spawning `agy.exe` Subprocesses (`agent/acp_client.py` / APA Antigravity Plugin):**
   - *Assumption:* `ACPClient._ensure_started()` ([agent/acp_client.py](file:///C:/Users/firdaus/APA/agent/acp_client.py#L188)) calls `acp.spawn_agent_process(client, self._binary)` without `windows_hide_flags()`, which can flash console windows when launched from GUI/TUI contexts on Windows.
   - *Safeguard:* In the APA Antigravity provider plugin (`plugins/model-providers/antigravity/`), resolve `agy.exe` explicitly (`%LOCALAPPDATA%\agy\bin\agy.exe` fallback), pass `creationflags=windows_hide_flags()`, preserve essential Windows env vars (`SYSTEMROOT`, `USERPROFILE`, `LOCALAPPDATA`, `APPDATA`, `PATH`, `TEMP`), and force `encoding="utf-8", errors="replace"` on stdio pipes.
3. **PowerShell vs Git Bash Command Guarding:**
   - *Assumption:* Several destructive command regexes in Legacy APA (`security/policy.py`) only match POSIX `rm -rf` or basic `del /f /s /q`.
   - *Safeguard:* Ensure APA Security Guard patterns also cover PowerShell destructive cmdlets (`Remove-Item -Recurse -Force`, `Format-Volume`, `Clear-Disk`, `Stop-Process -Force`).
