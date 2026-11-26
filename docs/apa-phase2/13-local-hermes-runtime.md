# APA Phase 2.12.2 — Local Hermes Runtime Bootstrap

## 1. Executive Summary

We inspected `CONTRIBUTING.md`, `activate.ps1`, `pyproject.toml`, and `hermes` (the repository root launcher script) to verify how Hermes Agent boots from a local checkout on Windows PowerShell without modifying the global Hermes installation or running `uv sync` / `pip install`.

- **Bootstrap Status**: **PASS**
- **New Dependency Installations Required**: **NONE** (all core runtime dependencies are already present in the existing PM Python 3.14.9 virtual environment).

---

## 2. How the Official Repository Launcher Works

Inspection of `activate.ps1` (`C:\Users\firdaus\APA\activate.ps1:L50-128`) and the repository entry script `C:\Users\firdaus\APA\hermes` shows that a local checkout runs with:
1. `PYTHONPATH=C:\Users\firdaus\APA` so Python imports all Hermes packages (`cli`, `run_agent`, `model_tools`, `hermes_cli`, `agent`, `tools`, `providers`, `plugins`) directly from `C:\Users\firdaus\APA`.
2. `__HERMES_ACTIVATED=1` so the `C:\Users\firdaus\APA\hermes` entry script executes directly inside the selected Python 3.14.9 interpreter without re-execing into the global installation.
3. An isolated `HERMES_HOME` (or `set_hermes_home_override`) so runtime validation never mutates the user's production `~/.hermes` directory.

---

## 3. Verification of Module Origins & Local CLI Execution

### 3.1 Module Origin Verification
Executing Python `3.14.9` (`%LOCALAPPDATA%\hermes\installs\615367502c44fc9d\environments\9e54dc7e12904b269cd5e9645a10405e\venv\Scripts\python.exe`) with `C:\Users\firdaus\APA` at the front of `sys.path` / `PYTHONPATH`:

```text
cli:         C:\Users\firdaus\APA\cli.py
run_agent:   C:\Users\firdaus\APA\run_agent.py
model_tools: C:\Users\firdaus\APA\model_tools.py
main:        C:\Users\firdaus\APA\hermes_cli\main.py
providers:   C:\Users\firdaus\APA\providers\__init__.py
```

- Zero modules are loaded from `D:\Antigravity-Hermes`.
- Zero application modules are loaded from `%LOCALAPPDATA%\hermes\installs\...\app`.

### 3.2 Local Hermes CLI Entrypoint Verification

Command executed:
```powershell
$env:PYTHONPATH = "C:\Users\firdaus\APA"
$env:__HERMES_ACTIVATED = "1"
& "$env:LOCALAPPDATA\hermes\installs\615367502c44fc9d\environments\9e54dc7e12904b269cd5e9645a10405e\venv\Scripts\python.exe" "C:\Users\firdaus\APA\hermes" --version
```

Actual output:
```text
Hermes Agent v0.23.1+7828.g77a30e9 (2026.9.24) · stable · local 77a30e912799
Install directory: C:\Users\firdaus\APA
Install method: git
Python: 3.14.9
OpenAI SDK: 2.54.0
Up to date
```

### Acceptance Checklist
- [x] Import modules succeed from `C:\Users\firdaus\APA`.
- [x] CLI entry point (`C:\Users\firdaus\APA\hermes` and `python -m hermes_cli.main`) succeeds and reports `Install directory: C:\Users\firdaus\APA` and commit `77a30e912799`.
- [x] Runtime does not load Hermes packages from the global installation.
- [x] Runtime does not load `main.py` or any module from `D:\Antigravity-Hermes`.
- [x] Python interpreter is `3.14.9`.
