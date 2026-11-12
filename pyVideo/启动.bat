@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import PySide6" >nul 2>nul
if errorlevel 1 goto nopyqt
python "video_collector.py"
if errorlevel 1 pause
exit /b

:nopyqt
echo Python or PySide6 is missing.
echo Install them with:  python -m pip install PySide6
echo.
pause
