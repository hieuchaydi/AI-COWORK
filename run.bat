@echo off
REM connect-AI terminal UI. Reads %APPDATA%\coworker\mcp.json on startup
REM and spawns the bridge/*.py MCP servers as subprocesses.
REM GUI version: run-web.bat (Workspace GUI at http://localhost:1420).

setlocal
set "ROOT=%~dp0"

if not exist "%ROOT%.env" (
  echo [error] .env not found. Copy .env.example to .env and fill in your keys.
  pause & exit /b 1
)
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ROOT%.env") do (
  if not "%%A"=="" set "%%A=%%B"
)
if not defined GEMINI_API_KEY (
  echo [error] GEMINI_API_KEY missing in .env
  pause & exit /b 1
)
REM The TUI shares run-web.bat's environment. Bootstrapping lives there (one
REM place, one env) — point the user at it instead of duplicating the install.
if not exist "%ROOT%.venv\Scripts\connect-ai.exe" (
  echo [error] Environment not set up yet. Run run-web.bat once — it creates
  echo         .venv and installs everything — then come back here.
  pause & exit /b 1
)

"%ROOT%.venv\Scripts\connect-ai.exe" --model gemini:gemini-2.5-flash

endlocal
