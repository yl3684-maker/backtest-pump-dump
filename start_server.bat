@echo off
echo ============================================
echo   Momentum Backtest Server
echo ============================================
echo.

REM Get local IP for display
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do (
    set LOCAL_IP=%%a
    goto :found
)
:found
set LOCAL_IP=%LOCAL_IP: =%

echo   Local access:   http://localhost:8501
echo   Network access: http://%LOCAL_IP%:8501
echo.
echo   Password is set in .streamlit\secrets.toml
echo   Press Ctrl+C to stop the server.
echo.

cd /d "%~dp0"
streamlit run ui/streamlit_app.py

pause
