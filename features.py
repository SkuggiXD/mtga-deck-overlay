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


# ---------------------------------------------------------------------------
# Card images (Scryfall, cached on disk)
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:80] or "card"


class CardImages:
    """Download + cache full card PNGs. Lookups run on a worker thread."""

    def __init__(self) -> None:
        self._mem: Dict[str, Any] = {}  # key -> PhotoImage
        self._inflight: set = set()
        self._lock = threading.Lock()
        self._callbacks: Dict[str, List] = {}

    def key(self, grp_id: int, name: str) -> str:
        if grp_id and int(grp_id) > 0:
            return f"id_{int(grp_id)}"
        return f"name_{_slug(name)}"

    def cached_path(self, grp_id: int, name: str) -> Optional[Path]:
        candidates = []
        if grp_id and int(grp_id) > 0:
            candidates.append(IMAGE_DIR / f"id_{int(grp_id)}.png")
        if name and not _looks_like_placeholder(name, grp_id if grp_id else None):
            candidates.append(IMAGE_DIR / f"name_{_slug(name)}.png")
        for p in candidates:
            if p.exists() and p.stat().st_size > 2000:
                return p
        return None

    def request(self, grp_id: int, name: str, on_ready) -> Optional[Path]:
        path = self.cached_path(grp_id, name)
        if path:
            return path
        key = self.key(grp_id, name)
        with self._lock:
            self._callbacks.setdefault(key, []).append(on_ready)
            if key in self._inflight:
                return None
            self._inflight.add(key)
        threading.Thread(target=self._download, args=(grp_id, name, key), daemon=True).start()
        return None

    def _download(self, grp_id: int, name: str, key: str) -> None:
        from urllib.parse import quote

        urls = []
        dest = IMAGE_DIR / f"{key}.png"
        # Basics: Scryfall's arena_id is often a different basic land. Name first.
        is_basic = name in BASIC_LANDS or (name or "").startswith("Snow-Covered")
        if name and not _looks_like_placeholder(name, grp_id if grp_id else None):
            q = quote(name)
            urls.append(f"https://api.scryfall.com/cards/named?exact={q}&format=image&version=png")
            if not is_basic:
                urls.append(f"https://api.scryfall.com/cards/named?fuzzy={q}&format=image&version=png")
        if grp_id and int(grp_id) > 0 and not is_basic:
            urls.append(f"https://api.scryfall.com/cards/arena/{int(grp_id)}?format=image&version=png")
        ok = False
        for url in urls:
            try:
                req = urllib.request.Request(url, headers=SCRYFALL_UA)
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = resp.read()
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                if len(data) < 2000:
                    continue
                # Scryfall png endpoint really is PNG. If we got JPEG, still save; loader handles it.
                suffix = ".jpg" if "jpeg" in ctype or data[:3] == b"\xff\xd8\xff" else ".png"
                dest = IMAGE_DIR / f"{key}{suffix}"
                dest.write_bytes(data)
                # keep a .png alias if we stored jpg under png name earlier
                if suffix == ".png" or dest.suffix == ".jpg":
                    ok = True
                    break
            except Exception:
                continue
            finally:
                time.sleep(0.05)
        with self._lock:
            self._inflight.discard(key)
            cbs = self._callbacks.pop(key, [])
        path = self.cached_path(grp_id, name) or (dest if ok and dest.exists() else None)
        for cb in cbs:
            try:
                cb(path)
            except Exception:
                pass

    def photo(self, path: Path, root, max_w: int = 252, max_h: int = 352) -> Optional[Any]:
        """Return a PhotoImage, resized to preview size. Kept alive in self._mem."""
        key = f"{path}:{max_w}x{max_h}"
        if key in self._mem:
            return self._mem[key]
        try:
            from PIL import Image, ImageTk  # type: ignore

            im = Image.open(path)
            # full card, readable but not huge
            im.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(im, master=root)
        except Exception:
            try:
                import tkinter as tk

                photo = tk.PhotoImage(file=str(path), master=root)
                w = int(photo.width())
                if w > max_w:
                    factor = max(2, round(w / max_w))
                    photo = photo.subsample(factor, factor)
            except Exception:
                return None
        self._mem[key] = photo
        return photo


# ---------------------------------------------------------------------------
# Manual / demo decks
# ---------------------------------------------------------------------------

def parse_deck_text(text: str, names: CardNames) -> Tuple[Counter, str]:
    """
    Accepts:
      4 Lightning Bolt
      4 Lightning Bolt (M21) 162
      Deck
      4 Lightning Bolt
      Sideboard
      ...
    Stores under synthetic negative ids so we don't collide with Arena grpIds.
    """
    counts: Counter = Counter()
    alias: Dict[int, str] = {}
    next_id = -1
    name_to_id: Dict[str, int] = {}
    section = "main"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        low = line.lower()
        if low in ("deck", "main", "maindeck", "main deck"):
            section = "main"
            continue
        if low in ("sideboard", "side", "sb"):
            section = "side"
            continue
        if section != "main":
            continue
        m = re.match(r"^(\d+)\s+[xX]?\s*(.+)$", line)
        if not m:
            m = re.match(r"^(.+?)\s+[xX]\s*(\d+)$", line)
            if m:
                qty, name = int(m.group(2)), m.group(1)
            else:
                qty, name = 1, line
        else:
            qty, name = int(m.group(1)), m.group(2)
        name = re.sub(r"\s+\([^)]+\)\s+\S+$", "", name).strip()
        if name not in name_to_id:
            name_to_id[name] = next_id
            alias[next_id] = name
            next_id -= 1
        counts[name_to_id[name]] += qty
    for iid, nm in alias.items():
        names.ids[str(iid)] = nm
    names.save()
    return counts, "Imported list"


DEMO_DECK = """
4 Lightning Bolt
4 Consider
4 Counterspell
4 Ledger Shredder
3 Snapcaster Mage
2 Vendilion Clique
4 Spirebluff Canal
4 Steam Vents
4 Island
3 Mountain
2 Otawara, Soaring City
3 Expressive Iteration
2 Spell Pierce
2 Mystical Dispute
3 Preordain
2 Brazen Borrower
3 Treasure Cruise
2 Shark Typhoon
"""


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def set_always_on_top(root) -> None:
    root.attributes("-topmost", True)
    if sys.platform.startswith("win"):
        try:
            import ctypes

            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            if not hwnd:
                hwnd = root.winfo_id()
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        except Exception:
            pass


class ObsBridge:
    """Write live HTML/text files for an OBS Browser Source."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_sig = ""

    def clear(self) -> None:
        blank = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='1'>"
            "<style>html,body{margin:0;padding:0;width:480px;height:1080px;background:transparent;}</style>"
            "</head><body></body></html>"
        )
        try:
            OBS_DIR.mkdir(exist_ok=True)
            (OBS_DIR / "playerdeck.html").write_text(blank, encoding="utf-8")
            (OBS_DIR / "oppdeck.html").write_text(blank, encoding="utf-8")
            (OBS_DIR / "you.txt").write_text("", encoding="utf-8")
            (OBS_DIR / "opp.txt").write_text("", encoding="utf-8")
            self._last_sig = ""
        except Exception:
            pass

    def publish(
        self,
        snap: Dict[str, Any],
        you_rows: List[Tuple],
        opp_rows: List[Tuple],
        meta_snap: Optional[Dict[str, Any]] = None,
    ) -> None:
        you_lines = [f"{qty} {nm}" for nm, qty, *_rest in you_rows if qty > 0]
        opp_lines = []
        last_z = None
        for nm, qty, *_rest in opp_rows:
            if not nm or str(nm).startswith("#"):
                continue
            zone = _rest[-1] if _rest else ""
            if zone != last_z and zone and zone != "lib":
                opp_lines.append(f"[{str(zone).upper()}]")
                last_z = zone
            if qty > 0:
                opp_lines.append(f"{qty} {nm}")
        pred_block = self._prediction_html(meta_snap)
        pred_txt = self._prediction_text(meta_snap)
        sig = "\n".join(you_lines) + "||" + "\n".join(opp_lines) + "||" + pred_txt
        with self._lock:
            if sig == self._last_sig:
                return
            self._last_sig = sig
        try:
            OBS_DIR.mkdir(exist_ok=True)
            (OBS_DIR / "you.txt").write_text("\n".join(you_lines) + ("\n" if you_lines else ""), encoding="utf-8")
            opp_txt = list(opp_lines)
            if pred_txt:
                opp_txt = [pred_txt, ""] + opp_txt
            (OBS_DIR / "opp.txt").write_text("\n".join(opp_txt) + ("\n" if opp_txt else ""), encoding="utf-8")
            (OBS_DIR / "playerdeck.html").write_text(
                self._page(str(snap.get("deck_name") or "Your library"), you_lines),
                encoding="utf-8",
            )
            (OBS_DIR / "oppdeck.html").write_text(
                self._page(
                    str(snap.get("opponent_name") or "Opponent"),
                    opp_lines,
                    extra=pred_block,
                ),
                encoding="utf-8",
            )
            (OBS_DIR / "README.txt").write_text(
                "OBS Browser Source size: 480 x 1080 (vertical)\n"
                "Files stay here even after you close the app.\n"
                f"{OBS_DIR}\n"
                "playerdeck.html — your library\n"
                "oppdeck.html — opponent + meta prediction\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    def _prediction_text(self, meta_snap: Optional[Dict[str, Any]]) -> str:
        if not meta_snap:
            return ""
        pred = meta_snap.get("prediction") or {}
        matches = pred.get("matches") or []
        if not matches:
            return str(meta_snap.get("status") or "")
        top = matches[0]
        pct = int(round(float(top.get("score") or 0) * 100))
        line = f"{top.get('name', '?')} {pct}%"
        likely = top.get("likely") or []
        if likely:
            line += " — " + ", ".join(likely[:5])
        return line

    def _prediction_html(self, meta_snap: Optional[Dict[str, Any]]) -> str:
        if not meta_snap:
            return ""
        pred = meta_snap.get("prediction") or {}
        matches = pred.get("matches") or []
        fmt = _esc(str(meta_snap.get("format") or "").upper())
        if not matches:
            st = _esc(str(meta_snap.get("status") or "Reading metagame…"))
            return f"<div class='pred'><div class='pk'>META {fmt}</div><div class='pn'>{st}</div></div>"
        bits = [f"<div class='pred'><div class='pk'>META {fmt}</div>"]
        for i, m in enumerate(matches[:3]):
            pct = int(round(float(m.get("score") or 0) * 100))
            name = _esc(str(m.get("name") or "?"))
            cls = "pn" if i == 0 else "ps"
            bits.append(f"<div class='{cls}'>{name}  {pct}%</div>")
            if i == 0:
                likely = m.get("likely") or []
                if likely:
                    bits.append(f"<div class='pl'>also: {_esc(', '.join(likely[:6]))}</div>")
        bits.append("</div>")
        return "".join(bits)

    def _page(self, title: str, lines: List[str], extra: str = "") -> str:
        if not lines:
            body = "<div class='empty'>no public cards yet</div>"
        else:
            bits = []
            stripe = 0
            for line in lines:
                if line.startswith("[") and line.endswith("]"):
                    bits.append(f"<div class='sec'>{_esc(line[1:-1])}</div>")
                    stripe = 0
                else:
                    bits.append(f"<div class='row r{stripe % 2}'>{_esc(line)}</div>")
                    stripe += 1
            body = "".join(bits)
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=480, height=1080'>"
            "<meta http-equiv='refresh' content='1'>"
            "<style>"
            "*{box-sizing:border-box;}"
            f"html,body{{margin:0;padding:0;width:{OBS_WIDTH}px;height:{OBS_HEIGHT}px;"
            "overflow:hidden;background:rgba(16,28,52,.45);color:#e8e6df;"
            "font-family:Segoe UI,sans-serif;font-size:18px;line-height:1.25;}}"
            ".col{width:100%;height:100%;background:rgba(18,32,58,.55);"
            "border-radius:12px;padding:12px 10px;overflow:hidden;}"
            "h1{margin:0 0 10px;padding:0 6px;font-size:15px;letter-spacing:.14em;color:#c9a227;}"
            ".row{padding:4px 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-radius:4px;}"
            ".r0{background:rgba(22,38,68,.50);}"
            ".r1{background:rgba(28,46,80,.50);}"
            ".sec{margin:8px 6px 4px;color:#c9a227;font-size:12px;letter-spacing:.12em;}"
            ".empty{color:#8b8a84;}"
            ".pred{margin:0 0 12px;padding:10px 12px;background:rgba(42,33,16,.92);"
            "border:1px solid #c9a227;border-radius:8px;}"
            ".pk{font-size:11px;letter-spacing:.14em;color:#c9a227;}"
            ".pn{font-size:18px;font-weight:700;margin-top:3px;}"
            ".ps{font-size:14px;color:#8b8a84;margin-top:2px;}"
            ".pl{font-size:13px;color:#e8e6df;margin-top:4px;}"
            "</style></head><body><div class='col'>"
            f"<h1>{_esc(title).upper()}</h1>{extra}{body}"
            "</div></body></html>"
        )


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


