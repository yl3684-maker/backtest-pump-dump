@echo off
echo ============================================
echo   Momentum Backtest — Public Tunnel (ngrok)
echo ============================================
echo.
echo   Requirements: ngrok must be installed and authenticated.
echo   Get it free at: https://ngrok.com/download
echo   After install run: ngrok config add-authtoken YOUR_TOKEN
echo.

cd /d "%~dp0"

REM Start Streamlit in background
start "Streamlit" /min streamlit run ui/streamlit_app.py

REM Wait a moment for Streamlit to initialise
timeout /t 4 /nobreak > nul

echo   Starting ngrok tunnel on port 8501...
echo   Look for the "Forwarding" line below — that is your public URL.
echo   Share that URL + the password from .streamlit\secrets.toml
echo.
ngrok http 8501

pause
