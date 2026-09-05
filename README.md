# MTGA Deck Overlay

Always-on-top tracker for Magic: The Gathering Arena. Shows cards left in your library, opponent public zones, a Goldfish meta guess, and writes OBS overlays.

It only **reads** `Player.log`. It does not inject into Arena or read memory.

## Download (Windows)

1. Open **[Releases](https://github.com/SkuggiXD/mtga-deck-overlay/releases/latest)**
2. Download `MTGA Deck Overlay.exe`
3. In Arena: gear → **View Account** → enable **Detailed Logs (Plugin Support)** → restart Arena
4. Run Arena windowed or borderless, then double-click the exe

First launch Windows SmartScreen may say the app is unrecognized. Click **More info** → **Run anyway**. The build is unsigned.

If there is no release yet, open the repo **Actions** tab → **Build Windows exe** → **Run workflow**. When it finishes, the exe is under that run’s Artifacts.

## Run from source

Python 3.10+ on PATH, then:

```bat
python overlay.py
```

or double-click `run.bat`. Optional: `python -m pip install pillow`

Build a local exe: `build.bat`

## OBS

While the app is running it writes:

`%LOCALAPPDATA%\\MTGADeckOverlay\\obs\\playerdeck.html`  
`%LOCALAPPDATA%\\MTGADeckOverlay\\obs\\oppdeck.html`

Add those as Browser Sources at **480 × 1080**. Closing the app blanks the pages.

## License

This project is free software, licensed under the **GNU General Public License v2.0 only** — the same license as the Linux kernel.

See [LICENSE](LICENSE) for the full terms. In short: you may run, study, share, and modify this program. If you distribute it or a modified version, you must also provide the corresponding source under GPLv2.

Copyright (C) 2026 SkuggiXD

Unofficial fan content. Not affiliated with Wizards of the Coast.
