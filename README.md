# MTGA Deck Overlay

A small always-on-top desktop overlay that shows the cards still in **your currently selected / in-match deck**.

It only **reads** Arena's `Player.log`. It does not inject into the process, does not read memory, and does not play cards for you.

## What you get

- Live list of remaining cards while a match is running (library tracker)
- Opponent public-zone tracker + Goldfish meta guess
- OBS HTML overlays at 480x1080 (`%LOCALAPPDATA%\\MTGADeckOverlay\\obs`)
- Count + chance the next card drawn is that card
- Drag the header to move, minimize, opacity slider, load a `.txt` deck as fallback

## Setup (Windows)

1. Install [Python 3.10+](https://www.python.org/downloads/) and tick **Add Python to PATH**.
2. In MTG Arena: Gear → **View Account** → enable **Detailed Logs (Plugin Support)** → restart Arena.
3. Run Arena windowed or borderless.
4. Double-click `run.bat`, or `python overlay.py`.

Build an exe: `build.bat` (needs PyInstaller + Pillow).

OBS Browser Sources: `playerdeck.html` and `oppdeck.html` in `%LOCALAPPDATA%\\MTGADeckOverlay\\obs` at **480 × 1080**.

Unofficial fan content. Not affiliated with Wizards of the Coast.
