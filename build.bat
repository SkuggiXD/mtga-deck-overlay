@echo off
setlocal
cd /d "%~dp0"

python -m pip install -U pip pyinstaller pillow

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "MTGA Deck Overlay" ^
  --icon icon.ico ^
  --hidden-import PIL ^
  --hidden-import PIL.Image ^
  --hidden-import PIL.ImageTk ^
  --hidden-import PIL.ImageDraw ^
  --hidden-import PIL.ImageFont ^
  --hidden-import tkinter ^
  --hidden-import tkinter.ttk ^
  --hidden-import tkinter.filedialog ^
  --hidden-import sqlite3 ^
  --hidden-import core ^
  --hidden-import features ^
  --hidden-import ui ^
  --hidden-import ctypes ^
  --collect-all PIL ^
  --collect-submodules tkinter ^
  overlay.py

echo.
echo Built: dist\MTGA Deck Overlay.exe
echo Don't run this from an Administrator prompt.
pause
