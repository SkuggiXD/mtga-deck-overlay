from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import (
    BASIC_LANDS,
    CACHE_DIR,
    FORMAT_PATH,
    IMAGE_DIR,
    META_UA,
    OBS_DIR,
    OBS_HEIGHT,
    OBS_WIDTH,
    SCRYFALL_UA,
    SIDE_PATH,
    CardNames,
    OverlayState,
    _looks_like_placeholder,
)


META_FORMATS = (
    ("standard", "STD"),
    ("historic", "HIS"),
    ("timeless", "TIME"),
    ("alchemy", "ALC"),
    ("pioneer", "PIO"),
)

_CARD_LINE = re.compile(r"^(\d+)\s+[xX]?\s*(.+)$")


def _clean_card_name(name: str) -> str:
    name = re.sub(r"\s+\([^)]+\)\s*\S*$", "", name)
    name = re.sub(r"\s+\([^)]+\)$", "", name)
    if " // " in name:
        name = name.split(" // ", 1)[0]
    return name.strip()


def parse_simple_decklist(text: str) -> Counter:
    counts: Counter = Counter()
    section = "main"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            # goldfish download: blank line starts sideboard
            if counts:
                section = "side"
            continue
        low = line.lower().rstrip(":")
        if low in ("deck", "main", "maindeck", "main deck"):
            section = "main"
            continue
        if low in ("sideboard", "side", "sb"):
            section = "side"
            continue
        if section != "main":
            continue
        m = _CARD_LINE.match(line)
        if not m:
            continue
        qty, name = int(m.group(1)), _clean_card_name(m.group(2))
        if name:
            counts[name] += qty
    return counts


def _norm_card(name: str) -> str:
    n = (name or "").lower().strip()
    n = re.sub(r"\s*//\s*.*$", "", n)
    n = re.sub(r"\s*\([^)]+\)\s*$", "", n)
    n = re.sub(r"[^a-z0-9' ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


class MetaEngine:
    MIN_CARDS = 3
    MAX_DECKS = 22

    def __init__(self) -> None:
        self.format = "standard"
        if FORMAT_PATH.exists():
            try:
                val = FORMAT_PATH.read_text(encoding="utf-8").strip().lower()
                if any(val == f for f, _ in META_FORMATS):
                    self.format = val
            except Exception:
                pass
        self.decks: List[Dict[str, Any]] = []
        self.status = "Meta idle"
        self.prediction: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self._last_key: Optional[Tuple[str, Tuple[str, ...]]] = None
        self._loading = False

    def set_format(self, fmt: str) -> None:
        if fmt == self.format:
            return
        self.format = fmt
        try:
            FORMAT_PATH.write_text(fmt, encoding="utf-8")
        except Exception:
            pass
        with self._lock:
            self.decks = []
            self.prediction = None
            self._last_key = None
        self.status = f"Format → {fmt}"

    def consider(self, seen_names: List[str], gy_names: Optional[List[str]] = None) -> None:
        unique = []
        seen = set()
        for n in seen_names:
            if not n or n.startswith("#") or n in BASIC_LANDS:
                continue
            if n in seen:
                continue
            seen.add(n)
            unique.append(n)
        gy = []
        for n in gy_names or []:
            if n and not n.startswith("#") and n not in BASIC_LANDS and n not in gy:
                gy.append(n)
        if len(unique) < self.MIN_CARDS:
            with self._lock:
                self.prediction = None
                self.status = f"Need {self.MIN_CARDS} unique non-lands ({len(unique)} so far)"
                self._last_key = None
            return
        key = (self.format, tuple(sorted(unique)), tuple(sorted(gy)))
        with self._lock:
            if key == self._last_key or self._loading:
                return
            self._loading = True
        threading.Thread(target=self._run, args=(key, unique, gy), daemon=True).start()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "format": self.format,
                "status": self.status,
                "prediction": None if self.prediction is None else dict(self.prediction),
                "loaded": len(self.decks),
            }

    def _run(self, key: Tuple[Any, ...], unique: List[str], gy: List[str]) -> None:
        try:
            self._ensure_decks()
            ranked = self._rank(unique, gy)
            with self._lock:
                self.prediction = {
                    "seen": unique,
                    "matches": ranked[:3],
                }
                self._last_key = key
                if ranked:
                    top = ranked[0]
                    self.status = f"{top['name']}  {top['hits']}/{top['need']}"
                else:
                    self.status = f"No overlap in {len(self.decks)} lists yet"
        except Exception as exc:
            with self._lock:
                self.status = f"Meta error: {exc}"
        finally:
            with self._lock:
                self._loading = False

    def _cache_path(self) -> Path:
        return CACHE_DIR / f"meta_{self.format}_v2.json"

    def _ensure_decks(self) -> None:
        with self._lock:
            if self.decks:
                return
        path = self._cache_path()
        if path.exists() and time.time() - path.stat().st_mtime < 24 * 3600:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                decks = data.get("decks") or []
                if decks:
                    with self._lock:
                        self.decks = decks
                        self.status = f"Meta cache ({len(decks)} {self.format} lists)"
                    return
            except Exception:
                pass
        with self._lock:
            self.status = f"Downloading {self.format} metagame…"
        decks = self._download_meta()
        if decks:
            try:
                path.write_text(json.dumps({"decks": decks, "ts": int(time.time())}), encoding="utf-8")
            except Exception:
                pass
        with self._lock:
            self.decks = decks
            self.status = f"Loaded {len(decks)} {self.format} lists"

    def _download_meta(self) -> List[Dict[str, Any]]:
        url = f"https://www.mtggoldfish.com/metagame/{self.format}"
        req = urllib.request.Request(url, headers=META_UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", "ignore")
        # archetype label + following deck id
        pairs: List[Tuple[str, str]] = []
        seen_ids = set()
        for m in re.finditer(
            r">([A-Za-z0-9][^<]{2,48})</a>[\s\S]{0,400}?/deck/(\d+)",
            html,
        ):
            name, did = m.group(1).strip(), m.group(2)
            if name.lower() in ("online", "paper", "arena", "decks", "standard", "historic"):
                continue
            if did in seen_ids:
                continue
            seen_ids.add(did)
            pairs.append((name, did))
        if not pairs:
            for did in dict.fromkeys(re.findall(r"/deck/(\d+)", html)):
                pairs.append((f"Deck {did}", did))
        decks: List[Dict[str, Any]] = []
        for name, did in pairs[: self.MAX_DECKS]:
            try:
                dreq = urllib.request.Request(
                    f"https://www.mtggoldfish.com/deck/download/{did}",
                    headers=META_UA,
                )
                with urllib.request.urlopen(dreq, timeout=15) as resp:
                    text = resp.read().decode("utf-8", "ignore")
                cards = parse_simple_decklist(text)
                if sum(cards.values()) < 20:
                    continue
                decks.append(
                    {
                        "name": name,
                        "id": did,
                        "url": f"https://www.mtggoldfish.com/deck/{did}",
                        "cards": dict(cards),
                    }
                )
                time.sleep(0.15)
            except Exception:
                continue
        return decks

    def _rank(self, unique: List[str], gy: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        with self._lock:
            decks = list(self.decks)
        want_map = {_norm_card(n): n for n in unique if _norm_card(n)}
        want = set(want_map)
        gy_set = {_norm_card(n) for n in (gy or []) if _norm_card(n)}
        ranked = []
        for d in decks:
            cards = d.get("cards") or {}
            main = {_norm_card(n) for n in cards if _norm_card(n)}
            hit_keys = want & main
            hits = sorted(want_map[k] for k in hit_keys)
            miss = sorted(want_map[k] for k in (want - main))
            need = len(want)
            score = (len(hit_keys) / need) if need else 0.0
            gy_hits = len(gy_set & main)
            if gy_set:
                score += 0.18 * (gy_hits / max(1, len(gy_set)))
            if not hit_keys:
                continue
            extras = []
            for nm, qty in sorted(cards.items(), key=lambda kv: (-kv[1], kv[0])):
                if _norm_card(nm) in want or nm in BASIC_LANDS:
                    continue
                extras.append(nm)
                if len(extras) >= 6:
                    break
            ranked.append(
                {
                    "name": d.get("name") or "Unknown",
                    "url": d.get("url") or "",
                    "hits": len(hits),
                    "need": need,
                    "score": score,
                    "miss": miss,
                    "likely": extras,
                }
            )
        ranked.sort(key=lambda r: (-r["score"], -r["hits"], r["name"]))
        return ranked
