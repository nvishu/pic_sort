@echo off
rem Double-click to open the photo sorter on Windows.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 start.py %* & goto :eof)
python start.py %*
