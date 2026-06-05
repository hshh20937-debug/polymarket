@echo off
cd /d "%~dp0"
echo Installing dependencies...
pip install -r requirements.txt
echo.
echo ============================================
echo  Running Polymarket Demo Bot - First Scan
echo ============================================
python main.py --once
echo.
echo To run continuously: python main.py
echo To view portfolio:   python main.py --report
pause
