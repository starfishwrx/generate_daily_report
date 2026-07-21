@echo off
setlocal

set "APP_VERSION=1.5.0"
set "RELEASE_DIR=dist\windows-release-v1.5.0"

if /I "%AUTODATAREPORT_INCLUDE_PUBLISH_CONFIG%"=="1" if not defined AUTODATAREPORT_INTERNAL_CONFIG (
    echo AUTODATAREPORT_INCLUDE_PUBLISH_CONFIG=1 requires AUTODATAREPORT_INTERNAL_CONFIG.
    goto :fail
)

where uv >nul 2>&1
if errorlevel 1 (
    echo uv is required for reproducible builds. Install uv and retry.
    goto :fail
)

echo Syncing the locked build environment...
uv sync --frozen --group build
if errorlevel 1 goto :fail
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"

echo Cleaning previous V1.5 build output...
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

if exist build\internal rmdir /s /q build\internal
if defined AUTODATAREPORT_INTERNAL_CONFIG (
    echo Preparing sanitized internal platform defaults...
    if /I "%AUTODATAREPORT_INCLUDE_PUBLISH_CONFIG%"=="1" (
        echo Internal publishing profile enabled.
        if defined AUTODATAREPORT_PUBLISH_ENV (
            %PYTHON_EXE% scripts\prepare_internal_defaults.py "%AUTODATAREPORT_INTERNAL_CONFIG%" build\internal --include-publish-settings --publish-env-file "%AUTODATAREPORT_PUBLISH_ENV%" --publish-revision 1
        ) else (
            %PYTHON_EXE% scripts\prepare_internal_defaults.py "%AUTODATAREPORT_INTERNAL_CONFIG%" build\internal --include-publish-settings --publish-revision 1
        )
    ) else (
        %PYTHON_EXE% scripts\prepare_internal_defaults.py "%AUTODATAREPORT_INTERNAL_CONFIG%" build\internal
    )
    if errorlevel 1 goto :fail
) else (
    echo Public build: AUTODATAREPORT_INTERNAL_CONFIG is not set.
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
xcopy /e /i /y dist\autodatareport-gui\_internal\* "%RELEASE_DIR%\_internal\" >nul
if errorlevel 1 goto :fail

echo Writing release manifest and hashes...
if /I "%AUTODATAREPORT_INCLUDE_PUBLISH_CONFIG%"=="1" (
    %PYTHON_EXE% scripts\write_release_manifest.py "%RELEASE_DIR%" "%APP_VERSION%" --allow-internal-publish-config
) else (
    %PYTHON_EXE% scripts\write_release_manifest.py "%RELEASE_DIR%" "%APP_VERSION%"
)
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
