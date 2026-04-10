@echo off
cd /d "%~dp0"

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

python main.py
if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: la aplicacion termino con codigo %ERRORLEVEL%
    pause
)
