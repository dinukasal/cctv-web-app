@echo off
cd /d C:\go2rtc\www
"C:\Python311\python.exe" -m http.server 8880 --bind 0.0.0.0 --directory "C:\go2rtc\www" >> "C:\go2rtc\dashboard.log" 2>&1