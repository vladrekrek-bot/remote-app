@echo off
chcp 65001 >nul
title SHKWIZI Remote
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python не найден. Установи его с python.org и поставь галочку "Add Python to PATH".
  pause
  exit /b
)
echo Проверяю библиотеки...
python -m pip install --quiet --disable-pip-version-check mss pillow aiohttp qrcode
echo Запускаю SHKWIZI Remote...
python shkwizi_remote.py
if errorlevel 1 pause
