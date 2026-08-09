@echo off
REM connect-AI launcher: agent runtime + Workspace GUI in one window.
REM First run bootstraps everything (Python venv + deps + GUI packages); later
REM runs skip straight to launching. Ctrl+C stops both children.
REM Runtime log: logs\connect-ai-server.log
REM
REM URLs after startup:
REM   http://localhost:1420            Workspace GUI (main UI)
REM   http://127.0.0.1:8765/v1/health  connect-ai-server health
REM   http://127.0.0.1:8766/google/wizard      Google sign-in wizard

setlocal
set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%ROOT%.env" (
  echo [error] .env not found. Copy .env.example to .env and fill in GEMINI_API_KEY.
  pause & exit /b 1
)

REM ---- 1. Python env (root .venv — the ONE environment this project uses) ----
if not exist "%PY%" (
  echo [setup] Creating Python environment in .venv ...
  where uv >nul 2>&1
  if errorlevel 1 (
    py -3.13 -m venv "%VENV%" 2>nul || py -3 -m venv "%VENV%"
  ) else (
    REM --seed puts pip in the venv; launch.py needs it to add bridge deps later.
    uv venv --seed --python 3.13 "%VENV%"
  )
  if not exist "%PY%" (
    echo [error] Could not create %VENV% — install Python 3.13 from python.org and retry.
    pause & exit /b 1
  )
)

REM connect-ai-server.exe only exists once the package is installed.
if not exist "%VENV%\Scripts\connect-ai-server.exe" (
  echo [setup] Installing Python dependencies ^(a few minutes on first run^) ...
  where uv >nul 2>&1
  if errorlevel 1 (
    "%PY%" -m pip install --upgrade pip -q
    "%PY%" -m pip install -e "%ROOT%connect-ai[messaging,browser,bedrock,dev]"
  ) else (
    uv pip install --python "%PY%" -e "%ROOT%connect-ai[messaging,browser,bedrock,dev]"
  )
  if not exist "%VENV%\Scripts\connect-ai-server.exe" (
    echo [error] Dependency install failed — see the output above.
    pause & exit /b 1
  )
)

REM ---- 2. GUI packages ----
if not exist "%ROOT%connect-ai\surfaces\gui\node_modules" (
  echo [setup] Installing GUI packages ^(npm install^) ...
  pushd "%ROOT%connect-ai\surfaces\gui"
  call npm install --no-audit --no-fund
  popd
)

REM ---- 3. Playwright browser (needed by the 13 browser tools) ----
if not exist "%LOCALAPPDATA%\ms-playwright" (
  echo [setup] Downloading Chromium for the browser tools ...
  "%PY%" -m playwright install chromium
)

REM ---- 4. Go ----
"%PY%" "%ROOT%launch.py"

endlocal
