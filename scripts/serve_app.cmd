@echo off
REM Start the AI-Investo API and PWA.
REM
REM Binds 0.0.0.0 so the app is reachable over Tailscale (100.x) as well as the
REM local network. Tailscale is the one that works away from home; the LAN
REM address only works on the same Wi-Fi.

setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"
set "PYTHONPATH=%ROOT%"
set "PYTHONUTF8=1"

echo.
echo   AI-Investo
echo   ----------
for /f "tokens=*" %%i in ('"C:\Program Files\Tailscale\tailscale.exe" ip -4 2^>nul') do set "TSIP=%%i"
if defined TSIP (
  echo   Tailscale : http://%TSIP%:8000
) else (
  echo   Tailscale : not connected
)
echo   Local     : http://localhost:8000
echo.
echo   Add to Home Screen on your phone to install it.
echo   Ctrl+C to stop.
echo.

"%ROOT%\.venv\Scripts\python.exe" -m uvicorn api.main:app --host 0.0.0.0 --port 8000
