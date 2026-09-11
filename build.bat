@echo off
cd /d "%~dp0"
gcc -shared -O2 -s -o dpihook.dll dpihook.c -luser32 || exit /b 1
pyinstaller --noconfirm --clean --onefile --noconsole --name aup2relink --collect-all tkinterdnd2 --add-binary "dpihook.dll;." aup2relink.py
