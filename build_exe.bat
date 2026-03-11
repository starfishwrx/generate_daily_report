@echo off
setlocal

if not defined VIRTUAL_ENV (
    echo Activating virtual environment .venv...
    call .venv\Scripts\activate
)

echo Ensuring pyinstaller is available...
python -m pip install --upgrade pyinstaller >nul

echo Cleaning previous Windows release...
if exist dist\windows-release rmdir /s /q dist\windows-release

echo Building CLI executable...
pyinstaller --noconfirm --clean autodatareport-cli.spec
if errorlevel 1 goto :fail

echo Building GUI executable...
pyinstaller --noconfirm --clean autodatareport-gui.spec
if errorlevel 1 goto :fail

echo Assembling Windows release directory...
mkdir dist\windows-release
xcopy /e /i /y dist\autodatareport-cli\* dist\windows-release\ >nul
copy /y dist\autodatareport-gui\autodatareport-gui.exe dist\windows-release\autodatareport-gui.exe >nul
if exist config.yaml copy /y config.yaml dist\windows-release\config.yaml >nul
if exist extra_auth.json copy /y extra_auth.json dist\windows-release\extra_auth.json >nul
if exist hosts_870.yaml copy /y hosts_870.yaml dist\windows-release\hosts_870.yaml >nul
if exist hosts_505.yaml copy /y hosts_505.yaml dist\windows-release\hosts_505.yaml >nul
if exist app.ico copy /y app.ico dist\windows-release\app.ico >nul

echo Build finished.
echo GUI: dist\windows-release\autodatareport-gui.exe
echo CLI: dist\windows-release\autodatareport-cli.exe
echo Release folder: dist\windows-release
goto :eof

:fail
echo Build failed.
exit /b 1

endlocal
