@echo off
setlocal
cd /d "%~dp0"

python -m pip install -U pip pyinstaller pillow

rem Do NOT hidden-import a module named "features".
rem Pillow ships PIL.features; PyInstaller will bind that name and
rem the exe then dies with: ModuleNotFoundError: No module named 'features'

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "MTGA Deck Overlay" ^
  --icon icon.ico ^
  --paths . ^
  --hidden-import core ^
  --hidden-import mtga_features ^
  --hidden-import ui ^
  --hidden-import match_recap ^
  --hidden-import PIL ^
  --hidden-import PIL.Image ^
  --hidden-import PIL.ImageTk ^
  --hidden-import PIL.ImageDraw ^
  --hidden-import tkinter ^
  --hidden-import tkinter.ttk ^
  --hidden-import tkinter.filedialog ^
  --hidden-import sqlite3 ^
  overlay.py

echo.
echo Built: dist\MTGA Deck Overlay.exe
echo Don't run this from an Administrator prompt.
pause
