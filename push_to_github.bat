@echo off
title Push Music App to GitHub
color 0B
echo ================================================================
echo     Push to https://github.com/praneshpranesh0363-netizen/music-app.git
echo ================================================================
echo.
echo GitHub requires authentication to push code to your repository.
echo.
echo Choose an option:
echo [1] Push using GitHub Personal Access Token (PAT) [Fastest & Recommended]
echo [2] Login with GitHub in Browser
echo [3] Exit
echo.
set /p choice="Enter your choice (1, 2, or 3): "

if "%choice%"=="1" goto token_push
if "%choice%"=="2" goto browser_login
if "%choice%"=="3" goto end

:token_push
echo.
echo ----------------------------------------------------------------
echo How to get a GitHub Personal Access Token (takes 30 seconds):
echo 1. Open: https://github.com/settings/tokens/new
echo 2. Check the "repo" checkbox (Full control of repositories)
echo 3. Click "Generate token" and COPY the token (starts with ghp_...)
echo ----------------------------------------------------------------
echo.
set /p token="Paste your GitHub Token here and press Enter: "

if "%token%"=="" (
    echo Token cannot be empty.
    pause
    exit /b
)

echo.
echo Pushing code to GitHub...
git push -u https://%token%@github.com/praneshpranesh0363-netizen/music-app.git main

if %errorlevel% equ 0 (
    echo.
    echo ============================================================
    echo SUCCESS! Your code is now live on GitHub:
    echo https://github.com/praneshpranesh0363-netizen/music-app
    echo ============================================================
) else (
    echo.
    echo Failed to push. Please verify that your token has 'repo' permission.
)
pause
exit /b

:browser_login
echo.
echo Opening browser login...
git push -u origin main
pause
exit /b

:end
exit /b
