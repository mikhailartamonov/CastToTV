@echo off
echo Building CastToTV...
pyinstaller --onefile --windowed --name CastToTV --icon=NONE cast_to_tv.py
echo.
echo Done! Executable: dist\CastToTV.exe
pause
