#!/usr/bin/env python3
"""
MTGA Deck Overlay — remaining cards in your current deck.

Reads Magic: The Gathering Arena's Player.log only. Does not inject into
the game process or read memory (the approach Wizards community managers
have historically tolerated for plugin-style tools).

Requirements:
  - Python 3.9+
  - Arena: Options → Account → Detailed Logs (Plugin Support) ON, then restart Arena
  - Run Arena windowed / borderless so the overlay can sit on top

Usage:
  python overlay.py
  python overlay.py --log "C:\\Users\\YOU\\AppData\\LocalLow\\Wizards Of The Coast\\MTGA\\Player.log"
  python overlay.py --demo
  python overlay.py --deck sample_deck.txt
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _persistent_dir() -> Path:
    """Stable folder that survives PyInstaller temp extracts and app restarts."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        d = root / "MTGADeckOverlay"
    else:
        d = Path.home() / ".mtga-deck-overlay"
    d.mkdir(parents=True, exist_ok=True)
    return d


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = _persistent_dir()
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)
IMAGE_DIR = CACHE_DIR / "images"
IMAGE_DIR.mkdir(exist_ok=True)
NAME_CACHE_PATH = CACHE_DIR / "arena_ids.json"
STATUS_PATH = CACHE_DIR / "name_source.txt"
SIDE_PATH = CACHE_DIR / "preview_side.txt"
FORMAT_PATH = CACHE_DIR / "meta_format.txt"
OBS_DIR = DATA_DIR / "obs"
OBS_DIR.mkdir(exist_ok=True)
MATCH_DIR = DATA_DIR / "matches"
MATCH_DIR.mkdir(exist_ok=True)
OBS_WIDTH, OBS_HEIGHT = 480, 1080
META_UA = {
    "User-Agent": "Mozilla/5.0 (compatible; mtga-deck-overlay/1.4; +https://www.mtggoldfish.com)",
    "Accept": "text/html,application/json,text/plain",
}
SCRYFALL_UA = {
    "User-Agent": "mtga-deck-overlay/1.2 (personal overlay; +https://scryfall.com/docs/api)",
    "Accept": "*/*",
}


def default_log_paths() -> List[Path]:
    home = Path.home()
    candidates = []
    # Windows
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE")
    if local:
        base = Path(local)
        # %LOCALAPPDATA%Low is a sibling of Local, not inside it
        if base.name.lower() == "local":
            low = base.parent / "LocalLow"
        else:
            low = base.parent / "LocalLow" if base.parent else Path()
        candidates.append(low / "Wizards Of The Coast" / "MTGA" / "Player.log")
        # Also try the classic AppData expansion
        user = os.environ.get("USERPROFILE")
        if user:
            candidates.append(
                Path(user) / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log"
            )
    # macOS
    candidates.append(home / "Library" / "Logs" / "Wizards Of The Coast" / "MTGA" / "Player.log")
    # Linux / Proton / Steam
    candidates.extend(
        [
            home / ".steam" / "steam" / "steamapps" / "compatdata" / "2141910" / "pfx" / "drive_c"
            / "users" / "steamuser" / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log",
            home / ".local" / "share" / "Steam" / "steamapps" / "compatdata" / "2141910" / "pfx" / "drive_c"
            / "users" / "steamuser" / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log",
        ]
    )
    # de-dupe
    seen = set()
    out = []
    for p in candidates:
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def find_player_log(override: Optional[str] = None) -> Optional[Path]:
    if override:
        p = Path(override).expanduser()
        return p if p.exists() else p  # still return so the UI can show the path
    for p in default_log_paths():
        if p.exists():
            return p
    return default_log_paths()[0] if default_log_paths() else None


BASIC_LANDS = {
    "Plains", "Island", "Swamp", "Mountain", "Forest",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest", "Wastes",
}


SUBTYPE_TO_BASIC = {
    "subtype_plains": "Plains",
    "subtype_island": "Island",
    "subtype_swamp": "Swamp",
    "subtype_mountain": "Mountain",
    "subtype_forest": "Forest",
}


def _fix_basic_name(name: str, type_blob: str) -> str:
    blob = f"{name} {type_blob}".lower()
    # Duals / shocks / verges keep their printed title
    land_hits = [n for n in ("plains", "island", "swamp", "mountain", "forest") if re.search(rf"\b{n}\b", blob)]
    if name and name not in BASIC_LANDS and not name.startswith("Snow-Covered") and len(land_hits) > 1:
        return name

    for needle, proper in (
        ("snow-covered plains", "Snow-Covered Plains"),
        ("snow-covered island", "Snow-Covered Island"),
        ("snow-covered swamp", "Snow-Covered Swamp"),
        ("snow-covered mountain", "Snow-Covered Mountain"),
        ("snow-covered forest", "Snow-Covered Forest"),
    ):
        if needle in blob:
            return proper

    # Prefer type line / subtype / mana ability over a wrong TitleId
    if re.search(r"subtype_mountain|\bbasic land\s*[—\-–]\s*mountain\b|add \{r\}", blob):
        if "add {u}" not in blob and "add {w}" not in blob and "add {b}" not in blob and "add {g}" not in blob:
            return "Snow-Covered Mountain" if "snow" in blob else "Mountain"
    if re.search(r"subtype_island|\bbasic land\s*[—\-–]\s*island\b|add \{u\}", blob):
        if "add {r}" not in blob and "add {w}" not in blob and "add {b}" not in blob and "add {g}" not in blob:
            return "Snow-Covered Island" if "snow" in blob else "Island"
    if re.search(r"subtype_swamp|\bbasic land\s*[—\-–]\s*swamp\b|add \{b\}", blob):
        if "add {r}" not in blob and "add {w}" not in blob and "add {u}" not in blob and "add {g}" not in blob:
            return "Snow-Covered Swamp" if "snow" in blob else "Swamp"
    if re.search(r"subtype_plains|\bbasic land\s*[—\-–]\s*plains\b|add \{w\}", blob):
        if "add {r}" not in blob and "add {u}" not in blob and "add {b}" not in blob and "add {g}" not in blob:
            return "Snow-Covered Plains" if "snow" in blob else "Plains"
    if re.search(r"subtype_forest|\bbasic land\s*[—\-–]\s*forest\b|add \{g\}", blob):
        if "add {r}" not in blob and "add {w}" not in blob and "add {b}" not in blob and "add {u}" not in blob:
            return "Snow-Covered Forest" if "snow" in blob else "Forest"

    for needle, proper in (
        ("plains", "Plains"),
        ("island", "Island"),
        ("swamp", "Swamp"),
        ("mountain", "Mountain"),
        ("forest", "Forest"),
        ("wastes", "Wastes"),
    ):
        if re.search(rf"\b{re.escape(needle)}\b", blob):
            if name not in BASIC_LANDS and " " in name and "Covered" not in name:
                return name
            return proper
    return name or ""


def _looks_like_placeholder(name: Optional[str], grp_id: Optional[int] = None) -> bool:
    if not name:
        return True
    if name.startswith("#") and name[1:].isdigit():
        return True
    if grp_id is not None and name == str(grp_id):
        return True
    return False


def find_card_databases() -> List[Path]:
    """Locate Arena's Raw_CardDatabase_*.mtga SQLite files."""
    roots: List[Path] = []
    env = os.environ.get("MTGA_RAW_DIR")
    if env:
        roots.append(Path(env))

    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    roots.extend(
        [
            Path(pf) / "Wizards of the Coast" / "MTGA" / "MTGA_Data" / "Downloads" / "Raw",
            Path(pf86) / "Wizards of the Coast" / "MTGA" / "MTGA_Data" / "Downloads" / "Raw",
            Path(pf86) / "Steam" / "steamapps" / "common" / "MTGA" / "MTGA_Data" / "Downloads" / "Raw",
            Path(pf) / "Steam" / "steamapps" / "common" / "MTGA" / "MTGA_Data" / "Downloads" / "Raw",
            Path(r"D:\SteamLibrary\steamapps\common\MTGA\MTGA_Data\Downloads\Raw"),
            Path.home() / "Library" / "Application Support" / "com.wizards.mtga" / "Downloads" / "Raw",
        ]
    )

    # Steam libraryfolders.vdf on Windows / Linux
    vdfs = [
        Path(pf86) / "Steam" / "steamapps" / "libraryfolders.vdf",
        Path(pf) / "Steam" / "steamapps" / "libraryfolders.vdf",
        Path.home() / ".steam" / "steam" / "steamapps" / "libraryfolders.vdf",
        Path.home() / ".local" / "share" / "Steam" / "steamapps" / "libraryfolders.vdf",
    ]
    for vdf in vdfs:
        if not vdf.exists():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            lib = Path(m.group(1).replace("\\\\", "\\"))
            roots.append(lib / "steamapps" / "common" / "MTGA" / "MTGA_Data" / "Downloads" / "Raw")

    found: List[Path] = []
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for p in root.glob("Raw_CardDatabase_*.mtga"):
            key = str(p)
            if key not in seen:
                seen.add(key)
                found.append(p)
    found.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return found


def load_names_from_arena_db(db_path: Path) -> Dict[str, str]:
    """Read GrpId → English title from Arena's SQLite card database."""
    import sqlite3

    out: Dict[str, str] = {}
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cur = conn.cursor()
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        loc: Dict[int, str] = {}

        def slurp(table: str, id_col: str, text_col: str) -> None:
            try:
                for lid, text in cur.execute(f"SELECT {id_col}, {text_col} FROM {table}"):
                    if lid is None or not text:
                        continue
                    loc[int(lid)] = str(text)
            except sqlite3.Error:
                pass

        if "Localizations_enUS" in tables:
            # Prefer short title strings over rules text when LocId is reused.
            try:
                cols = {r[1] for r in cur.execute("PRAGMA table_info(Localizations_enUS)")}
                if {"LocId", "Loc"} <= cols:
                    rows = list(cur.execute("SELECT LocId, Loc FROM Localizations_enUS"))
                    best: Dict[int, str] = {}
                    for lid, text in rows:
                        if lid is None or not text:
                            continue
                        t = str(text).strip()
                        prev = best.get(int(lid))
                        if prev is None or (len(t) < len(prev) and "{" not in t):
                            best[int(lid)] = t
                    loc.update(best)
            except Exception:
                slurp("Localizations_enUS", "LocId", "Loc")
        if not loc and "Localizations" in tables:
            cols = {r[1] for r in cur.execute("PRAGMA table_info(Localizations)")}
            if {"LocId", "Loc"} <= cols:
                slurp("Localizations", "LocId", "Loc")
            elif {"Id", "enUS"} <= cols:
                slurp("Localizations", "Id", "enUS")
            elif {"LocId", "enUS"} <= cols:
                slurp("Localizations", "LocId", "enUS")

        if "Cards" not in tables or not loc:
            return out

        card_cols = {r[1] for r in cur.execute("PRAGMA table_info(Cards)")}
        title_col = "TitleId" if "TitleId" in card_cols else None
        grp_col = "GrpId" if "GrpId" in card_cols else None
        if not title_col or not grp_col:
            return out

        extra = [
            c
            for c in (
                "Subtypes",
                "Subtype",
                "Types",
                "CardType",
                "Colors",
                "ColorId",
                "Color",
                "AbilityIds",
                "Abilities",
                "TypeLineId",
                "TypeLine",
                "SubtypeIds",
            )
            if c in card_cols
        ]
        cols_sql = f"{grp_col}, {title_col}" + ("" if not extra else ", " + ", ".join(extra))
        for row in cur.execute(f"SELECT {cols_sql} FROM Cards"):
            grp, title_id = row[0], row[1]
            extras = row[2:]
            if grp is None:
                continue
            parts = []
            if title_id is not None:
                t = loc.get(int(title_id))
                if t:
                    parts.append(str(t))
            extra_blob_bits = []
            for col, val in zip(extra, extras):
                if val is None:
                    continue
                extra_blob_bits.append(str(val))
                if col in ("TypeLineId",) or "Id" in col:
                    for piece in re.findall(r"\d+", str(val)):
                        hit = loc.get(int(piece))
                        if hit:
                            extra_blob_bits.append(str(hit))
            blob = " ".join(extra_blob_bits)
            raw_title = parts[0] if parts else ""
            raw_title = re.sub(r"<[^>]+>", "", raw_title).replace(" /// ", " // ").strip()
            name = _fix_basic_name(raw_title, blob)
            if name:
                out[str(int(grp))] = name
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Card name cache (Arena grpId → printed name)
# ---------------------------------------------------------------------------

class CardNames:
    def __init__(self) -> None:
        self.ids: Dict[str, str] = {}
        self.loc_ids: Dict[int, str] = {}  # TitleId / gameObjects.name → printed name
        self.source = "none"
        self.pending: set = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()

        if NAME_CACHE_PATH.exists():
            try:
                raw = json.loads(NAME_CACHE_PATH.read_text(encoding="utf-8"))
                self.ids = {
                    str(k): v
                    for k, v in raw.items()
                    if isinstance(v, str) and not _looks_like_placeholder(v, int(k) if str(k).lstrip("-").isdigit() else None)
                }
            except Exception:
                self.ids = {}

        loaded = self._load_arena_db()
        if loaded:
            self.source = f"Arena DB ({loaded} cards)"
        elif self.ids:
            self.source = f"cache ({len(self.ids)} cards)"

        self._bg = threading.Thread(target=self._resolver_loop, daemon=True)
        self._bg.start()

    def _load_arena_db(self) -> int:
        dbs = find_card_databases()
        for db in dbs:
            try:
                mapping = load_names_from_arena_db(db)
            except Exception:
                continue
            if not mapping:
                continue
            with self._lock:
                self.ids.update(mapping)
            self.save()
            return len(mapping)
        return 0

    def save(self) -> None:
        try:
            with self._lock:
                clean = {k: v for k, v in self.ids.items() if not _looks_like_placeholder(v)}
            NAME_CACHE_PATH.write_text(json.dumps(clean), encoding="utf-8")
        except Exception:
            pass

    def remember(self, grp_id: int, name: str) -> None:
        if _looks_like_placeholder(name, grp_id):
            return
        key = str(int(grp_id))
        with self._lock:
            existing = self.ids.get(key)
            # Never let a later Scryfall/log guess replace a real Arena DB title,
            # especially basic lands which Scryfall often maps to the wrong type.
            if existing and not _looks_like_placeholder(existing, grp_id) and existing != name:
                if existing in BASIC_LANDS or name in BASIC_LANDS:
                    return
            if existing == name:
                return
            self.ids[key] = name
        self.save()

    def remember_loc(self, loc_id: int, name: str) -> None:
        if loc_id is None or _looks_like_placeholder(name):
            return
        with self._lock:
            self.loc_ids[int(loc_id)] = name

    def get(self, grp_id: int) -> str:
        key = str(int(grp_id))
        with self._lock:
            name = self.ids.get(key)
            if name and not _looks_like_placeholder(name, grp_id):
                return name
            if grp_id >= 0:
                self.pending.add(int(grp_id))
                self._wake.set()
        return f"#{grp_id}"

    def resolve_loc(self, loc_id: Optional[int]) -> Optional[str]:
        if loc_id is None:
            return None
        with self._lock:
            return self.loc_ids.get(int(loc_id))

    def _resolver_loop(self) -> None:
        while True:
            self._wake.wait(timeout=2.0)
            self._wake.clear()
            with self._lock:
                batch = list(self.pending)
                self.pending.clear()
            for gid in batch:
                name = self._fetch_scryfall(gid)
                if name:
                    self.remember(gid, name)
                time.sleep(0.08)

    def _fetch_scryfall(self, grp_id: int) -> Optional[str]:
        key = str(int(grp_id))
        with self._lock:
            existing = self.ids.get(key)
            if existing and not _looks_like_placeholder(existing, grp_id):
                return None
        url = f"https://api.scryfall.com/cards/arena/{grp_id}"
        try:
            req = urllib.request.Request(
                url,
                headers=SCRYFALL_UA,
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            name = data.get("name")
            if not name:
                return None
            printed = str(name).split(" // ", 1)[0].strip()
            # Arena DB / GRE subtypes own basic lands. Scryfall arena IDs
            # frequently map a Mountain grpId to Island (and vice versa).
            if printed in BASIC_LANDS or printed.startswith("Snow-Covered"):
                return None
            return str(name)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Game / deck state
# ---------------------------------------------------------------------------

@dataclass
class OverlayState:
    status: str = "Waiting for Arena…"
    log_path: str = ""
    deck_name: str = ""
    in_match: bool = False
    turn: Optional[int] = None
    phase: str = ""
    # starting maindeck counts by grpId
    start_counts: Counter = field(default_factory=Counter)
    # remaining in library by grpId (None = not in a match / unknown)
    library_counts: Optional[Counter] = None
    sideboard: Counter = field(default_factory=Counter)
    local_seat: Optional[int] = None
    opponent_seat: Optional[int] = None
    opponent_name: str = ""
    seat_names: Dict[int, str] = field(default_factory=dict)
    recap_path: str = ""
    # grpId -> {zone_label: count} for currently public opponent cards
    opponent_public: Dict[str, Counter] = field(default_factory=dict)
    # grpIds ever seen from the opponent this game (bounced cards stay here)
    opponent_seen: Counter = field(default_factory=Counter)
    opponent_names: Dict[int, str] = field(default_factory=dict)
    opponent_hold: bool = False  # freeze list after game until next game
    # running GRE object map
    objects: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    zones: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            lib = self.library_counts
            start = self.start_counts
            if lib is not None:
                counts = Counter(lib)
                source = "library"
            elif start:
                counts = Counter(start)
                source = "deck"
            else:
                counts = Counter()
                source = "empty"
            return {
                "status": self.status,
                "log_path": self.log_path,
                "deck_name": self.deck_name,
                "in_match": self.in_match,
                "turn": self.turn,
                "phase": self.phase,
                "counts": counts,
                "start": Counter(start),
                "source": source,
                "sideboard": Counter(self.sideboard),
                "opponent_name": self.opponent_name,
                "opponent_seat": self.opponent_seat,
                "opponent_public": {k: Counter(v) for k, v in self.opponent_public.items()},
                "opponent_seen": Counter(self.opponent_seen),
                "opponent_names": dict(self.opponent_names),
                "opponent_hold": self.opponent_hold,
            }


# ---------------------------------------------------------------------------
# JSON extraction from Arena's messy log
# ---------------------------------------------------------------------------

JSON_HINTS = (
    "greToClientEvent",
    "greToClientMessages",
    "gameStateMessage",
    "matchGameRoomStateChangedEvent",
    "deckMessage",
    "CourseDeck",
    "EventSetDeck",
    "Deck.GetDeckLists",
    "ClientToGREMessage",
    "GREMessageType_ConnectResp",
    "GREMessageType_GameStateMessage",
    "authenticateResponse",
    "ZoneType_Library",
)


def extract_json_objects(text: str) -> Iterable[Any]:
    """Pull every top-level JSON object out of a blob of log text."""
    i = 0
    n = len(text)
    while i < n:
        start = text.find("{", i)
        if start < 0:
            return
        depth = 0
        in_str = False
        esc = False
        j = start
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        blob = text[start : j + 1]
                        try:
                            yield json.loads(blob)
                        except json.JSONDecodeError:
                            # sometimes the payload is a string-encoded JSON
                            try:
                                yield json.loads(json.loads(f'"{blob}"'))
                            except Exception:
                                pass
                        i = j + 1
                        break
            j += 1
        else:
            return


def walk(obj: Any) -> Iterable[Any]:
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def as_counter_from_pairs(items: Any) -> Counter:
    """Handle both [{cardId, quantity}] and flat [id, qty, id, qty] and [id, id, …]."""
    c: Counter = Counter()
    if not items:
        return c
    if isinstance(items, dict):
        for k, v in items.items():
            try:
                c[int(k)] += int(v)
            except Exception:
                pass
        return c
    if not isinstance(items, list) or not items:
        return c
    if isinstance(items[0], dict):
        for it in items:
            cid = it.get("cardId") or it.get("id") or it.get("grpId")
            qty = it.get("quantity") or it.get("count") or 1
            if cid is not None:
                c[int(cid)] += int(qty)
        return c
    # list of ints — either grpIds (one per card) or id/qty pairs
    ints = []
    for x in items:
        try:
            ints.append(int(x))
        except Exception:
            return c
    # Heuristic: if most values look like Arena grpIds (> 1000) it's a flat list of cards
    if ints and sum(1 for x in ints if x > 1000) > len(ints) * 0.6:
        c.update(ints)
        return c
    # otherwise treat as [id, qty, id, qty]
    for i in range(0, len(ints) - 1, 2):
        c[ints[i]] += ints[i + 1]
    return c


# ---------------------------------------------------------------------------
# Event application
# ---------------------------------------------------------------------------

class LogParser:
    def __init__(self, state: OverlayState, names: CardNames) -> None:
        self.state = state
        self.names = names
        self.my_user_id: Optional[str] = None
        self.pending_name_lookups: set = set()
        from match_recap import MatchRecap

        self.recap = MatchRecap(state, names, MATCH_DIR)

    def ingest_text(self, text: str) -> None:
        if not any(h in text for h in JSON_HINTS) and "gameStateMessage" not in text:
            return
        for obj in extract_json_objects(text):
            self.ingest_obj(obj)

    def ingest_obj(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        self._harvest_names(obj)
        # Auth / identity
        if "authenticateResponse" in obj or obj.get("clientId") or obj.get("screenName"):
            payload = obj.get("authenticateResponse") or obj
            uid = payload.get("clientId") or payload.get("playerId") or payload.get("accountId")
            if uid:
                self.my_user_id = str(uid)

        # Match room — find our seat
        if "matchGameRoomStateChangedEvent" in obj or "reservedPlayers" in obj:
            self._parse_match_room(obj)

        # Saved / selected deck lists
        self._parse_deck_payloads(obj)

        # GRE stream
        gre = obj.get("greToClientEvent") or {}
        msgs = gre.get("greToClientMessages") or []
        if not msgs and obj.get("type", "").startswith("GREMessageType_"):
            msgs = [obj]
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            self._parse_gre_message(msg)

        # Also scan nested copies (logs sometimes wrap twice)
        if "gameStateMessage" in obj and "greToClientEvent" not in obj:
            self._parse_game_state(obj.get("gameStateMessage") or {})

    def _harvest_names(self, obj: Any) -> None:
        """Pull grpId → title mappings out of whatever Arena logged."""
        for node in walk(obj):
            if not isinstance(node, dict):
                continue
            grp = node.get("grpId") or node.get("GrpId") or node.get("cardId") or node.get("CardId")
            title = (
                node.get("title")
                or node.get("Title")
                or node.get("cardName")
                or node.get("CardName")
                or node.get("englishName")
            )
            # gameObjects.name is a localization id, not a string
            raw_name = node.get("name")
            if isinstance(raw_name, str) and not raw_name.isdigit() and raw_name.lower() not in (
                "name",
                "card",
            ):
                title = title or raw_name
            if grp is not None and isinstance(title, str) and not _looks_like_placeholder(title):
                try:
                    self.names.remember(int(grp), title)
                except Exception:
                    pass
            loc = node.get("titleId") or node.get("TitleId")
            if loc is not None and isinstance(title, str):
                try:
                    self.names.remember_loc(int(loc), title)
                except Exception:
                    pass
            # CardMetadataInfo style lists
            meta = node.get("CardMetadataInfo") or node.get("cardMetadataInfo")
            if isinstance(meta, list):
                for item in meta:
                    if isinstance(item, dict):
                        self._harvest_names(item)

    def _parse_match_room(self, obj: Any) -> None:
        room = obj.get("matchGameRoomStateChangedEvent") or obj
        game_room = room.get("gameRoomInfo") or room
        state_type = (
            (game_room.get("stateType") or "")
            if isinstance(game_room, dict)
            else ""
        )
        players = []
        match_id = ""
        for node in walk(obj):
            if not isinstance(node, dict):
                continue
            if "reservedPlayers" in node and not players:
                players = node.get("reservedPlayers") or []
            mid = node.get("matchId") or node.get("MatchId") or node.get("matchID")
            if mid and not match_id:
                match_id = str(mid)
        with self.state.lock:
            for p in players:
                if not isinstance(p, dict):
                    continue
                uid = str(p.get("userId") or p.get("playerId") or "")
                name = p.get("playerName") or p.get("screenName") or ""
                seat = p.get("systemSeatId") or p.get("teamId")
                if self.my_user_id and uid and uid == self.my_user_id and seat:
                    self.state.local_seat = int(seat)
                if seat and name:
                    self.state.seat_names[int(seat)] = str(name)
                    self.state._seat_names = self.state.seat_names
                # fallback: first human seat if we don't know ourselves yet
                if self.state.local_seat is None and seat and not str(p.get("aiId") or ""):
                    pass
            names_by_seat = self.state.seat_names or getattr(self.state, "_seat_names", {})
            if self.state.local_seat is not None:
                for s, n in names_by_seat.items():
                    if int(s) != int(self.state.local_seat):
                        self.state.opponent_seat = int(s)
                        self.state.opponent_name = n
                        break
            try:
                self.recap.set_players(dict(self.state.seat_names), match_id)
            except Exception:
                pass
            if "MatchCompleted" in str(state_type) or "MatchGameRoomStateType_MatchCompleted" in str(state_type):
                self.state.in_match = False
                self.state.library_counts = None
                self.state.status = "Match ended — opponent list held"
                self.state.objects.clear()
                self.state.zones.clear()
                self.state.opponent_hold = True
                try:
                    self.recap.close_match("Match completed")
                except Exception:
                    pass

    def _parse_deck_payloads(self, obj: Any) -> None:
        # CourseDeck.MainDeck
        for node in walk(obj):
            if not isinstance(node, dict):
                continue
            if "CourseDeck" in node and isinstance(node["CourseDeck"], dict):
                cd = node["CourseDeck"]
                main = as_counter_from_pairs(cd.get("MainDeck") or cd.get("mainDeck"))
                side = as_counter_from_pairs(cd.get("Sideboard") or cd.get("sideboard"))
                name = (
                    (node.get("CourseDeckSummary") or {}).get("Name")
                    or cd.get("Name")
                    or node.get("Name")
                    or ""
                )
                if main:
                    with self.state.lock:
                        self.state.start_counts = main
                        self.state.sideboard = side
                        if name:
                            self.state.deck_name = name
                        if not self.state.in_match:
                            self.state.status = f"Loaded deck: {self.state.deck_name or 'unnamed'}"
            if "deckMessage" in node and isinstance(node["deckMessage"], dict):
                dm = node["deckMessage"]
                main = as_counter_from_pairs(dm.get("deckCards") or dm.get("DeckCards"))
                side = as_counter_from_pairs(dm.get("sideboardCards") or dm.get("SideboardCards"))
                if main:
                    with self.state.lock:
                        self.state.start_counts = main
                        self.state.sideboard = side
                        self.state.library_counts = Counter(main)
                        self.state.status = "Match deck locked in"
            # EventSetDeck-style { mainDeck: [id, qty, ...] } — only before a match
            if (
                not self.state.in_match
                and "mainDeck" in node
                and isinstance(node.get("mainDeck"), list)
                and node.get("mainDeck")
            ):
                main = as_counter_from_pairs(node.get("mainDeck"))
                side = as_counter_from_pairs(node.get("sideboard"))
                if main and max(main.values(), default=0) <= 30:
                    with self.state.lock:
                        self.state.start_counts = main
                        if side:
                            self.state.sideboard = side
                        name = node.get("name") or node.get("Name")
                        if name:
                            self.state.deck_name = str(name)

    def _parse_gre_message(self, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type") or ""
        seats = msg.get("systemSeatIds") or msg.get("systemSeatId") or []
        if isinstance(seats, int):
            seats = [seats]
        if self.state.local_seat is None and seats:
            # Connect / early messages are usually addressed to us
            if "ConnectResp" in mtype or "GameStateMessage" in mtype:
                with self.state.lock:
                    if self.state.local_seat is None:
                        self.state.local_seat = int(seats[0])

        if "ConnectResp" in mtype:
            deck_msg = (msg.get("connectResp") or {}).get("deckMessage") or msg.get("deckMessage")
            if isinstance(deck_msg, dict):
                main = as_counter_from_pairs(deck_msg.get("deckCards"))
                side = as_counter_from_pairs(deck_msg.get("sideboardCards"))
                if main:
                    with self.state.lock:
                        self.state.start_counts = main
                        self.state.sideboard = side
                        self.state.in_match = True
                        self.state.library_counts = Counter(main)
                        self.state.status = "Connected to match"
                        self._reset_opponent_for_new_game()

        gsm = msg.get("gameStateMessage")
        if gsm:
            self._parse_game_state(gsm)

    def _parse_game_state(self, gsm: Dict[str, Any]) -> None:
        if not isinstance(gsm, dict):
            return
        with self.state.lock:
            # Full snapshots replace the board; keep using diffs on top.
            if gsm.get("type") == "GameStateType_Full" or gsm.get("gameStateType") == "GameStateType_Full":
                self.state.objects.clear()
                self.state.zones.clear()
                if self.state.start_counts:
                    self.state.library_counts = Counter(self.state.start_counts)

            info = gsm.get("gameInfo") or {}
            stage = info.get("stage") or ""
            match_state = info.get("matchState") or ""
            if stage == "GameStage_Start":
                self._reset_opponent_for_new_game()
            if "GameInProgress" in str(match_state) or stage in ("GameStage_Play", "GameStage_Start"):
                self.state.in_match = True
            if "GameComplete" in str(match_state) or stage == "GameStage_GameOver":
                self.state.in_match = False
                self.state.library_counts = None
                self.state.status = "Game over — opponent list held"
                self.state.opponent_hold = True

            players = info.get("players") or gsm.get("players") or []
            if isinstance(players, list):
                for p in players:
                    if not isinstance(p, dict):
                        continue
                    seat = p.get("systemSeatNumber") or p.get("systemSeatId") or p.get("seatId")
                    if seat is None:
                        continue
                    seat = int(seat)
                    if self.state.local_seat is not None and seat != int(self.state.local_seat):
                        self.state.opponent_seat = seat
                        nm = p.get("playerName") or p.get("screenName") or p.get("name")
                        if nm:
                            self.state.opponent_name = str(nm)

            turn = gsm.get("turnInfo") or {}
            if turn.get("turnNumber") is not None:
                new_turn = int(turn["turnNumber"])
                if self.state.opponent_hold and new_turn == 1:
                    self._reset_opponent_for_new_game()
                self.state.turn = new_turn
            if turn.get("phase"):
                self.state.phase = str(turn["phase"]).replace("Phase_", "")

            for inst in gsm.get("diffDeletedInstanceIds") or []:
                self.state.objects.pop(int(inst), None)

            for obj in gsm.get("gameObjects") or []:
                if not isinstance(obj, dict):
                    continue
                iid = obj.get("instanceId")
                if iid is None:
                    continue
                prev = self.state.objects.get(int(iid), {})
                prev.update(obj)
                self.state.objects[int(iid)] = prev
                grp = obj.get("grpId")
                loc_name = obj.get("name")
                if grp is not None and loc_name is not None:
                    pretty = self.names.resolve_loc(loc_name) if not isinstance(loc_name, str) else None
                    if pretty:
                        self.names.remember(int(grp), pretty)
                if grp is not None:
                    subs = obj.get("subtypes") or obj.get("subtype") or []
                    if isinstance(subs, str):
                        subs = [subs]
                    for sub in subs:
                        key = str(sub).lower().replace(" ", "")
                        basic = SUBTYPE_TO_BASIC.get(key)
                        if basic:
                            # GRE subtype is authoritative for basics (Scryfall IDs are not)
                            with self.names._lock:
                                self.names.ids[str(int(grp))] = basic
                            break

            for zone in gsm.get("zones") or []:
                if not isinstance(zone, dict):
                    continue
                zid = zone.get("zoneId")
                if zid is None:
                    continue
                prev = self.state.zones.get(int(zid), {})
                prev.update(zone)
                self.state.zones[int(zid)] = prev

            self._recompute_library_locked()
            self._recompute_opponent_locked()
            try:
                self.recap.consume(gsm)
            except Exception:
                pass

    def _recompute_library_locked(self) -> None:
        """Caller must hold state.lock.

        Live tracking: starting maindeck minus cards we can see that have
        left the library (hand / field / yard / exile / stack). Hidden library
        cards are almost never resent in GRE diffs, so counting the library
        zone directly only works on the opening snapshot.
        """
        seat = self.state.local_seat
        lib_zones = []
        other_zone_ids = set()
        for z in self.state.zones.values():
            ztype = str(z.get("type") or "")
            zid = z.get("zoneId")
            owner = z.get("ownerSeatId")
            if ztype == "ZoneType_Library":
                lib_zones.append(z)
            elif zid is not None and ztype not in (
                "ZoneType_Limbo",
                "ZoneType_Pending",
                "ZoneType_Revealed",
            ):
                if seat is None or owner is None or int(owner) == int(seat):
                    other_zone_ids.add(int(zid))

        # Prefer our visible library if a full snapshot still has grpIds.
        lib_zone = None
        best_vis = -1
        for z in lib_zones:
            owner = z.get("ownerSeatId")
            if seat is not None and owner is not None and int(owner) != int(seat):
                continue
            ids = z.get("objectInstanceIds") or []
            vis = sum(1 for iid in ids if self.state.objects.get(int(iid), {}).get("grpId"))
            if vis > best_vis:
                best_vis = vis
                lib_zone = z
                if vis > 0 and owner is not None:
                    self.state.local_seat = int(owner)
                    seat = self.state.local_seat

        remaining = None
        if lib_zone and best_vis >= 8:
            counts: Counter = Counter()
            for iid in lib_zone.get("objectInstanceIds") or []:
                grp = self.state.objects.get(int(iid), {}).get("grpId")
                if grp:
                    counts[int(grp)] += 1
            if counts:
                remaining = counts

        if remaining is None and self.state.start_counts:
            seen_out: Counter = Counter()
            lib_ids = set()
            if lib_zone and lib_zone.get("zoneId") is not None:
                lib_ids.add(int(lib_zone["zoneId"]))
            for z in lib_zones:
                if z.get("zoneId") is not None:
                    lib_ids.add(int(z["zoneId"]))

            for go in self.state.objects.values():
                grp = go.get("grpId")
                if not grp:
                    continue
                types = go.get("cardTypes") or go.get("cardType") or []
                if isinstance(types, str):
                    types = [types]
                if any("Token" in str(t) for t in types):
                    continue
                owner = go.get("ownerSeatId")
                if seat is not None and owner is not None and int(owner) != int(seat):
                    continue
                zid = go.get("zoneId")
                if zid is not None and int(zid) in lib_ids:
                    continue
                # Only subtract cards that were actually in the submitted deck
                gid = int(grp)
                if gid not in self.state.start_counts:
                    continue
                seen_out[gid] += 1

            remaining = Counter(self.state.start_counts)
            remaining.subtract(seen_out)

        if remaining is None:
            return

        cleaned = Counter({k: v for k, v in remaining.items() if v > 0})
        self.state.library_counts = cleaned
        left = sum(cleaned.values())
        # Library leftovers after a deck load are not a match. Only GRE
        # ConnectResp / GameInProgress may flip in_match on.
        if self.state.in_match:
            started = sum(self.state.start_counts.values()) or left
            self.state.status = f"In match — {left}/{started} left in library"

    def _recompute_opponent_locked(self) -> None:
        """Public opponent cards: battlefield / yard / exile / stack / revealed."""
        us = self.state.local_seat
        opp = self.state.opponent_seat
        zone_by_id = {}
        for z in self.state.zones.values():
            zid = z.get("zoneId")
            if zid is not None:
                zone_by_id[int(zid)] = str(z.get("type") or "")

        PUBLIC = {
            "ZoneType_Battlefield": "Battlefield",
            "ZoneType_Graveyard": "Graveyard",
            "ZoneType_Exile": "Exile",
            "ZoneType_Stack": "Stack",
            "ZoneType_Revealed": "Revealed",
            "ZoneType_Command": "Command",
        }
        HIDDEN = {"ZoneType_Library", "ZoneType_Hand", "ZoneType_Limbo"}

        # Infer opponent seat from objects if match-room parse missed it
        if opp is None and us is not None:
            seats = set()
            for go in self.state.objects.values():
                for key in ("ownerSeatId", "controllerSeatId"):
                    if go.get(key) is not None:
                        seats.add(int(go[key]))
            for s in seats:
                if int(s) != int(us):
                    opp = int(s)
                    self.state.opponent_seat = opp
                    break

        public: Dict[str, Counter] = {}
        seen_now: Counter = Counter()

        def is_theirs(go: Dict[str, Any]) -> bool:
            owner = go.get("ownerSeatId")
            controller = go.get("controllerSeatId")
            if opp is not None:
                if owner is not None and int(owner) == int(opp):
                    return True
                if controller is not None and int(controller) == int(opp):
                    return True
                return False
            if us is not None:
                if owner is not None and int(owner) != int(us):
                    return True
                if controller is not None and int(controller) != int(us):
                    return True
            return False

        def name_from_go(go: Dict[str, Any], gid: int) -> Optional[str]:
            cached = self.state.opponent_names.get(gid)
            if cached and not _looks_like_placeholder(cached, gid):
                return cached
            pretty = self.names.get(gid)
            if pretty and not _looks_like_placeholder(pretty, gid):
                return pretty
            loc_name = go.get("name")
            if loc_name is not None and not isinstance(loc_name, str):
                hit = self.names.resolve_loc(loc_name)
                if hit and not _looks_like_placeholder(hit, gid):
                    self.names.remember(gid, hit)
                    return hit
            for key in ("title", "cardName", "englishName"):
                val = go.get(key)
                if isinstance(val, str) and not _looks_like_placeholder(val, gid):
                    self.names.remember(gid, val)
                    return val
            subs = go.get("subtypes") or go.get("subtype") or []
            if isinstance(subs, str):
                subs = [subs]
            for sub in subs:
                basic = SUBTYPE_TO_BASIC.get(str(sub).lower().replace(" ", ""))
                if basic:
                    self.names.remember(gid, basic)
                    return basic
            return None

        for go in self.state.objects.values():
            grp = go.get("grpId")
            if not grp:
                continue
            types = go.get("cardTypes") or go.get("cardType") or []
            if isinstance(types, str):
                types = [types]
            if any("Token" in str(t) for t in types):
                continue
            if not is_theirs(go):
                continue
            gid = int(grp)
            pretty = name_from_go(go, gid)
            if pretty:
                self.state.opponent_names[gid] = pretty
            zid = go.get("zoneId")
            ztype = zone_by_id.get(int(zid), "") if zid is not None else ""
            if ztype in HIDDEN:
                continue
            label = PUBLIC.get(ztype)
            if not label:
                continue
            public.setdefault(label, Counter())[gid] += 1
            seen_now[gid] += 1

        # After the game, GRE snapshots go empty. Keep the last known list.
        if self.state.opponent_hold and not seen_now and not any(public.values()):
            return

        self.state.opponent_public = public
        for gid, n in seen_now.items():
            if n > self.state.opponent_seen[gid]:
                self.state.opponent_seen[gid] = n
            pretty = self.state.opponent_names.get(gid) or self.names.get(gid)
            if pretty and not _looks_like_placeholder(pretty, gid):
                self.state.opponent_names[gid] = pretty

    def _reset_opponent_for_new_game(self) -> None:
        """Caller should hold state.lock when invoked from a parse path."""
        self.state.opponent_public.clear()
        self.state.opponent_seen.clear()
        self.state.opponent_names.clear()
        self.state.opponent_hold = False


# ---------------------------------------------------------------------------
# Log tailer
# ---------------------------------------------------------------------------

class LogTailer(threading.Thread):
    def __init__(self, path: Path, parser: LogParser, state: OverlayState) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.parser = parser
        self.state = state
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        with self.state.lock:
            self.state.log_path = str(self.path)
            self.state.status = f"Watching {self.path.name}…"

        # First pass: read the last ~8 MB so we pick up the current deck
        # without chewing a 200 MB log every launch.
        self._scan_existing()
        self._follow()

    def _scan_existing(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            with self.state.lock:
                self.state.status = f"Log not found yet:\n{self.path}"
            return
        start = max(0, size - 8 * 1024 * 1024)
        try:
            with self.path.open("r", encoding="utf-8", errors="ignore") as f:
                if start:
                    f.seek(start)
                    f.readline()  # drop partial line
                buf = []
                for line in f:
                    if line.startswith("[UnityCrossThreadLogger]") and buf:
                        self.parser.ingest_text("".join(buf))
                        buf = [line]
                    else:
                        buf.append(line)
                if buf:
                    self.parser.ingest_text("".join(buf))
        except Exception as exc:
            with self.state.lock:
                self.state.status = f"Read error: {exc}"

    def _follow(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.path.exists():
                    time.sleep(1.0)
                    continue
                with self.path.open("r", encoding="utf-8", errors="ignore") as f:
                    f.seek(0, os.SEEK_END)
                    last_size = f.tell()
                    buf: List[str] = []
                    last_flush = time.time()
                    while not self._stop.is_set():
                        line = f.readline()
                        if not line:
                            if buf and time.time() - last_flush > 0.25:
                                self.parser.ingest_text("".join(buf))
                                buf = []
                                last_flush = time.time()
                            try:
                                size = self.path.stat().st_size
                            except FileNotFoundError:
                                break
                            if size < last_size:
                                # log rotated / truncated
                                f.seek(0)
                                last_size = 0
                            else:
                                time.sleep(0.08)
                            continue
                        last_size = f.tell()
                        if line.startswith("[UnityCrossThreadLogger]") and buf:
                            self.parser.ingest_text("".join(buf))
                            buf = [line]
                            last_flush = time.time()
                        else:
                            buf.append(line)
                            if len(buf) > 200:
                                self.parser.ingest_text("".join(buf))
                                buf = []
                                last_flush = time.time()
            except Exception as exc:
                with self.state.lock:
                    self.state.status = f"Tail error: {exc}"
                time.sleep(1.0)


# ---------------------------------------------------------------------------
# Metagame prediction (MTGGoldfish lists, cached 24h)
# ---------------------------------------------------------------------------
