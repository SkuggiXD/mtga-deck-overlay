#!/usr/bin/env python3
"""MTGA Deck Overlay — remaining cards in your current deck."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from core import CardNames, OverlayState, LogParser, LogTailer, find_player_log
from features import parse_deck_text, DEMO_DECK
from ui import launch_ui


def main() -> None:
    ap = argparse.ArgumentParser(description="MTGA remaining-cards overlay")
    ap.add_argument("--log", help="Path to Player.log")
    ap.add_argument("--db", help="Path to Raw_CardDatabase_*.mtga (or the Raw folder)")
    ap.add_argument("--demo", action="store_true", help="Show a sample deck without reading logs")
    ap.add_argument("--deck", help="Load a .txt decklist on startup")
    args = ap.parse_args()
    if args.db:
        p = Path(args.db)
        if p.is_file():
            os.environ["MTGA_RAW_DIR"] = str(p.parent)
        else:
            os.environ["MTGA_RAW_DIR"] = str(p)

    names = CardNames()
    state = OverlayState()
    state.status = f"Waiting for Arena…  names from {names.source}"
    parser = LogParser(state, names)

    if args.demo:
        counts, _ = parse_deck_text(DEMO_DECK, names)
        state.start_counts = counts
        state.deck_name = "Izzet Demo"
        state.status = "Demo mode — not reading Arena"
    elif args.deck:
        text = Path(args.deck).read_text(encoding="utf-8", errors="ignore")
        counts, _ = parse_deck_text(text, names)
        state.start_counts = counts
        state.deck_name = Path(args.deck).stem
        state.status = f"Loaded {state.deck_name}"

    log_path = find_player_log(args.log)
    if log_path and not args.demo:
        tailer = LogTailer(log_path, parser, state)
        tailer.start()
        state.log_path = str(log_path)
    elif not args.demo:
        state.status = "Player.log not found. Use --log PATH or Load .txt deck."

    launch_ui(state, names)


if __name__ == "__main__":
    main()
