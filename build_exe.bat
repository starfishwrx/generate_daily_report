@echo off
setlocal

set "APP_VERSION=1.3.0"
set "RELEASE_DIR=dist\windows-release-v1.3.0"

where uv >nul 2>&1
if errorlevel 1 (
    echo uv is required for reproducible builds. Install uv and retry.
    goto :fail
)

echo Syncing the locked build environment...
uv sync --frozen --group build
if errorlevel 1 goto :fail
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"

echo Cleaning previous V1.3 build output...
if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%"
if exist "%RELEASE_DIR%" (
    echo Failed to clean %RELEASE_DIR%. Close running EXE files and try again.
    goto :fail
)
if exist dist\autodatareport-cli rmdir /s /q dist\autodatareport-cli
if exist dist\autodatareport-cli (
    echo Failed to clean dist\autodatareport-cli. Close running CLI files and try again.
    goto :fail
)
if exist dist\autodatareport-gui rmdir /s /q dist\autodatareport-gui
if exist dist\autodatareport-gui (
    echo Failed to clean dist\autodatareport-gui. Close running GUI files and try again.
    goto :fail
)
if exist build\autodatareport-cli rmdir /s /q build\autodatareport-cli
if exist build\autodatareport-cli (
    echo Failed to clean build\autodatareport-cli.
    goto :fail
)
if exist build\autodatareport-gui rmdir /s /q build\autodatareport-gui
if exist build\autodatareport-gui (
    echo Failed to clean build\autodatareport-gui.
    goto :fail
)

echo Building CLI executable...
%PYTHON_EXE% -m PyInstaller --noconfirm --clean autodatareport-cli.spec
if errorlevel 1 goto :fail

echo Building GUI executable...
%PYTHON_EXE% -m PyInstaller --noconfirm --clean autodatareport-gui.spec
if errorlevel 1 goto :fail

echo Assembling Windows release directory...
mkdir "%RELEASE_DIR%"
if errorlevel 1 goto :fail
xcopy /e /i /y dist\autodatareport-cli\* "%RELEASE_DIR%\" >nul
if errorlevel 1 goto :fail
copy /y dist\autodatareport-gui\autodatareport-gui.exe "%RELEASE_DIR%\autodatareport-gui.exe" >nul
if errorlevel 1 goto :fail

echo Writing release manifest and hashes...
%PYTHON_EXE% scripts\write_release_manifest.py "%RELEASE_DIR%" "%APP_VERSION%"
if errorlevel 1 goto :fail

echo Build finished.
echo GUI: %RELEASE_DIR%\autodatareport-gui.exe
echo CLI: %RELEASE_DIR%\autodatareport-cli.exe
echo Release folder: %RELEASE_DIR%
goto :eof

:fail
echo Build failed.
exit /b 1

endlocal
