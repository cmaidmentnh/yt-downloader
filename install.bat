@echo off
:: Install YT Downloader on Windows
:: Run as Administrator

:: Kill any running instance
taskkill /f /im "python.exe" /fi "WINDOWTITLE eq ytdl" 2>nul

:: Add hosts entry if not present
findstr /C:"ytdl" %SystemRoot%\System32\drivers\etc\hosts >nul 2>&1
if errorlevel 1 (
    echo 127.0.0.1 ytdl >> %SystemRoot%\System32\drivers\etc\hosts
)

:: Create a scheduled task to run at logon
schtasks /create /tn "YTDownloader" /tr "pythonw \"%~dp0app.py\"" /sc onlogon /rl highest /f

:: Start it now
start /b pythonw "%~dp0app.py"

echo.
echo Done! Go to http://ytdl
pause
