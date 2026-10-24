@echo off
rem Starts the StS2 advisor overlay. Rebuilds the reader first (fast when nothing changed).
rem The overlay exits with code 3 when its source changed (auto-update): rebuild and relaunch on the new code.
cd /d "%~dp0"
:start
rem Close any earlier overlay + reader (a leftover reader locks the exe and breaks the build).
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*advisor\overlay.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
taskkill /im Sts2Reader.exe /f >nul 2>&1
dotnet build reader -c Release -v q -nologo || (pause & exit /b 1)
python advisor\overlay.py
if %errorlevel%==3 goto start
if errorlevel 1 pause
