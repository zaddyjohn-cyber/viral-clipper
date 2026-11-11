@echo off
REM Double-click this to run Zaram Viral Klipa. It starts the server and opens
REM the dashboard in your browser. Close this window to stop it.
cd /d "%~dp0"
title Zaram Viral Klipa
python server.py
if errorlevel 1 (
  echo.
  echo Zaram Viral Klipa stopped with an error. The lines above say why.
  echo If it cannot find python, install it from python.org and tick
  echo "Add python.exe to PATH" during setup.
  echo.
  pause
)
