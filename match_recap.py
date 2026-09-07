from __future__ import annotations

import datetime as _dt
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


def _looks_like_placeholder(name: Optional[str], grp_id: Optional[int] = None) -> bool:
    if not name:
        return True
    if name.startswith("#") and name[1:].isdigit():
        return True
    if grp_id is not None and name == str(grp_id):
        return True
    return False


_PUBLIC_ZONES = {
    "ZoneType_Battlefield",
    "ZoneType_Graveyard",
    "ZoneType_Exile",
    "ZoneType_Stack",
    "ZoneType_Revealed",
    "ZoneType_Command",
}
_HIDDEN_ZONES = {"ZoneType_Library", "ZoneType_Hand", "ZoneType_Limbo", "ZoneType_Pending"}

_SKIP_ANN = {
    "AnnotationType_EnteredZoneThisTurn",
    "AnnotationType_ObjectIdChanged",
    "AnnotationType_Layer",
    "AnnotationType_ModifiedPower",
    "AnnotationType_ModifiedToughness",
    "AnnotationType_ModifiedLife",
    "AnnotationType_Tapped",
    "AnnotationType_TappedAffected",
    "AnnotationType_PhaseOrStepModified",
    "AnnotationType_CounterAdded",
    "AnnotationType_CounterRemoved",
}

_WIN_WORDS = re.compile(r"Win|Won|Victory|Team1Won|Team2Won", re.I)
_SUBTYPE = re.compile(r"(?:SubType_|subtype_)?", re.I)


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}"')
    except Exception:
        pass


def _safe_name(text: str, fallback: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "", (text or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    if s.upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}:
        s = fallback
    return (s[:40] if s else fallback)


def _ann_types(ann: Dict[str, Any]) -> List[str]:
    raw = ann.get("type") or ann.get("types") or []
    if isinstance(raw, str):
        return [raw]
    return [str(t) for t in raw if t]


def _ann_details(ann: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for d in ann.get("details") or []:
        if not isinstance(d, dict):
            continue
        key = d.get("key")
        if not key:
            continue
        if d.get("valueString") not in (None, ""):
            out[str(key)] = d.get("valueString")
        elif "valueInt32" in d and d.get("valueInt32") is not None:
            out[str(key)] = d.get("valueInt32")
        elif "valueInt64" in d and d.get("valueInt64") is not None:
            out[str(key)] = d.get("valueInt64")
    return out


def _is_token(go: Dict[str, Any]) -> bool:
    types = go.get("cardTypes") or go.get("cardType") or []
    if isinstance(types, str):
        types = [types]
    return any("Token" in str(t) for t in types)


def _pt(go: Dict[str, Any]) -> str:
    p, t = go.get("power"), go.get("toughness")
    if p is None or t is None:
        return ""
    return f"{p}/{t}"


def _type_words(go: Dict[str, Any]) -> List[str]:
    raw = go.get("cardTypes") or go.get("cardType") or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for t in raw:
        word = re.sub(r"^(CardType_|cardtype_)", "", str(t), flags=re.I).replace("_", " ").strip()
        if word.lower() in {"token", "none"}:
            continue
        if word:
            out.append(word.title())
    return out


def _subtype_words(go: Dict[str, Any]) -> List[str]:
    raw = go.get("subtypes") or go.get("subtype") or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for s in raw:
        word = re.sub(r"^(SubType_|subtype_)", "", str(s), flags=re.I).replace("_", " ").strip()
        if word:
            out.append(word.title())
    return out


class MatchRecap:
    """Write a public-info text recap of the current match."""

    def __init__(self, state: Any, names: Any, match_dir: Path) -> None:
        self.state = state
        self.names = names
        self.match_dir = match_dir
        self.match_dir.mkdir(parents=True, exist_ok=True)
        self.path: Optional[Path] = None
        self._started: Optional[_dt.datetime] = None
        self._match_id = ""
        self._game_no = 0
        self._seen_ann: Set[int] = set()
        self._turn_key: Optional[Tuple[int, int]] = None
        self._turn_num: Optional[int] = None
        self._phase = ""
        self._life: Dict[int, int] = {}
        self._attacking: Set[int] = set()
        self._blocking: Dict[int, int] = {}
        self._header_written = False
        self._game_open = False
        self._final = False
        self._stack_seen: Set[int] = set()
        self._logged_cast: Set[int] = set()
        self._bf: Dict[int, Tuple[str, Optional[int]]] = {}
        self._mull: Dict[int, int] = {}
        self._kept: Set[int] = set()
        self._last_affector: str = ""
        self._eot_dumped: Set[int] = set()
        self._saw_combat = False
        self._attached: Dict[int, int] = {}
        self._logged_target: Set[Tuple[str, str]] = set()

    def set_players(self, seat_names: Dict[int, str], match_id: str = "") -> None:
        if match_id and match_id != self._match_id and self.path:
            self.close_match("New match started")
        if match_id:
            self._match_id = str(match_id)
        if seat_names:
            self._maybe_rename()

    def close_match(self, reason: str = "Match ended") -> None:
        if self._game_open:
            self._dump_board(f"Board at {reason.lower()}")
            self._line(f"--- {reason} ---")
            self._game_open = False
        self._final = True
        self._rewrite_ids()
        self._maybe_rename()
        self.path = None
        self._started = None
        self._seen_ann.clear()
        self._turn_key = None
        self._turn_num = None
        self._phase = ""
        self._life.clear()
        self._attacking.clear()
        self._blocking.clear()
        self._header_written = False
        self._final = False
        self._stack_seen.clear()
        self._logged_cast.clear()
        self._bf.clear()
        self._mull.clear()
        self._kept.clear()
        self._last_affector = ""
        self._eot_dumped.clear()
        self._saw_combat = False
        self._attached.clear()
        self._logged_target.clear()

    def note_mulligan(self, seat: Optional[int], decision: str, count: Optional[int] = None) -> None:
        self._ensure_file()
        who = self._who(seat)
        low = (decision or "").lower()
        if "accept" in low or "keep" in low:
            self._kept.add(int(seat) if seat is not None else -1)
            extra = f" ({count} cards)" if count else ""
            self._line(f"  {who} keeps{extra}")
        elif "mull" in low:
            n = count if count is not None else self._mull.get(int(seat) if seat is not None else -1, 0) + 1
            if seat is not None:
                self._mull[int(seat)] = int(n)
            self._line(f"  {who} mulligans to {max(0, 7 - int(n))}")
        else:
            self._line(f"  {who} mulligan: {decision}")

    def note_player_mulligan_count(self, seat: int, count: int, hand_size: Optional[int] = None) -> None:
        prev = self._mull.get(int(seat), 0)
        if count > prev:
            self._mull[int(seat)] = count
            self._ensure_file()
            left = hand_size if hand_size is not None else max(0, 7 - int(count))
            self._line(f"  {self._who(seat)} mulligans to {left}")

    def consume(self, gsm: Dict[str, Any]) -> None:
        """Caller holds state.lock."""
        info = gsm.get("gameInfo") or {}
        stage = str(info.get("stage") or "")
        match_state = str(info.get("matchState") or "")
        game_no = info.get("gameNumber") or info.get("gameNubmer")
        if game_no is not None:
            try:
                game_no = int(game_no)
            except Exception:
                game_no = self._game_no or 1
        mid = str(info.get("matchId") or info.get("matchID") or self._match_id or "")
        if mid and mid != self._match_id and self.path:
            self.close_match("New match id")
        if mid:
            self._match_id = mid

        starting = stage in ("GameStage_Start", "GameStage_Play") or "GameInProgress" in match_state
        over = stage == "GameStage_GameOver" or "GameComplete" in match_state
        if starting and not self._game_open:
            if game_no and game_no != self._game_no and self._game_no:
                self._line("")
            self._game_no = game_no or max(1, self._game_no)
            self._ensure_file()
            self._line(f"========== Game {self._game_no} ==========")
            self._game_open = True
            self._reset_game_trackers()
        elif starting and game_no and self._game_no and game_no != self._game_no:
            self._dump_board(f"End of game {self._game_no}")
            self._line("")
            self._game_no = game_no
            self._line(f"========== Game {self._game_no} ==========")
            self._reset_game_trackers()
            self._game_open = True

        if not self.path and not starting and not over:
            return
        if starting or over or self._game_open:
            self._ensure_file()

        turn = gsm.get("turnInfo") or {}
        tnum = turn.get("turnNumber")
        active = turn.get("activePlayer") or turn.get("decisionPlayer")
        try:
            tnum = int(tnum) if tnum is not None else None
        except Exception:
            tnum = None
        try:
            active = int(active) if active is not None else None
        except Exception:
            active = None
        phase = str(turn.get("phase") or "").replace("Phase_", "")
        step = str(turn.get("step") or "").replace("Step_", "")

        if tnum is not None and active is not None:
            key = (tnum, active)
            if key != self._turn_key:
                if self._turn_key is not None:
                    prev_turn = self._turn_key[0]
                    self._end_turn(prev_turn)
                self._turn_key = key
                self._turn_num = tnum
                self._saw_combat = False
                self._line("")
                self._line(f"Turn {tnum} — {self._who(active)}")
                self._stack_seen.clear()
        if phase and phase != self._phase:
            prev = self._phase
            self._phase = phase
            plow = phase.lower()
            slow = step.lower()
            if plow in ("combat", "combatphase") or slow in (
                "declareattackers",
                "declareblockers",
                "combatdamage",
            ):
                if not self._saw_combat:
                    self._line("  — Combat —")
                    self._saw_combat = True
            if plow in ("ending", "end", "endingphase") or slow in ("end", "cleanup", "endofturn"):
                self._end_turn(self._turn_num)

        self._mulligan_from_players(gsm.get("players") or info.get("players") or [])
        if self._turn_num == 1 and self._game_open:
            seats = set(self.state.seat_names or ())
            if self.state.local_seat is not None:
                seats.add(int(self.state.local_seat))
            if self.state.opponent_seat is not None:
                seats.add(int(self.state.opponent_seat))
            for s in seats or (1, 2):
                if int(s) in self._kept:
                    continue
                self._kept.add(int(s))
                n = self._mull.get(int(s))
                extra = f" after {n} mulligan(s)" if n else " (mulligan count not reported)"
                self._line(f"  {self._who(int(s))} keeps{extra}")
        self._life_lines(gsm.get("players") or info.get("players") or [])
        self._annotations(gsm.get("annotations") or [])
        self._stack_casts()
        self._attacks(gsm.get("gameObjects") or [])
        self._blocks(gsm.get("gameObjects") or [])
        self._attachments()
        self._battlefield_diff()

        if over and self._game_open:
            self._dump_board("Final board")
            winner = self._winner_text(info, gsm)
            self._line(f"========== Game {self._game_no or '?'} over ==========")
            if winner:
                self._line(f"  {winner}")
            if self._life:
                bits = [f"{self._who(s)} {hp}" for s, hp in sorted(self._life.items())]
                self._line("  Life: " + ", ".join(bits))
            self._game_open = False
            self._final = True
            self._rewrite_ids()
            self._maybe_rename()

    def _reset_game_trackers(self) -> None:
        self._turn_key = None
        self._turn_num = None
        self._phase = ""
        self._life.clear()
        self._attacking.clear()
        self._blocking.clear()
        self._stack_seen.clear()
        self._logged_cast.clear()
        self._bf.clear()
        self._mull.clear()
        self._kept.clear()
        self._last_affector = ""
        self._eot_dumped.clear()
        self._saw_combat = False
        self._attached.clear()
        self._logged_target.clear()

    def _desired_name(self) -> str:
        seats = dict(getattr(self.state, "seat_names", {}) or {})
        n1 = _safe_name(seats.get(1) or "", "Seat 1")
        n2 = _safe_name(seats.get(2) or "", "Seat 2")
        ts = (self._started or _dt.datetime.now()).strftime("%Y-%m-%d %H%M")
        tag = " FINAL" if self._final else ""
        return f"{n1} vs {n2} {ts}{tag}.txt"

    def _ensure_file(self) -> None:
        if self.path:
            self._maybe_rename()
            return
        self._started = _dt.datetime.now()
        self.match_dir.mkdir(parents=True, exist_ok=True)
        name = self._desired_name()
        path = self.match_dir / name
        n = 2
        while path.exists():
            stem = name[:-4] if name.endswith(".txt") else name
            path = self.match_dir / f"{stem}-{n}.txt"
            n += 1
        self.path = path
        try:
            self.state.recap_path = str(path)
        except Exception:
            pass
        self._write_header()

    def _maybe_rename(self) -> None:
        if not self.path or not self.path.exists():
            return
        wanted = self.match_dir / self._desired_name()
        if wanted == self.path:
            return
        if wanted.exists():
            return
        try:
            self.path.rename(wanted)
            self.path = wanted
            try:
                self.state.recap_path = str(wanted)
            except Exception:
                pass
        except Exception:
            pass

    def _write_header(self) -> None:
        if self._header_written:
            return
        seats = dict(getattr(self.state, "seat_names", {}) or {})
        started = (self._started or _dt.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "MTGA Deck Overlay — match recap (public information, verbose)",
            f"Started: {started}",
        ]
        if self._match_id:
            lines.append(f"Match id: {self._match_id}")
        if self.state.deck_name:
            lines.append(f"Your deck: {self.state.deck_name}")
        for seat in (1, 2):
            label = seats.get(seat) or f"Seat {seat}"
            extra = ""
            if self.state.local_seat and int(seat) == int(self.state.local_seat):
                extra = "  (you)"
            lines.append(f"Seat {seat}: {label}{extra}")
        lines.append("")
        lines.append("Includes casts, combat, end-of-turn boards, and cards leaving play.")
        lines.append("Hidden zones (hand / library) stay unnamed for the opponent.")
        lines.append("")
        self._raw("\n".join(lines) + "\n")
        self._header_written = True

    def _who(self, seat: Optional[int]) -> str:
        if seat is None:
            return "Unknown"
        seats = getattr(self.state, "seat_names", {}) or {}
        name = seats.get(int(seat))
        if name:
            return str(name)
        if self.state.local_seat is not None and int(seat) == int(self.state.local_seat):
            return "You"
        if self.state.opponent_seat is not None and int(seat) == int(self.state.opponent_seat):
            return self.state.opponent_name or "Opponent"
        return f"Seat {seat}"

    def _zone_type(self, zid: Any) -> str:
        if zid is None:
            return ""
        try:
            zid = int(zid)
        except Exception:
            return ""
        z = self.state.zones.get(zid) or {}
        return str(z.get("type") or "")

    def _pretty_from_grp(self, grp: Any) -> Optional[str]:
        if grp is None:
            return None
        try:
            gid = int(grp)
        except Exception:
            return None
        pretty = self.names.get(gid)
        if pretty and not _looks_like_placeholder(pretty, gid):
            return pretty
        loc = self.names.resolve_loc(gid)
        if loc and not _looks_like_placeholder(loc, gid):
            return loc
        return None

    def _describe_go(self, go: Dict[str, Any], grp: Optional[int]) -> str:
        pretty = self._pretty_from_grp(grp)
        if pretty:
            return pretty
        loc = go.get("name")
        if loc is not None and not isinstance(loc, str):
            hit = self.names.resolve_loc(loc)
            if hit and not _looks_like_placeholder(hit):
                if grp is not None:
                    try:
                        self.names.remember(int(grp), hit)
                    except Exception:
                        pass
                return hit
        for key in ("title", "cardName", "englishName"):
            val = go.get(key)
            if isinstance(val, str) and not _looks_like_placeholder(val, grp):
                return val
        bits = []
        pt = _pt(go)
        if pt:
            bits.append(pt)
        kinds = _type_words(go)
        subs = _subtype_words(go)
        if kinds:
            bits.extend(kinds)
        if subs:
            bits.append(" ".join(subs))
        if _is_token(go) and "token" not in " ".join(bits).lower():
            bits.append("token")
        if bits:
            label = " ".join(bits)
            if grp:
                return f"{label} (#{int(grp)})"
            return label
        if grp:
            return f"#{int(grp)}"
        return "a card"

    def _card_name(self, iid: Any, allow_hidden: bool) -> Tuple[str, Optional[int], Optional[int]]:
        try:
            iid = int(iid)
        except Exception:
            return ("a card", None, None)
        go = self.state.objects.get(iid) or {}
        grp = go.get("grpId")
        try:
            grp_i = int(grp) if grp is not None else None
        except Exception:
            grp_i = None
        seat = go.get("controllerSeatId") or go.get("ownerSeatId")
        try:
            seat_i = int(seat) if seat is not None else None
        except Exception:
            seat_i = None
        ztype = self._zone_type(go.get("zoneId"))
        hidden = ztype in _HIDDEN_ZONES
        is_ours = (
            self.state.local_seat is not None
            and seat_i is not None
            and int(seat_i) == int(self.state.local_seat)
        )
        if hidden and not (allow_hidden and is_ours) and not (ztype in _PUBLIC_ZONES):
            return ("a card", seat_i, grp_i)
        return (self._describe_go(go, grp_i), seat_i, grp_i)

    def _mulligan_from_players(self, players: Any) -> None:
        if not isinstance(players, list):
            return
        for p in players:
            if not isinstance(p, dict):
                continue
            seat = p.get("systemSeatNumber") or p.get("systemSeatId") or p.get("seatId")
            if seat is None:
                continue
            try:
                seat_i = int(seat)
            except Exception:
                continue
            mc = p.get("mulliganCount")
            if mc is None:
                continue
            try:
                mc = int(mc)
            except Exception:
                continue
            pending = str(p.get("pendingMessageType") or "")
            if mc and mc != self._mull.get(seat_i, 0):
                self.note_player_mulligan_count(seat_i, mc)
            if "Mulligan" in pending:
                continue
            if (
                self._game_open
                and seat_i not in self._kept
                and self._turn_num == 1
                and "mulligan" not in pending.lower()
            ):
                self._kept.add(seat_i)
                extra = f" after {self._mull.get(seat_i, 0)} mulligan(s)" if self._mull.get(seat_i) else " (no mulligan)"
                self._line(f"  {self._who(seat_i)} keeps{extra}")

    def _life_lines(self, players: Any) -> None:
        if not isinstance(players, list):
            return
        for p in players:
            if not isinstance(p, dict):
                continue
            seat = p.get("systemSeatNumber") or p.get("systemSeatId") or p.get("seatId")
            life = p.get("lifeTotal")
            if life is None:
                life = p.get("life")
            if seat is None or life is None:
                continue
            try:
                seat_i, hp = int(seat), int(life)
            except Exception:
                continue
            prev = self._life.get(seat_i)
            self._life[seat_i] = hp
            if prev is not None and prev != hp:
                delta = hp - prev
                sign = f"+{delta}" if delta > 0 else str(delta)
                self._line(f"  {self._who(seat_i)} {prev} → {hp} ({sign})")

    def _annotations(self, anns: Any) -> None:
        if not isinstance(anns, list):
            return
        for ann in anns:
            if not isinstance(ann, dict):
                continue
            aid = ann.get("id")
            if aid is not None:
                try:
                    aid_i = int(aid)
                except Exception:
                    aid_i = None
                if aid_i is not None:
                    if aid_i in self._seen_ann:
                        continue
                    self._seen_ann.add(aid_i)
                    if len(self._seen_ann) > 4000:
                        self._seen_ann = set(list(self._seen_ann)[-2000:])
            types = _ann_types(ann)
            joined = "".join(types)
            if any(t in _SKIP_ANN for t in types) and not any(
                k in joined for k in ("ZoneTransfer", "Damage", "Attack", "Block", "Reveal", "Resolution", "Target", "Attach")
            ):
                continue
            details = _ann_details(ann)
            affector = ann.get("affectorId")
            affected = ann.get("affectedIds") or []
            if not isinstance(affected, list):
                affected = [affected] if affected is not None else []

            if any("Target" in t for t in types):
                self._log_targets(affector, affected, details)
                continue
            if any("Attach" in t for t in types):
                src, _, _ = self._card_name(affector, False)
                for tgt in affected:
                    dst, _, _ = self._card_name(tgt, False)
                    self._log_pair(f"  {src} attaches to {dst}", src, dst)
                continue
            if any("ZoneTransfer" in t for t in types):
                self._zone_transfer(details, affector, affected)
                continue
            if any("Damage" in t for t in types):
                amt = details.get("damage") or details.get("Damage") or details.get("amount")
                src, src_seat, _ = self._card_name(affector, allow_hidden=False)
                tgt_bits = []
                for tgt in affected:
                    tgt_name, tgt_seat, _ = self._card_name(tgt, allow_hidden=False)
                    if tgt_name == "a card" and tgt_seat is None:
                        try:
                            maybe_seat = int(tgt)
                        except Exception:
                            maybe_seat = None
                        if maybe_seat in (1, 2):
                            tgt_bits.append(self._who(maybe_seat))
                            continue
                    if tgt_name and tgt_name != "a card":
                        tgt_bits.append(tgt_name)
                    elif tgt_seat is not None:
                        tgt_bits.append(self._who(tgt_seat))
                if amt is not None:
                    extra = (" to " + ", ".join(tgt_bits)) if tgt_bits else ""
                    self._line(f"  {src} deals {amt} damage{extra}")
                continue
            if any("CardRevealed" in t or "Revealed" in t for t in types):
                names = []
                for iid in ([affector] if affector is not None else []) + list(affected):
                    nm, _, _ = self._card_name(iid, allow_hidden=True)
                    if nm != "a card":
                        names.append(nm)
                if names:
                    self._line("  Revealed: " + ", ".join(dict.fromkeys(names)))
                continue
            if any("DeclaredAttacker" in t or "Attackers" in t for t in types):
                names = []
                for iid in affected or ([affector] if affector is not None else []):
                    nm, _, _ = self._card_name(iid, allow_hidden=False)
                    if nm != "a card":
                        names.append(nm)
                if names:
                    self._line("  Attackers: " + ", ".join(names))
                continue
            if any("DeclaredBlocker" in t or "Blocker" in t for t in types):
                blk, _, _ = self._card_name(affector if affector is not None else (affected[0] if affected else None), False)
                atk_ids = affected if affector is not None else []
                atk_names = []
                for iid in atk_ids:
                    nm, _, _ = self._card_name(iid, False)
                    if nm != "a card":
                        atk_names.append(nm)
                if blk != "a card":
                    if atk_names:
                        self._line(f"  {blk} blocks {', '.join(atk_names)}")
                    else:
                        self._line(f"  {blk} blocks")
                continue
            if any("ResolutionStart" in t or "ResolutionComplete" in t for t in types):
                nm, seat, _ = self._card_name(affector, allow_hidden=False)
                if nm != "a card" and "Start" in "".join(types):
                    if affector not in self._logged_cast:
                        who = self._who(seat)
                        self._line(f"  {who} resolves {nm}")

    def _zone_transfer(self, details: Dict[str, Any], affector: Any, affected: List[Any]) -> None:
        cat = str(details.get("category") or details.get("Category") or "")
        dest = details.get("zone_dest") or details.get("destinationZoneId")
        src = details.get("zone_src") or details.get("sourceZoneId")
        dest_t = self._zone_type(dest)
        src_t = self._zone_type(src)
        iids = list(affected) or ([affector] if affector is not None else [])
        if not iids:
            return
        public_dest = dest_t in _PUBLIC_ZONES or dest_t == "ZoneType_Stack"
        allow_hidden = cat in ("Draw", "Discard", "Mulligan")
        cause = ""
        if affector is not None:
            cname, _, _ = self._card_name(affector, allow_hidden=False)
            if cname and cname != "a card" and affector not in iids:
                cause = cname
                self._last_affector = cname
        dest_label = dest_t.replace("ZoneType_", "") or "unknown"
        for iid in iids:
            nm, seat, _ = self._card_name(iid, allow_hidden=allow_hidden)
            who = self._who(seat)
            if cat == "CastSpell" or (dest_t == "ZoneType_Stack" and cat in ("", "Cast", "Play")):
                self._line(f"  {who} casts {nm}")
                try:
                    self._logged_cast.add(int(iid))
                except Exception:
                    pass
            elif cat == "PlayLand":
                self._line(f"  {who} plays {nm}")
            elif cat == "Draw":
                self._line(f"  {who} draws {nm}")
            elif cat == "Discard":
                self._line(f"  {who} discards {nm}")
            elif cat == "Mulligan":
                self._line(f"  {who} mulligan-sends {nm}")
            elif cat == "Mill" or (src_t == "ZoneType_Library" and dest_t == "ZoneType_Graveyard" and cat != "Resolve"):
                self._line(f"  {who} mills {nm}")
            elif src_t == "ZoneType_Battlefield" and dest_t != "ZoneType_Battlefield":
                why = f" ({cause})" if cause else (f" ({cat})" if cat else "")
                verb = {
                    "ZoneType_Exile": "exiled",
                    "ZoneType_Graveyard": "dies",
                    "ZoneType_Hand": "bounced to hand",
                    "ZoneType_Library": "leaves play → library",
                    "ZoneType_Stack": "flickered onto stack",
                    "ZoneType_Command": "leaves play → command",
                }.get(dest_t, f"leaves play → {dest_label}")
                self._line(f"  {nm} {verb}{why}")
            elif cat in ("Exile", "ExileFromPlay", "ExileFromHand"):
                why = f" ({cause})" if cause else ""
                self._line(f"  {nm} is exiled{why}")
            elif cat in ("Countered", "CounterSpell"):
                self._line(f"  {nm} is countered")
            elif cat == "Resolve":
                if nm != "a card":
                    self._line(f"  {nm} resolves")
            elif cat in ("Put", "PutOnBattlefield", "Return", "ReturnToBattlefield"):
                if public_dest and nm != "a card":
                    self._line(f"  {nm} enters the battlefield")
            elif public_dest and nm != "a card" and dest_t == "ZoneType_Stack":
                self._line(f"  {who} puts {nm} on the stack")
                try:
                    self._logged_cast.add(int(iid))
                except Exception:
                    pass
            elif public_dest and nm != "a card" and cat:
                pretty_cat = re.sub(r"([a-z])([A-Z])", r"\1 \2", cat).lower()
                why = f" ({cause})" if cause else ""
                self._line(f"  {nm}: {pretty_cat}{why}")

    def _stack_casts(self) -> None:
        now: Set[int] = set()
        for z in self.state.zones.values():
            if str(z.get("type") or "") != "ZoneType_Stack":
                continue
            for iid in z.get("objectInstanceIds") or []:
                try:
                    now.add(int(iid))
                except Exception:
                    continue
        for iid in now - self._stack_seen:
            if iid in self._logged_cast:
                continue
            nm, seat, _ = self._card_name(iid, allow_hidden=False)
            if nm == "a card":
                continue
            self._line(f"  {self._who(seat)} casts {nm}")
            self._logged_cast.add(iid)
        self._stack_seen = now

    def _attacks(self, objs: Any) -> None:
        if not isinstance(objs, list):
            objs = list(self.state.objects.values())
        now: Set[int] = set()
        names = []
        for go in objs if isinstance(objs, list) else []:
            if not isinstance(go, dict):
                continue
            if not go.get("attacking"):
                continue
            iid = go.get("instanceId")
            if iid is None:
                continue
            try:
                iid = int(iid)
            except Exception:
                continue
            now.add(iid)
            if iid not in self._attacking:
                nm, _, _ = self._card_name(iid, allow_hidden=False)
                if nm != "a card":
                    names.append(nm)
        if not now:
            for iid, go in self.state.objects.items():
                if go.get("attacking"):
                    now.add(int(iid))
                    if int(iid) not in self._attacking:
                        nm, _, _ = self._card_name(iid, False)
                        if nm != "a card":
                            names.append(nm)
        if names:
            self._line("  Attacks: " + ", ".join(names))
        if now:
            self._attacking = now
        elif "combat" not in self._phase.lower():
            self._attacking.clear()

    def _blocks(self, objs: Any) -> None:
        pairs = []
        seen = {}
        pool = []
        if isinstance(objs, list):
            pool.extend(objs)
        pool.extend(self.state.objects.values())
        for go in pool:
            if not isinstance(go, dict):
                continue
            iid = go.get("instanceId")
            if iid is None:
                continue
            try:
                iid = int(iid)
            except Exception:
                continue
            blockers = (
                go.get("blockingCreatureInstanceIds")
                or go.get("blockers")
                or go.get("blockedByInstanceIds")
                or []
            )
            if isinstance(blockers, int):
                blockers = [blockers]
            if go.get("blocking") and not blockers:
                # this object is a blocker; attacker id may be in "attacker" fields
                atk = go.get("attackerInstanceId") or go.get("attackingInstanceId") or go.get("blockingAttackerInstanceId")
                if atk is not None:
                    blockers = []
                    blk_id = iid
                    atk_id = int(atk)
                    key = (blk_id, atk_id)
                    if key not in self._blocking:
                        self._blocking[blk_id] = atk_id
                        pairs.append((blk_id, atk_id))
                    continue
            if blockers:
                for b in blockers:
                    try:
                        b = int(b)
                    except Exception:
                        continue
                    key = (b, iid)
                    if self._blocking.get(b) == iid:
                        continue
                    self._blocking[b] = iid
                    pairs.append((b, iid))
        for blk, atk in pairs:
            bn, _, _ = self._card_name(blk, False)
            an, _, _ = self._card_name(atk, False)
            if bn != "a card" or an != "a card":
                self._line(f"  {bn} blocks {an}")

    def _battlefield_now(self) -> Dict[int, Tuple[str, Optional[int]]]:
        out: Dict[int, Tuple[str, Optional[int]]] = {}
        bf_ids = set()
        for z in self.state.zones.values():
            if str(z.get("type") or "") != "ZoneType_Battlefield":
                continue
            for iid in z.get("objectInstanceIds") or []:
                try:
                    bf_ids.add(int(iid))
                except Exception:
                    pass
        for iid, go in self.state.objects.items():
            ztype = self._zone_type(go.get("zoneId"))
            if ztype != "ZoneType_Battlefield" and int(iid) not in bf_ids:
                continue
            nm, seat, _ = self._card_name(iid, False)
            out[int(iid)] = (nm, seat)
        return out

    def _battlefield_diff(self) -> None:
        now = self._battlefield_now()
        if not self._bf:
            self._bf = now
            return
        left = set(self._bf) - set(now)
        for iid in sorted(left):
            nm, seat = self._bf[iid]
            go = self.state.objects.get(iid) or {}
            dest = self._zone_type(go.get("zoneId")) or "gone"
            dest_label = dest.replace("ZoneType_", "") or "gone"
            if dest == "ZoneType_Battlefield":
                continue
            why = f" ({self._last_affector})" if self._last_affector else ""
            if nm == "a card":
                continue
            # Zone-transfer line usually already covered this.
            if dest in ("ZoneType_Exile", "ZoneType_Graveyard", "ZoneType_Hand", "ZoneType_Library"):
                continue
            self._line(f"  {nm} leaves battlefield → {dest_label}{why}")
        self._bf = now

    def _end_turn(self, turn: Optional[int]) -> None:
        if turn is None:
            return
        try:
            t = int(turn)
        except Exception:
            return
        if t in self._eot_dumped:
            return
        self._eot_dumped.add(t)
        if not self._saw_combat:
            self._line("  (no combat)")
        self._dump_board(f"End of turn {t}")
        self._saw_combat = False

    def _log_pair(self, line: str, a: str, b: str) -> None:
        key = (line.strip(), a, b)
        if key in self._logged_target:
            return
        self._logged_target.add(key)
        if len(self._logged_target) > 400:
            self._logged_target = set(list(self._logged_target)[-200:])
        self._line(line)

    def _log_targets(self, affector: Any, affected: List[Any], details: Dict[str, Any]) -> None:
        src, _, _ = self._card_name(affector, allow_hidden=False)
        extra = []
        for key in ("idTarget", "target", "Target"):
            if details.get(key) is not None:
                extra.append(details.get(key))
        ids = list(affected) + extra
        names = []
        for iid in ids:
            nm, seat, _ = self._card_name(iid, allow_hidden=False)
            if nm == "a card":
                try:
                    maybe = int(iid)
                except Exception:
                    maybe = None
                if maybe in (1, 2):
                    nm = self._who(maybe)
            if nm and nm != "a card":
                names.append(nm)
        if src == "a card" and not names:
            return
        if names:
            self._log_pair(f"  {src} targets {', '.join(dict.fromkeys(names))}", src, ",".join(names))
        elif src != "a card":
            self._log_pair(f"  {src} chooses a target", src, "?")

    def _attachments(self) -> None:
        now: Dict[int, int] = {}
        for iid, go in self.state.objects.items():
            attached_to = go.get("attachedTo") or go.get("attachedToInstanceId")
            attachments = go.get("attachments") or go.get("attachedInstanceIds") or []
            if attached_to is not None:
                try:
                    now[int(iid)] = int(attached_to)
                except Exception:
                    pass
            if isinstance(attachments, int):
                attachments = [attachments]
            if isinstance(attachments, list):
                for aura in attachments:
                    try:
                        now[int(aura)] = int(iid)
                    except Exception:
                        pass
        for aura, host in now.items():
            if self._attached.get(aura) == host:
                continue
            self._attached[aura] = host
            an, _, _ = self._card_name(aura, False)
            hn, _, _ = self._card_name(host, False)
            if an == "a card" and hn == "a card":
                continue
            self._log_pair(f"  {an} attaches to {hn}", an, hn)

    def _rewrite_ids(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except Exception:
            return

        def repl(m):
            gid = int(m.group(1))
            pretty = self._pretty_from_grp(gid)
            return pretty if pretty else m.group(0)

        new = re.sub(r"#(\d+)", repl, text)
        if new != text:
            try:
                self.path.write_text(new, encoding="utf-8")
            except Exception:
                pass

    def _dump_board(self, title: str) -> None:
        now = self._battlefield_now()
        self._bf = now
        if not now:
            return
        by_seat: Dict[int, List[str]] = {}
        for iid, (nm, seat) in now.items():
            if nm == "a card":
                continue
            go = self.state.objects.get(iid) or {}
            pt = _pt(go)
            label = nm + (f" {pt}" if pt and pt not in nm else "")
            by_seat.setdefault(int(seat) if seat is not None else 0, []).append(label)
        if not by_seat:
            return
        self._line(f"  [{title}]")
        for seat in sorted(by_seat):
            cards = ", ".join(sorted(by_seat[seat]))
            self._line(f"    {self._who(seat) if seat else 'Unknown'}: {cards}")

    def _winner_text(self, info: Dict[str, Any], gsm: Dict[str, Any]) -> str:
        win = (
            info.get("winningTeamId")
            or info.get("winnerSeatId")
            or info.get("winningSeatId")
            or gsm.get("winningTeamId")
        )
        if win is not None:
            try:
                return f"Winner: {self._who(int(win))}"
            except Exception:
                return f"Winner seat: {win}"
        for node in (info, gsm):
            res = node.get("results") or node.get("resultList") or node.get("finalMatchResult")
            if isinstance(res, list):
                for r in res:
                    blob = str(r)
                    if _WIN_WORDS.search(blob):
                        return f"Result: {blob[:160]}"
            elif isinstance(res, dict):
                blob = str(res.get("result") or res.get("reason") or "")
                if blob:
                    return f"Result: {blob[:160]}"
        return ""

    def _line(self, text: str) -> None:
        if not self.path:
            self._ensure_file()
        self._raw(text.rstrip() + "\n")

    def _raw(self, text: str) -> None:
        if not self.path:
            return
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(text)
                f.flush()
        except Exception:
            pass
