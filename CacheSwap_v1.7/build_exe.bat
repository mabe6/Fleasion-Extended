@echo off
rem Builds a single CacheSwap.exe in the dist folder.
cd /d "%~dp0"
py -m pip install -r requirements.txt pyinstaller
py -m PyInstaller --noconfirm --onefile --windowed --name CacheSwap app.py
echo.
echo Done. The exe is in the dist folder.
pause
