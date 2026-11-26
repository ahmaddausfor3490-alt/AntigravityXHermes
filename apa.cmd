@echo off
setlocal
set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"
set "HERMES_HOME=%REPO%\data\hermes-home"
set "UV_PROJECT_ENVIRONMENT=%REPO%\.venv"
set "PATH=%REPO%\bin;%PATH%"
set "PY=%REPO%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo Error: APA Python not found at %PY%
    exit /b 1
)

"%PY%" "%REPO%\apa.py" %*
exit /b %ERRORLEVEL%
