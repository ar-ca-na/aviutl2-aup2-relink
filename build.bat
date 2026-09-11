@echo off
cd /d "%~dp0"
pyinstaller --noconfirm --clean --onefile --noconsole --name aup2relink --collect-all tkinterdnd2 aup2relink.py
