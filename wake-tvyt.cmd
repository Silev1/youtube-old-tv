@echo off
rem wake-tvyt.cmd — включить сервер tvyt по локальной сети.
rem Двойной щелчок или: wake-tvyt.cmd
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wake-tvyt.ps1" %*
pause
