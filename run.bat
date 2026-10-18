@echo off
title MusicFlow Web App
echo ========================================================
echo  Starting MusicFlow Web App (Ad-Free Player + Downloader)
echo ========================================================
echo.
echo Installing requirements if needed...
python -m pip install -r requirements.txt
echo.
echo Starting web server at http://localhost:8080 ...
start http://localhost:8080
python app.py
pause
