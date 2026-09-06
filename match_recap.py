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
    "AnnotationType_ModifiedLife",  # life is written from player totals
    "AnnotationType_Tapped",
    "AnnotationType_TappedAffected",
    "AnnotationType_PhaseOrStepModified",
    "AnnotationType_Attachment",
    "AnnotationType_CounterAdded",
    "AnnotationType_CounterRemoved",
}

_WIN_WORDS = re.compile(r"Win|Won|Victory|Team1Won|Team2Won", re.I)


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
        self._phase = ""
        self._life: Dict[int, int] = {}
        self._attacking: Set[int] = set()
        self._header_written = False
        self._game_open = False

    def set_players(self, seat_names: Dict[int, str], match_id: str = "") -> None:
        if match_id and match_id != self._match_id and self.path:
            self.close_match("New match started")
        if match_id:
            self._match_id = str(match_id)
        if seat_names:
            self._maybe_rename()

    def close_match(self, reason: str = "Match ended") -> None:
        if self._game_open:
            self._line(f"--- {reason} ---")
            self._game_open = False
        self.path = None
        self._started = None
        self._seen_ann.clear()
        self._turn_key = None
        self._phase = ""
        self._life.clear()
        self._attacking.clear()
        self._header_written = False

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
            self._turn_key = None
            self._phase = ""
            self._life.clear()
            self._attacking.clear()
        elif starting and game_no and self._game_no and game_no != self._game_no:
            self._line("")
            self._game_no = game_no
            self._line(f"========== Game {self._game_no} ==========")
            self._turn_key = None
            self._phase = ""
            self._life.clear()
            self._attacking.clear()
            self._game_open = True

        if not self.path and not starting and not over:
            return
        if starting or over or self._game_open:
            self._ensure_file()

        turn = gsm.get("turnInfo") or {}
        tnum = turn.get("turnNumber")
        active = turn.get("activePlayer") or turn.get("decisionPlayer")
        if tnum is not None:
            try:
                tnum = int(tnum)
            except Exception:
                tnum = None
        if active is not None:
            try:
                active = int(active)
            except Exception:
                active = None
        if tnum is not None and active is not None:
            key = (tnum, active)
            if key != self._turn_key:
                self._turn_key = key
                self._line("")
                self._line(f"Turn {tnum} — {self._who(active)}")
        phase = str(turn.get("phase") or "").replace("Phase_", "")
        if phase and phase != self._phase:
            self._phase = phase
            if phase.lower() in ("combat", "combatphase", "beginningofcombat"):
                self._line("  — Combat —")

        self._life_lines(gsm.get("players") or info.get("players") or [])
        self._annotations(gsm.get("annotations") or [])
        self._attacks(gsm.get("gameObjects") or [])

        if over and self._game_open:
            winner = self._winner_text(info, gsm)
            self._line(f"========== Game {self._game_no or '?'} over ==========")
            if winner:
                self._line(f"  {winner}")
            if self._life:
                bits = [f"{self._who(s)} {hp}" for s, hp in sorted(self._life.items())]
                self._line("  Life: " + ", ".join(bits))
            self._game_open = False

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
            path = self.match_dir / name.replace(".txt", f"-{n}.txt")
            n += 1
        self.path = path
        try:
            self.state.recap_path = str(path)
        except Exception:
            pass
        self._write_header()

    def _desired_name(self) -> str:
        seats = dict(getattr(self.state, "seat_names", {}) or {})
        n1 = _safe_name(seats.get(1) or "", "Seat 1")
        n2 = _safe_name(seats.get(2) or "", "Seat 2")
        ts = (self._started or _dt.datetime.now()).strftime("%Y-%m-%d %H%M")
        return f"{n1} vs {n2} {ts}.txt"

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
        you = seats.get(self.state.local_seat or 0) or "You"
        started = (self._started or _dt.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "MTGA Deck Overlay — match recap (public information only)",
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
        lines.append("Hidden zones (hand / library) are omitted. Card names appear once they become public.")
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

    def _card_name(self, iid: Any, allow_hidden: bool) -> Tuple[str, Optional[int], Optional[int]]:
        try:
            iid = int(iid)
        except Exception:
            return ("a card", None, None)
        go = self.state.objects.get(iid) or {}
        grp = go.get("grpId")
        seat = go.get("controllerSeatId") or go.get("ownerSeatId")
        try:
            seat_i = int(seat) if seat is not None else None
        except Exception:
            seat_i = None
        zid = go.get("zoneId")
        ztype = self._zone_type(zid)
        hidden = ztype in _HIDDEN_ZONES
        is_ours = (
            self.state.local_seat is not None
            and seat_i is not None
            and int(seat_i) == int(self.state.local_seat)
        )
        if hidden and not (allow_hidden and is_ours):
            return ("a card", seat_i, int(grp) if grp else None)
        pretty = None
        if grp is not None:
            pretty = self.names.get(int(grp))
        if _looks_like_placeholder(pretty, int(grp) if grp else None):
            pretty = None
        if not pretty:
            loc = go.get("name")
            if loc is not None and not isinstance(loc, str):
                pretty = self.names.resolve_loc(loc)
        if pretty and not _looks_like_placeholder(pretty):
            return (pretty, seat_i, int(grp) if grp else None)
        if grp and not hidden:
            return (f"#{int(grp)}", seat_i, int(grp))
        return ("a card", seat_i, int(grp) if grp else None)

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
            if any(t in _SKIP_ANN for t in types) and not any("ZoneTransfer" in t for t in types):
                if any("Damage" in t or "Attack" in t or "Revealed" in t for t in types):
                    pass
                else:
                    continue
            details = _ann_details(ann)
            affector = ann.get("affectorId")
            affected = ann.get("affectedIds") or []
            if not isinstance(affected, list):
                affected = [affected] if affected is not None else []

            if any("ZoneTransfer" in t for t in types):
                self._zone_transfer(details, affector, affected)
                continue
            if any("Damage" in t for t in types):
                amt = details.get("damage") or details.get("Damage") or details.get("amount")
                src, src_seat, _ = self._card_name(affector, allow_hidden=False)
                tgt = affected[0] if affected else None
                tgt_name, tgt_seat, _ = self._card_name(tgt, allow_hidden=False) if tgt else ("", None, None)
                who = self._who(src_seat) if src_seat is not None else src
                if amt is not None:
                    extra = f" to {tgt_name}" if tgt_name and tgt_name != "a card" else ""
                    self._line(f"  {src} deals {amt} damage{extra}")
                continue
            if any("CardRevealed" in t or "Revealed" in t for t in types):
                names = []
                for iid in ([affector] if affector is not None else []) + list(affected):
                    nm, seat, _ = self._card_name(iid, allow_hidden=True)
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
        for iid in iids:
            # Our own draws may name the card; opponent draws stay "a card".
            nm, seat, _ = self._card_name(iid, allow_hidden=allow_hidden)
            who = self._who(seat)
            if cat == "CastSpell":
                self._line(f"  {who} casts {nm}")
            elif cat == "PlayLand":
                self._line(f"  {who} plays {nm}")
            elif cat == "Draw":
                self._line(f"  {who} draws {nm}")
            elif cat == "Discard":
                self._line(f"  {who} discards {nm}")
            elif cat == "Mill" or (src_t == "ZoneType_Library" and dest_t == "ZoneType_Graveyard"):
                if cat and cat != "Resolve":
                    self._line(f"  {who} mills {nm}")
            elif cat in ("Exile", "ExileFromPlay", "ExileFromHand"):
                self._line(f"  {nm} is exiled")
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
            elif public_dest and nm != "a card" and dest_t == "ZoneType_Graveyard" and src_t == "ZoneType_Battlefield":
                self._line(f"  {nm} dies")
            elif public_dest and nm != "a card" and cat:
                pretty_cat = re.sub(r"([a-z])([A-Z])", r"\1 \2", cat).lower()
                self._line(f"  {nm}: {pretty_cat}")

    def _attacks(self, objs: Any) -> None:
        if not isinstance(objs, list):
            return
        now: Set[int] = set()
        names = []
        for go in objs:
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
        if names:
            self._line("  Attacks: " + ", ".join(names))
        if now:
            self._attacking = now
        elif self._phase.lower().startswith("combat") is False:
            self._attacking.clear()

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
