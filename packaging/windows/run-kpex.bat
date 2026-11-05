@echo off
rem Starts the Kingston K-PEX emulator.
rem Put the firmware (KPEX100.rom or gmp-m6.rom) next to this file and run it.
rem On first start a disk image disk-<firmware name>.img is created.
rem Your music, photos, texts and games go into the player-files folder:
rem it is copied to the player on start, and the player's changes come back on exit.

setlocal
cd /d "%~dp0"

set "ROM="
for %%F in (KPEX100.rom gmp-m6.rom) do if not defined ROM if exist "%%F" set "ROM=%%F"
if not defined ROM for %%F in (*.rom) do if not defined ROM set "ROM=%%F"
if not defined ROM (
    echo No firmware found.
    echo Put KPEX100.rom or gmp-m6.rom into this folder:
    echo   %~dp0
    echo and run this file again.
    pause
    exit /b 1
)

for %%F in ("%ROM%") do set "DISK=disk-%%~nF.img"

if not exist "%DISK%" (
    echo Creating disk image %DISK% from %ROM% ...
    "%~dp0pypy\pypy3.exe" tools\mkdisk.py "%ROM%" "%DISK%"
    if errorlevel 1 goto error
)

echo Starting the emulator with %ROM% and %DISK% ...
echo Files for the player go into the player-files folder. Close the emulator
echo window (or press Esc) to save the player's changes back to that folder.
"%~dp0pypy\pypy3.exe" gui.py "%ROM%" --disk "%DISK%" --sync-folder player-files %*
if errorlevel 1 goto error
exit /b 0

:error
echo.
echo The emulator stopped with an error, see the messages above.
pause
exit /b 1
