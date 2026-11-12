@echo off
rem F1 Points Tracker: update the data, then open the dashboard in your browser.
cd /d "%~dp0"
set PY=python
where python >nul 2>nul || set PY=py

echo Updating points from F1DB and lap data from OpenF1...
%PY% update.py

echo.
echo Opening the dashboard. Leave this window open while you use it; close it to stop.
start "" http://localhost:8000/
%PY% -m http.server 8000 --bind 127.0.0.1
