@echo off
REM build_setup.bat — Build AICowork-Setup.exe bằng PyInstaller
REM
REM Cách dùng:
REM   1. Chạy một lần: build_setup.bat
REM   2. File output: dist\AICowork-Setup.exe (~20MB)
REM   3. Gửi file đó cho user — họ double-click, nhập API key, click Start
REM
REM Yêu cầu: Python 3.9+ trong PATH

setlocal
set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "PY=%VENV%\Scripts\python.exe"

REM Dùng venv của project nếu có, fallback system Python
if not exist "%PY%" (
    echo [build] .venv not found, using system Python...
    set "PY=python"
)

echo [build] Checking/Installing PyInstaller...
"%PY%" -m pip install pyinstaller --quiet
if errorlevel 1 (
    echo [error] Could not install PyInstaller. Install Python 3.9+ and retry.
    pause & exit /b 1
)

REM Dọn build cũ
if exist "%ROOT%dist\AICowork-Setup.exe" (
    del "%ROOT%dist\AICowork-Setup.exe"
)
if exist "%ROOT%build" rmdir /s /q "%ROOT%build"

echo [build] Building AICowork-Setup.exe ...
"%PY%" -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "AICowork-Setup" ^
    --add-data "%ROOT%.env.example;." ^
    --hidden-import "tkinter" ^
    --hidden-import "tkinter.ttk" ^
    --hidden-import "tkinter.messagebox" ^
    --hidden-import "urllib.request" ^
    --hidden-import "urllib.error" ^
    --distpath "%ROOT%dist" ^
    --workpath "%ROOT%build" ^
    --noconfirm ^
    "%ROOT%setup_wizard.py"

if errorlevel 1 (
    echo [error] PyInstaller build failed. Check output above.
    pause & exit /b 1
)

REM Cleanup build artifacts (giữ dist/)
if exist "%ROOT%build" rmdir /s /q "%ROOT%build"
if exist "%ROOT%AICowork-Setup.spec" del "%ROOT%AICowork-Setup.spec"

if exist "%ROOT%dist\AICowork-Setup.exe" (
    echo:
    echo [build] SUCCESS!
    echo [build] Output: %ROOT%dist\AICowork-Setup.exe
    echo [build] Size:
    for %%F in ("%ROOT%dist\AICowork-Setup.exe") do echo         %%~zF bytes
    echo:
    echo [build] Gửi file này cho user. Ho double-click, nhap API key, click Start.
    echo [build] App se tu cai dat Python deps lan dau [can internet + Python tren may].
) else (
    echo [error] Build succeeded but output file not found?
    exit /b 1
)

endlocal
pause
