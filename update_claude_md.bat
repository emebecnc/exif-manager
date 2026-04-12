@echo off
REM Actualizar CLAUDE.md con estado actual del proyecto
REM Ejecutar con doble-click

cd /d "%~dp0"

echo [%date% %time%] Actualizando CLAUDE.md...

python update_claude_md.py

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ✅ CLAUDE.md actualizado exitosamente
    echo.
    echo Proximos pasos:
    echo 1. git add CLAUDE.md
    echo 2. git commit -m "docs: update CLAUDE.md"
    echo 3. git push
    echo.
    pause
) else (
    echo.
    echo ❌ Error al actualizar CLAUDE.md
    pause
)
