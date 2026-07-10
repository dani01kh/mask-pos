@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  Mask POS — Desktop Shortcut Installer
::  Run this file once after unzipping the package.
::  It creates a shortcut on the Desktop that opens MaskPOS.exe
::  from whichever folder this .bat file lives in.
:: ============================================================

set "APP_DIR=%~dp0"
:: Remove trailing backslash
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

set "EXE_PATH=%APP_DIR%\MaskPOS.exe"

if not exist "%EXE_PATH%" (
    echo.
    echo  ERROR: MaskPOS.exe was not found in:
    echo         %APP_DIR%
    echo.
    echo  Make sure you run this file from inside the unzipped MaskPOS folder.
    echo.
    pause
    exit /b 1
)

:: Create the shortcut using PowerShell
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$WshShell = New-Object -ComObject WScript.Shell;" ^
  "$desktop = [Environment]::GetFolderPath('Desktop');" ^
  "$lnk = $WshShell.CreateShortcut($desktop + '\Mask POS.lnk');" ^
  "$lnk.TargetPath = '%EXE_PATH:\=\\%';" ^
  "$lnk.WorkingDirectory = '%APP_DIR:\=\\%';" ^
  "$lnk.IconLocation = '%EXE_PATH:\=\\%';" ^
  "$lnk.Description = 'Mask Point of Sale';" ^
  "$lnk.Save();"

if errorlevel 1 (
    echo.
    echo  Could not create shortcut automatically.
    echo  Please manually create a shortcut to:
    echo         %EXE_PATH%
    echo.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo   Mask POS shortcut created on your Desktop successfully!
echo   Double-click "Mask POS" on your Desktop to launch the app.
echo  ============================================================
echo.
timeout /t 3 >nul
