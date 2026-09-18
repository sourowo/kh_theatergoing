@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 抓取最新場次...
py kh_art_cinema_scraper.py
if errorlevel 1 goto fail
echo [2/2] 更新網站...
py update_site.py
if errorlevel 1 goto fail
start "" kh_art_planner.html
pause
exit /b 0
:fail
echo 更新失敗，請看上方訊息
pause
exit /b 1
