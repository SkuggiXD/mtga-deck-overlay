from __future__ import annotations

import atexit
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import (
    BASIC_LANDS,
    OBS_DIR,
    SIDE_PATH,
    CardNames,
    OverlayState,
    _looks_like_placeholder,
)
from mtga_features import (
    META_FORMATS,
    CardImages,
    MetaEngine,
    ObsBridge,
    parse_deck_text,
    set_always_on_top,
)

def launch_ui(state: OverlayState, names: CardNames) -> None:
    import tkinter as tk
    from tkinter import filedialog, ttk

    BG = "#12141a"
    BG2 = "#1b1e27"
    FG = "#e8e6df"
    DIM = "#8b8a84"
    ACCENT = "#c9a227"
    LAND = "#6aa84f"
    ROW_A = "#161922"
    ROW_B = "#1c2030"

    root = tk.Tk()
    root.title("MTGA Deck Overlay")
    root.geometry("680x720+40+80")
    root.configure(bg=BG)
    root.attributes("-alpha", 0.94)
    root.overrideredirect(True)
    root.resizable(True, True)
    set_always_on_top(root)

    # drag from header
    drag = {"x": 0, "y": 0}

    def start_move(e):
        drag["x"], drag["y"] = e.x, e.y

    def do_move(e):
        x = root.winfo_x() + e.x - drag["x"]
        y = root.winfo_y() + e.y - drag["y"]
        root.geometry(f"+{x}+{y}")

    header = tk.Frame(root, bg=BG2, cursor="fleur")
    header.pack(fill="x")
    header.bind("<Button-1>", start_move)
    header.bind("<B1-Motion>", do_move)

    title = tk.Label(
        header,
        text="LIBRARY",
        bg=BG2,
        fg=ACCENT,
        font=("Segoe UI", 10, "bold"),
        anchor="w",
    )
    title.pack(side="left", padx=10, pady=6)
    title.bind("<Button-1>", start_move)
    title.bind("<B1-Motion>", do_move)

    view_mode = {"value": "you"}

    def set_view(mode: str) -> None:
        view_mode["value"] = mode
        you_btn.config(fg=ACCENT if mode == "you" else DIM)
        opp_btn.config(fg=ACCENT if mode == "opp" else DIM)

    you_btn = tk.Button(
        header, text="YOU", command=lambda: set_view("you"),
        bg=BG2, fg=ACCENT, bd=0, activebackground="#2a2e3a", activeforeground=FG,
        font=("Segoe UI", 8, "bold"), cursor="hand2",
    )
    you_btn.pack(side="left", padx=(4, 0))
    opp_btn = tk.Button(
        header, text="OPP", command=lambda: set_view("opp"),
        bg=BG2, fg=DIM, bd=0, activebackground="#2a2e3a", activeforeground=FG,
        font=("Segoe UI", 8, "bold"), cursor="hand2",
    )
    opp_btn.pack(side="left", padx=(2, 0))

    def shutdown() -> None:
        try:
            obs.clear()
        except Exception:
            pass
        root.destroy()

    def minimize() -> None:
        root.overrideredirect(False)
        root.iconify()

    def on_map(_e=None):
        if root.state() == "normal":
            root.overrideredirect(True)
            set_always_on_top(root)

    root.bind("<Map>", on_map)

    close_btn = tk.Button(
        header,
        text="✕",
        command=shutdown,
        bg=BG2,
        fg=DIM,
        bd=0,
        activebackground="#3a2030",
        activeforeground="#fff",
        font=("Segoe UI", 10),
        cursor="hand2",
    )
    close_btn.pack(side="right", padx=(0, 4))
    min_btn = tk.Button(
        header,
        text="–",
        command=minimize,
        bg=BG2,
        fg=DIM,
        bd=0,
        activebackground="#2a2e3a",
        activeforeground="#fff",
        font=("Segoe UI", 10),
        cursor="hand2",
    )
    min_btn.pack(side="right", padx=2)

    preview_side = {"value": "right"}
    if SIDE_PATH.exists():
        try:
            val = SIDE_PATH.read_text(encoding="utf-8").strip().lower()
            if val in ("left", "right"):
                preview_side["value"] = val
        except Exception:
            pass

    def set_preview_side(side: str) -> None:
        preview_side["value"] = side
        try:
            SIDE_PATH.write_text(side, encoding="utf-8")
        except Exception:
            pass
        style_side_btns()
        try:
            dock_preview()
        except Exception:
            pass

    def style_side_btns() -> None:
        on, off = ACCENT, DIM
        left_btn.config(fg=on if preview_side["value"] == "left" else off)
        right_btn.config(fg=on if preview_side["value"] == "right" else off)

    right_btn = tk.Button(
        header, text="▶", command=lambda: set_preview_side("right"),
        bg=BG2, fg=DIM, bd=0, activebackground="#2a2e3a", activeforeground=FG,
        font=("Segoe UI", 10), cursor="hand2",
    )
    right_btn.pack(side="right", padx=(0, 2))
    left_btn = tk.Button(
        header, text="◀", command=lambda: set_preview_side("left"),
        bg=BG2, fg=DIM, bd=0, activebackground="#2a2e3a", activeforeground=FG,
        font=("Segoe UI", 10), cursor="hand2",
    )
    left_btn.pack(side="right", padx=(0, 0))
    style_side_btns()

    status_lbl = tk.Label(root, text="", bg=BG, fg=DIM, font=("Segoe UI", 8), justify="left", anchor="w")
    status_lbl.pack(fill="x", padx=10, pady=(4, 0))

    meta = MetaEngine()

    totals = tk.Label(root, text="—", bg=BG, fg=FG, font=("Segoe UI", 11, "bold"), anchor="w")
    totals.pack(fill="x", padx=10, pady=(2, 4))

    pick = tk.Frame(root, bg="#2a2110", highlightbackground=ACCENT, highlightthickness=1)
    pick.pack(fill="x", padx=8, pady=(0, 6))
    pick_kicker = tk.Label(
        pick, text="MOST LIKELY NEXT DRAW", bg="#2a2110", fg=ACCENT,
        font=("Segoe UI", 7, "bold"), anchor="w",
    )
    pick_kicker.pack(fill="x", padx=10, pady=(6, 0))
    pick_name = tk.Label(
        pick, text="—", bg="#2a2110", fg=FG,
        font=("Segoe UI", 13, "bold"), anchor="w",
    )
    pick_name.pack(fill="x", padx=10, pady=(0, 0))
    pick_meta = tk.Label(
        pick, text="", bg="#2a2110", fg=DIM,
        font=("Segoe UI", 8), anchor="w",
    )
    pick_meta.pack(fill="x", padx=10, pady=(0, 8))

    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True)

    dock = tk.Frame(body, bg="#0c0d12", width=268)
    dock.pack_propagate(False)
    preview_img = tk.Label(
        dock, bg="#0c0d12", text="Hover a card", fg=DIM,
        font=("Segoe UI", 9), wraplength=240, justify="center",
    )
    preview_img.pack(fill="both", expand=True, padx=8, pady=8)

    canvas_frame = tk.Frame(body, bg=BG)

    canvas = tk.Canvas(canvas_frame, bg=BG, highlightthickness=0, bd=0)
    try:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Overlay.Vertical.TScrollbar",
            background=BG2,
            troughcolor=BG,
            bordercolor=BG,
            lightcolor=BG2,
            darkcolor=BG2,
            arrowcolor=DIM,
            relief="flat",
            borderwidth=0,
            arrowsize=12,
        )
        style.map(
            "Overlay.Vertical.TScrollbar",
            background=[("active", "#2a2e3a"), ("pressed", ACCENT)],
            arrowcolor=[("pressed", BG), ("active", FG)],
        )
        style.layout(
            "Overlay.Vertical.TScrollbar",
            [
                (
                    "Vertical.Scrollbar.trough",
                    {
                        "sticky": "ns",
                        "children": [
                            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
                        ],
                    },
                )
            ],
        )
        scroll = ttk.Scrollbar(
            canvas_frame, orient="vertical", command=canvas.yview, style="Overlay.Vertical.TScrollbar"
        )
    except Exception:
        scroll = tk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview, bg=BG2, troughcolor=BG, bd=0)
    inner = tk.Frame(canvas, bg=BG)
    inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _sync_scroll(_e=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _sync_inner_width(e):
        canvas.itemconfigure(inner_id, width=max(1, e.width))
        _sync_scroll()

    inner.bind("<Configure>", _sync_scroll)
    canvas.bind("<Configure>", _sync_inner_width)
    canvas.configure(yscrollcommand=scroll.set)
    canvas.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    canvas_frame.pack(side="left", fill="both", expand=True)

    def on_mousewheel(event):
        delta = event.delta if event.delta else 0
        if sys.platform == "darwin":
            canvas.yview_scroll(-1 * int(delta), "units")
        else:
            canvas.yview_scroll(-1 * int(delta / 120), "units")

    canvas.bind_all("<MouseWheel>", on_mousewheel)

    def on_list_motion(e):
        try:
            w = inner.winfo_containing(e.x_root, e.y_root)
        except Exception:
            return
        while w is not None:
            gid = getattr(w, "_card_gid", None)
            if gid is not None:
                schedule_preview(int(gid), getattr(w, "_card_name", "") or "")
                return
            w = getattr(w, "master", None)

    inner.bind("<Motion>", on_list_motion)
    canvas.bind("<Motion>", on_list_motion)

    footer = tk.Frame(root, bg=BG2)
    footer.pack(fill="x")

    def load_text_deck():
        path = filedialog.askopenfilename(
            title="Load decklist (.txt)",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
        )
        if not path:
            return
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        counts, label = parse_deck_text(text, names)
        with state.lock:
            state.start_counts = counts
            state.library_counts = None
            state.deck_name = Path(path).stem
            state.status = f"Loaded {label}: {state.deck_name}"

    def open_obs_folder() -> None:
        OBS_DIR.mkdir(exist_ok=True)
        # Touch files so the folder isn't empty on first click.
        for name in ("playerdeck.html", "oppdeck.html", "you.txt", "opp.txt"):
            p = OBS_DIR / name
            if not p.exists():
                p.write_text("", encoding="utf-8")
        try:
            if sys.platform == "win32":
                os.startfile(str(OBS_DIR))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{OBS_DIR}"')
            else:
                os.system(f'xdg-open "{OBS_DIR}"')
        except Exception:
            pass

    tk.Button(
        footer,
        text="OBS",
        command=open_obs_folder,
        bg=BG2,
        fg=ACCENT,
        bd=0,
        activebackground="#2a2e3a",
        font=("Segoe UI", 8, "bold"),
        cursor="hand2",
    ).pack(side="left", padx=(8, 2), pady=4)

    tk.Button(
        footer,
        text="Load .txt deck",
        command=load_text_deck,
        bg=BG2,
        fg=FG,
        bd=0,
        activebackground="#2a2e3a",
        font=("Segoe UI", 8),
        cursor="hand2",
    ).pack(side="left", padx=8, pady=4)

    fmt_btns: Dict[str, Any] = {}

    def style_fmt() -> None:
        for slug, btn in fmt_btns.items():
            btn.config(fg=ACCENT if meta.format == slug else DIM)

    def pick_fmt(slug: str) -> None:
        meta.set_format(slug)
        style_fmt()

    for slug, short in META_FORMATS:
        b = tk.Button(
            footer, text=short, command=lambda s=slug: pick_fmt(s),
            bg=BG2, fg=DIM, bd=0, activebackground="#2a2e3a",
            font=("Segoe UI", 7, "bold"), cursor="hand2",
        )
        b.pack(side="left", padx=1)
        fmt_btns[slug] = b
    style_fmt()

    opacity = tk.DoubleVar(value=0.94)

    def set_opacity(v):
        try:
            root.attributes("-alpha", float(v))
        except Exception:
            pass

    tk.Scale(
        footer,
        from_=0.45,
        to=1.0,
        resolution=0.01,
        orient="horizontal",
        variable=opacity,
        command=set_opacity,
        bg=BG2,
        fg=DIM,
        highlightthickness=0,
        troughcolor="#2a2e3a",
        length=90,
        showvalue=0,
    ).pack(side="right", padx=8, pady=2)

    resize = {"w": 380, "h": 720, "x": 0, "y": 0}

    def start_resize(e):
        resize["w"], resize["h"] = root.winfo_width(), root.winfo_height()
        resize["x"], resize["y"] = e.x_root, e.y_root

    def do_resize(e):
        w = max(300, resize["w"] + (e.x_root - resize["x"]))
        h = max(420, resize["h"] + (e.y_root - resize["y"]))
        root.geometry(f"{w}x{h}+{root.winfo_x()}+{root.winfo_y()}")

    grip = tk.Label(footer, text="⌟", bg=BG2, fg=DIM, font=("Segoe UI", 10), cursor="size_nw_se")
    grip.pack(side="right", padx=(0, 6))
    grip.bind("<Button-1>", start_resize)
    grip.bind("<B1-Motion>", do_resize)

    row_widgets: List[tk.Widget] = []
    row_map: Dict[int, Dict[str, Any]] = {}
    last_row_ids: List[int] = []
    images = CardImages()
    obs = ObsBridge()
    atexit.register(obs.clear)

    hover = {"gid": None, "hide_job": None, "show_job": None, "pinned": False, "zoom": False}

    zoom_panel = tk.Frame(body, bg="#0c0d12", highlightbackground=ACCENT, highlightthickness=1)
    zoom_img = tk.Label(zoom_panel, bg="#0c0d12", text="", fg=DIM)
    zoom_img.pack(fill="both", expand=True, padx=6, pady=6)

    def place_zoom() -> None:
        body.update_idletasks()
        bw = max(1, body.winfo_width())
        bh = max(1, body.winfo_height())
        dw = dock.winfo_width() or 268
        # Fit the zoom card inside the list column, never outside the window.
        max_w = max(180, bw - dw - 12)
        max_h = max(220, bh - 12)
        path = None
        gid = hover.get("gid")
        name = getattr(preview_img, "_card_name", "") or ""
        if gid is not None:
            path = images.cached_path(int(gid), name)
        if path:
            photo = images.photo(path, root, max_w=min(420, max_w), max_h=min(580, max_h))
            if photo:
                zoom_img.config(image=photo, text="")
                zoom_img.image = photo
        zoom_panel.update_idletasks()
        pw = min(zoom_panel.winfo_reqwidth() or 280, max_w)
        if preview_side["value"] == "left":
            x = dw + 4
        else:
            x = max(4, bw - dw - pw - 4)
        zoom_panel.place(x=x, y=4, width=pw, height=bh - 8)

    def hide_zoom() -> None:
        hover["zoom"] = False
        try:
            zoom_panel.place_forget()
        except Exception:
            pass

    def show_zoom(gid: int, name: str) -> None:
        hover["zoom"] = True
        path = images.cached_path(gid, name)
        if not path:
            zoom_img.config(image="", text="Loading…")
            place_zoom()

            def ready(p):
                if hover.get("zoom") and hover.get("gid") == gid:
                    root.after(0, lambda: show_zoom(gid, name))

            images.request(gid, name, ready)
            return
        place_zoom()

    def on_dock_enter(_e=None):
        hover["pinned"] = True
        gid = hover.get("gid")
        if gid is None:
            return
        nm = getattr(preview_img, "_card_name", "") or ""
        show_zoom(int(gid), nm)

    def on_dock_leave(e):
        try:
            w = body.winfo_containing(e.x_root, e.y_root)
        except Exception:
            w = None
        cur = w
        while cur is not None:
            if cur is zoom_panel or cur is zoom_img:
                return
            cur = getattr(cur, "master", None)
        hover["pinned"] = False
        hide_zoom()
        schedule_hide()

    def on_zoom_leave(e):
        try:
            w = body.winfo_containing(e.x_root, e.y_root)
        except Exception:
            w = None
        cur = w
        while cur is not None:
            if cur is dock or cur is preview_img or cur is zoom_panel:
                return
            cur = getattr(cur, "master", None)
        hover["pinned"] = False
        hide_zoom()

    dock.bind("<Enter>", on_dock_enter)
    dock.bind("<Leave>", on_dock_leave)
    preview_img.bind("<Enter>", on_dock_enter)
    preview_img.bind("<Leave>", on_dock_leave)
    zoom_panel.bind("<Leave>", on_zoom_leave)
    zoom_img.bind("<Leave>", on_zoom_leave)

    def dock_preview() -> None:
        dock.pack_forget()
        canvas_frame.pack_forget()
        if preview_side["value"] == "left":
            dock.pack(side="left", fill="y")
            canvas_frame.pack(side="left", fill="both", expand=True)
        else:
            canvas_frame.pack(side="left", fill="both", expand=True)
            dock.pack(side="right", fill="y")

    def hide_preview() -> None:
        hover["gid"] = None
        hover["pinned"] = False
        # Keep the last card in the dock so the pane doesn't flash empty.

    def show_path(path: Optional[Path], gid: int) -> None:
        if hover["gid"] != gid:
            return
        if not path:
            preview_img.config(image="", text="No image")
            return
        photo = images.photo(path, root)
        if not photo:
            preview_img.config(image="", text="Can't display image")
            return
        preview_img.config(image=photo, text="")
        preview_img.image = photo
        preview_img._card_name = getattr(preview_img, "_card_name", "") or ""

    def open_preview(gid: int, name: str) -> None:
        hover["gid"] = gid
        preview_img._card_name = name
        path = images.cached_path(gid, name)
        if path:
            show_path(path, gid)
            return
        preview_img.config(text="Loading…")

        def ready(p):
            root.after(0, lambda: show_path(p, gid))

        images.request(gid, name, ready)

    dock_preview()

    def schedule_preview(gid: int, name: str) -> None:
        if hover["hide_job"]:
            try:
                root.after_cancel(hover["hide_job"])
            except Exception:
                pass
            hover["hide_job"] = None
        if hover.get("gid") == gid:
            return
        if hover["show_job"]:
            try:
                root.after_cancel(hover["show_job"])
            except Exception:
                pass
        hover["show_job"] = root.after(40, lambda: open_preview(gid, name))

    def schedule_hide() -> None:
        if hover["show_job"]:
            try:
                root.after_cancel(hover["show_job"])
            except Exception:
                pass
            hover["show_job"] = None

        def later():
            if hover["pinned"]:
                return
            hide_preview()

        hover["hide_job"] = root.after(350, later)

    def _still_inside(container, x_root, y_root) -> bool:
        try:
            w = container.winfo_containing(x_root, y_root)
        except Exception:
            return False
        while w is not None:
            if w == container or w == dock:
                return True
            try:
                w = w.master
            except Exception:
                break
        return False

    def bind_hover(row, gid: int, name: str) -> None:
        def enter(_e, g=gid, n=name):
            schedule_preview(g, n)

        def leave(e, box=row):
            if _still_inside(box, e.x_root, e.y_root):
                return
            # Moving onto the next row / section header should not kill the preview.
            if _still_inside(inner, e.x_root, e.y_root):
                return
            schedule_hide()

        # Bind the whole row + children so the name, count, and padding all count.
        def bind_tree(w):
            w.bind("<Enter>", enter)
            w.bind("<Leave>", leave)
            w.bind("<Motion>", enter)
            for child in w.winfo_children():
                bind_tree(child)

        bind_tree(row)

    # dock hover bindings set next to the zoom window

    def hypergeo(have: int, deck: int, look: int = 1) -> str:
        if deck <= 0 or have <= 0:
            return "0%"
        # P(at least one in next `look` draws) ≈ 1 - C(deck-have, look)/C(deck, look)
        if look >= deck:
            return "100%" if have else "0%"
        miss = 1.0
        remaining_deck = deck
        remaining_miss = deck - have
        for _ in range(look):
            if remaining_deck <= 0:
                break
            miss *= remaining_miss / remaining_deck
            remaining_miss -= 1
            remaining_deck -= 1
            if remaining_miss < 0:
                miss = 0
                break
        pct = (1 - miss) * 100
        return f"{pct:.0f}%"

    def refresh() -> None:
        nonlocal last_row_ids
        snap = state.snapshot()
        counts: Counter = snap["counts"]
        start: Counter = snap["start"]
        source = snap["source"]

        opp_mode = view_mode["value"] == "opp"
        rows = []
        sections: List[Tuple[str, int]] = []  # header labels inserted as fake rows via section markers

        public = snap.get("opponent_public") or {}
        seen = snap.get("opponent_seen") or Counter()
        saved = snap.get("opponent_names") or {}

        def opp_name(gid: int) -> str:
            cached = saved.get(gid) or saved.get(str(gid))
            if cached and not _looks_like_placeholder(str(cached), gid):
                return str(cached)
            nm = names.get(int(gid))
            if nm and not _looks_like_placeholder(nm, gid):
                try:
                    with state.lock:
                        state.opponent_names[int(gid)] = nm
                except Exception:
                    pass
                return nm
            return nm

        opp_rows_obs = []
        present = set()
        for zone in ("Battlefield", "Stack", "Graveyard", "Exile", "Revealed", "Command"):
            zc = public.get(zone) or {}
            items = []
            for gid, qty in zc.items():
                if qty <= 0:
                    continue
                nm = opp_name(int(gid))
                items.append((nm, qty, qty, int(gid), zone))
            if not items:
                continue
            items.sort(key=lambda r: (_looks_like_placeholder(r[0], r[3]), r[0] in BASIC_LANDS, r[0].lower()))
            sections.append((zone.upper(), len(items)))
            for it in items:
                opp_rows_obs.append(it)
                present.add(it[3])
        leftover = []
        for gid, qty in seen.items():
            if qty <= 0 or int(gid) in present:
                continue
            nm = opp_name(int(gid))
            if _looks_like_placeholder(nm, int(gid)):
                names.get(int(gid))
                continue
            leftover.append((nm, qty, qty, int(gid), "Seen"))
        if leftover:
            leftover.sort(key=lambda r: r[0].lower())
            sections.append(("SEEN EARLIER", len(leftover)))
            opp_rows_obs.extend(leftover)

        if opp_mode:
            rows = list(opp_rows_obs)
        else:
            for gid, qty in counts.items():
                if qty <= 0:
                    continue
                nm = names.get(int(gid))
                started = start.get(gid, qty)
                rows.append((nm, qty, started, gid, "lib"))
            rows.sort(key=lambda r: (r[0] in BASIC_LANDS, r[0].lower()))

        total = sum(max(0, q) for _, q, _, _, _ in rows)
        started_total = sum(start.values()) or total
        turn_bit = f"  ·  T{snap['turn']}" if snap["turn"] else ""
        phase_bit = f" {snap['phase']}" if snap["phase"] else ""
        if opp_mode:
            name_bit = snap.get("opponent_name") or "Opponent"
            title.config(text=str(name_bit)[:22].upper())
        else:
            name_bit = snap["deck_name"] or "No deck yet"
            title.config(text=name_bit[:22].upper())
        src = getattr(names, "source", "")
        status_lbl.config(text=f"{snap['status']}{turn_bit}{phase_bit}" + (f"\nnames: {src}" if src else ""))
        if opp_mode:
            known = sum((snap.get("opponent_seen") or Counter()).values())
            hold = "  ·  held after game" if snap.get("opponent_hold") else ""
            totals.config(
                text=f"{total} public"
                + (f"  ·  {known} seen this game" if known else "  ·  hidden cards stay hidden")
                + hold
            )
        else:
            src_label = "in library" if source == "library" else "in selected deck"
            totals.config(text=f"{total} cards {src_label}" + (f"  ·  started {started_total}" if started_total else ""))

        if not opp_mode and rows and total > 0:
            def pick_key(r):
                nm, qty, _started, _gid, _z = r
                return (-qty, nm in BASIC_LANDS, nm.lower())
            best_nm, best_qty, _, best_gid, _z = sorted(rows, key=pick_key)[0]
            pct = (best_qty / total) * 100
            pick_name.config(text=best_nm)
            pick_meta.config(text=f"{best_qty} left  ·  {pct:.1f}% to draw next")
            pick.pack(fill="x", padx=8, pady=(0, 6))

            def _enter_pick(_e, g=best_gid, n=best_nm):
                schedule_preview(g, n)

            for w in (pick, pick_kicker, pick_name, pick_meta):
                w.bind("<Enter>", _enter_pick)
                w.bind("<Leave>", lambda e: schedule_hide())
        else:
            if opp_mode:
                seen_names = [nm for nm, qty, _s, gid, zone in rows]
                gy_names = [nm for nm, qty, _s, gid, zone in rows if zone == "Graveyard"]
                meta.consider(seen_names, gy_names)
                ms = meta.snapshot()
                pred = ms.get("prediction")
                pick.pack(fill="x", padx=8, pady=(0, 6))
                pick_kicker.config(text=f"META  ·  {ms.get('format','').upper()}")
                if pred and pred.get("matches"):
                    top = pred["matches"][0]
                    pct = int(round(top["score"] * 100))
                    pick_name.config(text=f"{top['name']}  {pct}%")
                    likely = ", ".join(top.get("likely") or [])[:70]
                    others = "  |  ".join(
                        f"{m['name']} {int(round(m['score']*100))}%"
                        for m in pred["matches"][1:3]
                    )
                    line2 = (f"also: {likely}" if likely else ms.get("status", ""))
                    if others:
                        line2 = f"{line2}\n{others}"
                    pick_meta.config(text=line2)
                else:
                    pick_name.config(text=ms.get("status") or "Reading metagame…")
                    pick_meta.config(text=f"{len(seen_names)} unique seen  ·  {ms.get('loaded',0)} lists")
            else:
                pick_name.config(text="—")
                pick_meta.config(text="")
                pick_kicker.config(text="MOST LIKELY NEXT DRAW")
                pick.pack(fill="x", padx=8, pady=(0, 6))

        ids_now = [f"{view_mode['value']}:{z}:{gid}" for *_, gid, z in rows]
        structure_changed = ids_now != last_row_ids

        if structure_changed:
            for w in row_widgets:
                w.destroy()
            row_widgets.clear()
            row_map.clear()
            last_row_ids = ids_now

        if not rows:
            if structure_changed:
                hint_txt = (
                    "No opponent cards seen yet.\n\n"
                    "Hand and library stay hidden.\n"
                    "Cards show up when they hit\n"
                    "the battlefield, stack, yard,\n"
                    "or exile."
                ) if opp_mode else (
                    "No deck seen yet.\n\n"
                    "1. Enable Detailed Logs in Arena\n"
                    "    Options → Account → Detailed Logs\n"
                    "2. Restart Arena and select a deck\n"
                    "   (or queue into a match)\n"
                    "3. Or load a .txt decklist below"
                )
                hint = tk.Label(
                    inner, text=hint_txt, bg=BG, fg=DIM, font=("Segoe UI", 9),
                    justify="left", anchor="w",
                )
                hint.pack(fill="x", padx=10, pady=12)
                row_widgets.append(hint)
        else:
            if structure_changed:
                header_row = tk.Frame(inner, bg=BG)
                header_row.pack(fill="x")
                tk.Label(header_row, text="#", bg=BG, fg=DIM, width=3, anchor="e", font=("Segoe UI", 9)).pack(side="left")
                hdr = "ZONE / CARD" if opp_mode else "CARD  (hover for art)"
                tk.Label(header_row, text=hdr, bg=BG, fg=DIM, anchor="w", font=("Segoe UI", 9)).pack(
                    side="left", fill="x", expand=True
                )
                right_hdr = "ZONE" if opp_mode else "NEXT"
                tk.Label(header_row, text=right_hdr, bg=BG, fg=DIM, width=8, anchor="e", font=("Segoe UI", 9)).pack(
                    side="right", padx=(0, 8)
                )
                row_widgets.append(header_row)

            last_zone = None
            for i, (nm, qty, started, gid, zone) in enumerate(rows):
                if opp_mode and zone != last_zone:
                    last_zone = zone
                    if structure_changed:
                        sec = tk.Label(
                            inner, text=str(zone).upper(), bg=BG, fg=ACCENT,
                            font=("Segoe UI", 8, "bold"), anchor="w", pady=4,
                        )
                        sec.pack(fill="x", padx=8)
                        row_widgets.append(sec)
                bg = ROW_A if i % 2 == 0 else ROW_B
                fg = LAND if nm in BASIC_LANDS else FG
                qty_txt = str(qty)
                pct_txt = (zone[:5].upper() if opp_mode else hypergeo(qty, total, 1))
                shown = nm if len(nm) < 34 else nm[:32] + "…"
                key = (int(gid), zone)
                if structure_changed:
                    row = tk.Frame(inner, bg=bg, cursor="hand2")
                    row.pack(fill="x")
                    qlbl = tk.Label(
                        row, text=qty_txt, bg=bg, fg=ACCENT, width=3, anchor="e",
                        font=("Consolas", 13, "bold"), pady=6,
                    )
                    qlbl.pack(side="left")
                    nlbl = tk.Label(
                        row, text=shown, bg=bg, fg=fg, anchor="w",
                        font=("Segoe UI", 12), pady=6,
                    )
                    nlbl.pack(side="left", fill="x", expand=True)
                    plbl = tk.Label(
                        row, text=pct_txt, bg=bg, fg=DIM, width=8, anchor="e",
                        font=("Consolas", 11), pady=6,
                    )
                    plbl.pack(side="right", padx=(0, 8))
                    bind_hover(row, int(gid), nm)
                    row._card_gid = int(gid)
                    row._card_name = nm
                    row_map[key] = {"qty": qlbl, "name": nlbl, "pct": plbl, "name_str": nm}
                    row_widgets.append(row)
                else:
                    rec = row_map.get(key)
                    if rec:
                        rec["qty"].config(text=qty_txt)
                        rec["pct"].config(text=pct_txt)
                        if rec.get("name_str") != nm:
                            rec["name"].config(text=shown)
                            rec["name_str"] = nm

        you_obs = []
        for gid, qty in counts.items():
            if qty <= 0:
                continue
            nm = names.get(int(gid))
            if _looks_like_placeholder(nm, int(gid)):
                continue
            you_obs.append((nm, qty, qty, int(gid), "lib"))
        you_obs.sort(key=lambda r: (r[0] in BASIC_LANDS, r[0].lower()))
        try:
            obs.publish(snap, you_obs, opp_rows_obs, meta.snapshot())
        except Exception:
            pass

        root.after(400, refresh)

    root.after(200, refresh)
    try:
        root.mainloop()
    finally:
        obs.clear()


