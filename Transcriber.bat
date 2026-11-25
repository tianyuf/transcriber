@echo off
rem Double-click this file to open Transcriber in your web browser (Windows).
rem The first start downloads what Transcriber needs, which takes a minute.
cd /d "%~dp0"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>nul
if not errorlevel 1 goto run
echo Transcriber needs a small free helper program called uv, which installs and runs it.
echo It comes from https://astral.sh/uv and goes into your user folder; nothing else on the computer changes.
set "answer="
set /p answer="Install uv now? [y/N] "
if /i not "%answer%"=="y" goto declined
powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
goto run

:declined
echo Nothing was installed.
pause
exit /b 1

:run
uv run --project . transcriber app
if errorlevel 1 (
  echo.
  echo Transcriber stopped with an error, shown above.
  pause
)
