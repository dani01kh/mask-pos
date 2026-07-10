@echo off
setlocal
set "SRC=%~dp0"
set "APPDIR=%USERPROFILE%\Desktop\Mask POS"

if not exist "%APPDIR%" mkdir "%APPDIR%"
robocopy "%SRC%" "%APPDIR%" /E /XD "__pycache__" /XF "Install Mask POS To Desktop.bat" >nul
if errorlevel 8 (
    echo Could not copy Mask POS to the Desktop.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$desktop=[Environment]::GetFolderPath('Desktop'); $target='%APPDIR%\MaskPOS.exe'; $shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'OPEN MASK POS.lnk')); $shortcut.TargetPath=$target; $shortcut.WorkingDirectory='%APPDIR%'; $shortcut.IconLocation=$target; $shortcut.Save()"
start "" "%APPDIR%\MaskPOS.exe"
