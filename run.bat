@echo off
REM Start BananaGent. Must use the venv interpreter — the system-wide
REM "python" on this machine is 32-bit and can't load the audio deps.
cd /d "%~dp0"
REM -u keeps the log unbuffered so you see output as it happens.
".venv\Scripts\python.exe" -u main.py
pause
