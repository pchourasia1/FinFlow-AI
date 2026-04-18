@echo off
rem Double-click launcher for run_services.ps1.
rem RemoteSigned lets this local script run even where the default policy is Restricted.
powershell -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0run_services.ps1" %*
