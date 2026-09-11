@echo off
cd /d "%~dp0"
echo Packaging Hestia POS Bill Printer as .exe...
npx electron-builder --win --x64
echo.
echo Done. Check the dist/ folder for the .exe
echo.
pause
