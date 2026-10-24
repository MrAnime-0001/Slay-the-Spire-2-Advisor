"""Always-on-top advisor overlay. Spawns reader.exe, solves each new state, shows the best line.
F8 toggles autopilot (plays the plan and ends the turn, every turn, until combat ends). F7 shows more / less.
Drag to move, Ctrl+right-click to quit.
Run the game in windowed or borderless mode, and keep this window away from the enemies.
STS2_DEMO=<recordings .jsonl> replays recorded states instead of reading the game (design checks)."""
import ctypes
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import winsound

import autoplay  # first: sets DPI awareness before any window exists
import icons
from pathing import DEATH, best_routes, direction, next_events, rolled_lineup, route_names
from runplan import describe as describe_plan, plan, relic_fit
from deck import lineup, profile
from enemies import attacks, replay_moves
from picker import choose_card, deck_pick, event_advice, hand_pick, pick, pile_pick, potion_swap, relic_value, rest_advice, shop_advice
from relics import COMMUNITY, GAMEDATA, KNOWLEDGE, Relics
from solver import fight_type, held_energy_reason, next_hand, next_turn_unblocked, solve, unmodeled_player_powers, unmodeled_powers

READER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "reader", "bin", "Release", "net10.0", "Sts2Reader.exe")
latest = {"line": None, "err": ""}
ROOM = {"Monster": "M", "Elite": "E", "Unknown": "?", "Event": "?", "Shop": "$", "Treasure": "T", "RestSite": "R", "Ancient": "A"}

# --- look: toned to the game (warm dark panel, parchment text, bronze/gold trim), drawn with system fonts only.
# It is a third-party tool: the header always says "by Yutti" (its maker); game icons come from your own install (icons.py).
BG, SURFACE, TRIM, TRIM_HI = "#16110d", "#211a14", "#8a6a3a", "#c9a45c"
TEXT, MUTED, DIM = "#efe3c8", "#a8977a", "#6b5d4a"
GOOD, WARN, BAD, HP_RED, HP_TRACK, STEEL = "#7fae5a", "#d9a441", "#b8392f", "#c0392b", "#3a1512", "#7da3c4"
ACCENT = {"ironclad": "#c0392b", "silent": "#5c9c4a", "defect": "#3f88c5", "necrobinder": "#a86fb5",
          "regent": "#e08a2e"}
CARD = {"Attack": "#b8392f", "Skill": "#4f7fa8", "Power": "#c9a45c", "Status": "#6b6b6b", "Curse": "#5e3a6e",
        "Potion": "#4fa88a"}
DRAW_RED, DISCARD_BLUE = "#d9604f", "#6fa8dc"  # next hand: from the draw pile / from the reshuffled discard
WIDTH, PAD, ICON = 360, 12, 18
VK_F7 = 0x76


def pump(stream, key):
    for line in stream:
        latest[key] = line.strip()
        if key == "line":
            latest["err"] = ""  # a good read clears one-off read errors


class Demo:
    """Stands in for the reader process: replays a recording, one state every 2 s, looping."""
    def __init__(self, path):
        with open(path, encoding="utf-8") as f:
            self.lines = [x.strip() for x in f if x.strip()]
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            for x in self.lines:
                latest["line"] = x
                time.sleep(2)

    def poll(self):
        return None

    def kill(self):
        pass


if os.environ.get("STS2_DEMO"):
    proc = Demo(os.environ["STS2_DEMO"])
else:
    proc = subprocess.Popen([READER], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    threading.Thread(target=pump, args=(proc.stdout, "line"), daemon=True).start()
    threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True).start()

root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True, "-alpha", 0.96)
POS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay_pos.txt")  # where you dragged it
root.geometry(os.environ.get("STS2_POS") or (open(POS_FILE).read().strip() if os.path.exists(POS_FILE) else "+40+40"))
root.configure(bg=TRIM)  # 1 px bronze frame around the panel
_fams = set(tkfont.families())
SERIF = next((f for f in ("Constantia", "Georgia") if f in _fams), "Segoe UI")
SANS = "Segoe UI"
F_TITLE, F_NAME, F_BIG = (SERIF, 11, "bold"), (SERIF, 10, "bold"), (SERIF, 13, "bold")
F_BODY, F_SMALL, F_CAPS = (SANS, 9), (SANS, 8), (SERIF, 8, "bold")

panel = tk.Frame(root, bg=BG)
panel.pack(padx=1, pady=1)
tk.Frame(panel, bg=BG, width=WIDTH, height=0).pack()  # fixes the width: content wraps instead of jumping
accent = tk.Frame(panel, bg=TRIM_HI, height=2)
banner = tk.Label(panel, font=(SERIF, 11, "bold"), fg=TEXT, bg=BAD, text="AUTOPILOT STOPPED — TAKE OVER", pady=4)
header = tk.Frame(panel, bg=BG)
context, primary, secondary, notes = (tk.Frame(panel, bg=BG) for _ in range(4))
err = tk.Label(panel, font=F_SMALL, fg=BAD, bg=BG, anchor="w", justify="left", wraplength=WIDTH - 2 * PAD)
footer = tk.Label(panel, font=F_SMALL, fg=DIM, bg=BG, anchor="w", text="drag to move · Ctrl+right-click to quit")
bottom = tk.Frame(panel, bg=BG, height=8)
tk.Label(header, text="Advisor", font=F_TITLE, fg=TRIM_HI, bg=BG).pack(side="left")
tk.Label(header, text="by Yutti", font=F_SMALL, fg=DIM, bg=BG).pack(side="left", padx=(6, 0), pady=(3, 0))
more = tk.Label(header, font=F_SMALL, fg=MUTED, bg=BG)
more.pack(side="right")
pill = tk.Label(header, font=(SANS, 8, "bold"), padx=6)
pill.pack(side="right", padx=(0, 8))

# --- F8 auto-play: hotkey thread sets a flag; tick() starts or cancels the worker. F7: compact / expanded. ---
f8, f7 = threading.Event(), threading.Event()
cancel = threading.Event()
worker = {"thread": None, "msg": "", "alert": False}
autoplay.listen_hotkey(f8.set)
autoplay.listen_hotkey(f7.set, VK_F7)
expanded = bool(os.environ.get("STS2_EXPANDED"))  # F7 toggles; the env var is for demo screenshots
# ponytail: runpilot.py (full-run autopilot) is parked: its out-of-combat clicks weren't reliable live.


def get_state():
    return json.loads(latest["line"]) if latest["line"] else {}


def play():
    def log(m):  # overlay line + autopilot_debug.log, so a stop can be diagnosed afterwards
        worker.update(msg="autopilot: " + m)
        with open(autoplay.DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {m}\n")
    result = autoplay.autopilot(get_state, lambda s: solve(s)[0], hand_pick, cancel, log,
                                choose=lambda g, c, l: autoplay.click_choice(g, c, l, lambda s: choose_card(s)[0]),
                                pile=lambda g, c, l: autoplay.pick_from_pile(g, c, l, pile_pick))
    log("stopped: " + result)
    if not (result.startswith(("combat over", "you died")) or result == "cancelled"):
        worker["alert"] = True  # stopped mid-fight: you're playing now, make it impossible to miss
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)


def on_f8():
    if worker["thread"] and worker["thread"].is_alive():
        cancel.set()
    elif get_state().get("status") == "combat":
        cancel.clear()
        worker["alert"] = False
        worker["thread"] = threading.Thread(target=play, daemon=True)
        worker["thread"].start()
    else:
        worker["msg"] = "autopilot: not in combat"


def click_through(on):
    """While autopilot drives the mouse the overlay must not swallow hovers/clicks meant for the game
    (potion bar, enemies, buttons under the HUD): WS_EX_TRANSPARENT passes mouse input through."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT: the real top-level window
    style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
    style = style | 0x80020 if on else style & ~0x20  # WS_EX_LAYERED | WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, -20, style)


def start_drag(e):
    root._dx, root._dy = e.x_root - root.winfo_x(), e.y_root - root.winfo_y()


root.bind_all("<ButtonPress-1>", start_drag)
root.bind_all("<B1-Motion>", lambda e: root.geometry(f"+{e.x_root - root._dx}+{e.y_root - root._dy}"))
root.bind_all("<Control-Button-3>", lambda e: close())


# --- names and icons ---
def title(kind, name):
    """The game's English name (cards:DEFEND_NECROBINDER.title -> "Defend"), else the class name spaced out."""
    t = GAMEDATA.get("text", {}).get(f"{kind}:{icons.snake(name).upper()}.title")
    return t or re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


_photos = {}


def photo(kind, name, size=ICON, badge=None):
    """Tk image for an icon, or None while it isn't available (the caller draws a stand-in). badge: a room type whose
    map icon sits small in the bottom-right corner (a "?" that rolls a fight shows the fight with a "?" on it)."""
    key = (kind, name, size, badge)
    if key not in _photos:
        from PIL import ImageTk
        img = icons.get(kind, name)
        if img is None:
            return None  # maybe still downloading: ask again next draw
        img = icons.fit(img, size)
        if badge and (b := icons.get("room", badge)) is not None:
            b = icons.fit(b, size // 2)
            img.alpha_composite(b, (size - b.width, size - b.height))
        _photos[key] = ImageTk.PhotoImage(img)
    return _photos[key]


def route_row(r, rooms, tail, state, size=18, color=TRIM):
    """A route as icons: the elite / boss it meets (pre-rolled queues) as their art, other rooms as map icons."""
    label(r, tail, F_SMALL, MUTED).pack(side="right")  # first: a long route clips its icons, never the HP
    second = (state.get("rooms") or {}).get("second_boss")  # A10: both final bosses at the end
    for t, name, orig in rooms + ([("Boss", second, "Boss")] if second and rooms and rooms[-1][0] == "Boss" else []):
        kind, art = ("encounter", name) if t in ("Elite", "Boss") and name else ("room", t)
        icon(r, kind, art, color, size, ROOM.get(t, t[:1]), badge="Unknown" if orig == "Unknown" != t else None
             ).pack(side="left", padx=(0, 2 if size > 14 else 1))


def icon(parent, kind, name, color=TRIM, size=ICON, letter=None, badge=None):
    """The icon, or a drawn stand-in (a bronze-rimmed disc with the first letter), so nothing shows as missing."""
    img = photo(kind, name, size, badge)
    if img is not None:
        return tk.Label(parent, image=img, bg=parent["bg"], bd=0)
    c = tk.Canvas(parent, width=size, height=size, bg=parent["bg"], highlightthickness=0)
    c.create_oval(1, 1, size - 1, size - 1, fill=SURFACE, outline=color, width=1.5)
    c.create_text(size / 2, size / 2 + 0.5, text=(letter or name[:1]).upper(), fill=color, font=(SERIF, size // 2, "bold"))
    return c


# --- small builders ---
def clear(frame):
    for w in frame.winfo_children():
        w.destroy()


def label(parent, text, font=F_BODY, fg=TEXT, **kw):
    return tk.Label(parent, text=text, font=font, fg=fg, bg=parent["bg"], **kw)


def row(parent, pady=1):
    r = tk.Frame(parent, bg=parent["bg"])
    r.pack(fill="x", padx=PAD, pady=pady)
    return r


def section(parent, text):
    """Section title in gold small caps with a bronze rule after it."""
    r = row(parent, (6, 2))
    label(r, text.upper(), F_CAPS, TRIM_HI).pack(side="left")
    tk.Frame(r, bg=TRIM, height=1).pack(side="left", fill="x", expand=True, padx=(6, 0), pady=(2, 0))


def chip(parent, text, fg=TEXT, bg=SURFACE, font=(SANS, 8, "bold")):
    return tk.Label(parent, text=text, font=font, fg=fg, bg=bg, padx=6, pady=1,
                    highlightthickness=1, highlightbackground=TRIM)


def bar(parent, frac, color, w=90, h=7, track=SURFACE, extra=0.0, extra_color=STEEL):
    """A filled bar (HP, scores); extra = a second segment after it (block on the HP bar)."""
    c = tk.Canvas(parent, width=w, height=h, bg=track, highlightthickness=1, highlightbackground=TRIM)
    fill = max(0.0, min(1.0, frac)) * w
    if fill:
        c.create_rectangle(0, 0, fill, h + 1, fill=color, width=0)
    if extra:
        c.create_rectangle(fill, 0, min(w, fill + extra * w), h + 1, fill=extra_color, width=0)
    return c


def lines(parent, texts, fg=MUTED, font=F_BODY):
    """str lines; a list of (text, colour) parts is one line in several colours (wraps like a label)."""
    for t in texts:
        if isinstance(t, str):
            label(row(parent, 0), t, font, fg, anchor="w", justify="left", wraplength=WIDTH - 2 * PAD).pack(fill="x")
            continue
        f = tkfont.Font(font=font)
        width = WIDTH - 2 * PAD
        n = 1 + f.measure("".join(p for p, _ in t)) // width  # ponytail: wrapped line count estimated, not measured
        box = tk.Text(row(parent, 0), font=font, bg=parent["bg"], bd=0, highlightthickness=0, wrap="word",
                      height=n, width=1, cursor="arrow", padx=0, pady=0)
        for i, (p, c) in enumerate(t):
            box.tag_configure(str(i), foreground=c or fg)
            box.insert("end", p, str(i))
        box.configure(state="disabled")
        box.pack(fill="x")


# --- views: each screen becomes a dict, draw() paints it ---
# view keys: chip (screen name), stats (callable(frame) drawing the context row), title, head (big line),
# steps (combat plan rows), outcome (Plan), ranks [(kind, name, value, label)], body [str], more [str], notes [str]
def steps_view(state, plan):
    out = []
    by_id = {f.get("combat_id"): f for f in state.get("enemies", [])}
    order = icons.screen_order(state)
    for i, (name, tgt, ok, c, cid, kind) in enumerate(plan.steps, 1):
        foe = by_id.get(cid) if cid is not None else None
        intent = ((foe or {}).get("intents") or [{}])[0].get("kind") or ""
        # which one on screen, like the map's path dots: ○●○ (only with 2+ enemies; replaces the "#2" suffix)
        pos = "".join("●" if x == cid else "○" for x in order) if len(order) > 1 and cid in order else ""
        out.append({"n": i, "name": name, "card": c, "kind": kind, "ok": ok, "pos": pos,
                    "target": re.sub(r" #\d+$", "", tgt) if tgt and pos else tgt,
                    "intent": icons.intent_icon(intent) if intent else None})
    return out


def num(x):
    return f"{round(x)}"


def character(state):
    """Your character (lower case): the reader's field, else the card pool most of your cards come from."""
    if state.get("character"):
        return state["character"].lower()
    pools = [GAMEDATA.get("cards", {}).get(c["name"], {}).get("pool") for c in state.get("deck") or state.get("hand", [])]
    pools = [p.lower() for p in pools if p and p.lower() in ACCENT]
    return max(set(pools), key=pools.count) if pools else ""


def outcome_chips(parent, plan):
    r = row(parent, (3, 2))
    est = "~" if plan.guessed else ""  # includes the card a card potion will offer: an estimate, not a sure thing
    if plan.win:
        chip(r, "LETHAL" + ("?" if est else ""), BG, GOOD).pack(side="left", padx=(0, 4))
    chip(r, f"DMG {est}{num(plan.damage)}").pack(side="left", padx=(0, 4))
    chip(r, f"BLOCK {est}{num(plan.block)}", STEEL).pack(side="left", padx=(0, 4))
    loss = round(plan.hp_loss)
    chip(r, f"HP {-loss:+d}" if loss else "HP ±0", BAD if loss > 0 else GOOD).pack(side="left")


def outcome_text(plan):
    return ("LETHAL  " if plan.win else "") + f"{num(plan.damage)} dmg · {num(plan.block)} block · {-round(plan.hp_loss):+d} HP"


def next_moves(state):
    """H2 (user: hidden info, drawn like the game's intents): one row per living enemy: this turn's intents (as
    shown), then each replayed next move (MonsterAi RNG) after a ">". A minion this turn's move summons gets a row
    with an empty first slot, then its first move. Marks are (intent icon, number): attack damage with Strength
    ("8", "1x3"), Block for defend moves, the icon alone otherwise."""
    asc, out = state.get("ascension", 0), []
    my_vuln = next((p["amount"] for p in state.get("player", {}).get("powers", []) if p["name"] == "VulnerablePower"), 0)
    weak_left = 0.6 if any(r.get("name") == "PaperKrane" for r in state.get("relics", [])) else 0.75  # solver weak_cut

    def marks(ai, m, powers, shown=None, k=0):
        """k: which enemy turn (0 = this one): their Weak (x0.75) / our Vulnerable (x1.5) apply while they last."""
        weak, vuln = powers.get("WeakPower", 0) > k, my_vuln > k
        mv, got = ai.get("moves", {}).get(m, {}), []
        for it in shown if shown is not None else mv.get("intents", []):
            kind = it["kind"].removesuffix("Intent")
            if kind in ("SingleAttack", "MultiAttack", "DeathBlow"):
                hits = ([(it["damage"], max(it.get("hits") or 1, 1))] if shown is not None and it.get("damage") is not None
                        else attacks(mv, asc, powers))
                mult = (weak_left if weak else 1) * (1.5 if vuln else 1)
                n = " ".join(f"{max(0, math.floor((d + powers.get('StrengthPower', 0)) * mult))}" + (f"x{h}" if h > 1 else "")
                             for d, h in hits) + (" W" if weak else "") + (" V" if vuln else "")
            elif kind == "Defend":
                n = f"{sum(x['block'] for x in mv.get('effects', []) if isinstance(x.get('block'), (int, float)))}"
            else:
                n = ""
            got.append((icons.intent_icon(it["kind"]), n or ("?" if "Attack" in kind else "")))
        return list(dict.fromkeys(got)) or [("unknown", "")]

    for e, ms in zip(state["enemies"], replay_moves(state["enemies"], state.get("monster_ai_rng"), 2) or [None] * 99):
        if e["creature"]["hp"] <= 0:
            continue
        ai = GAMEDATA["monsters"].get(e["name"], {})
        powers = {p["name"]: p["amount"] for p in e["creature"].get("powers", [])}
        turns = ([marks(ai, e.get("move"), powers, e.get("intents") or [])]
                 + [marks(ai, m, powers, k=k) for k, m in enumerate(ms or [], 1)])
        out.append((title("monsters", e["name"]), turns))
        for x in ai.get("moves", {}).get(e.get("move"), {}).get("effects", []):  # summoned next: empty now > first move
            mai = GAMEDATA["monsters"].get(x.get("summon"), {})
            first = mai.get("start") if isinstance(mai.get("start"), str) else None
            if x.get("summon"):
                out.append((title("monsters", x["summon"]) + " (new)", [[], marks(mai, first, {}) if first else [("unknown", "")]]))
    return out


def next_move_rows(parent, rows):
    """next_moves() drawn as the game shows intents: per enemy, icon + number, turn after turn."""
    if not rows:
        return
    label(row(parent, (4, 0)), "next moves", F_SMALL, DIM, anchor="w").pack(fill="x")
    for name, turns in rows:
        r = row(parent, 0)
        label(r, name, F_SMALL, MUTED, width=14, anchor="w").pack(side="left")
        for k, marks in enumerate(turns):
            if k:
                label(r, ">", F_SMALL, DIM).pack(side="left", padx=4)
            if not marks:  # not on the field yet (summoned this turn)
                label(r, "·", F_SMALL, DIM, width=2).pack(side="left")
            for ic, n in marks:
                icon(r, "intent", ic, MUTED, 16).pack(side="left")
                tags = [t for t in n.split(" ") if t in ("W", "V")]
                n = " ".join(t for t in n.split(" ") if t not in ("W", "V"))
                if n:
                    label(r, n, F_CAPS, TEXT).pack(side="left", padx=(1, 1 if tags else 4))
                for t in tags:  # W: their Weak cuts it (green) · V: our Vulnerable raises it (red)
                    label(r, t, F_SMALL, GOOD if t == "W" else BAD).pack(side="left", padx=(0, 3))


def step_rows(parent, steps):
    if not steps:
        label(row(parent), "End turn", F_NAME, TEXT).pack(side="left")
        return
    for s in steps:
        r = row(parent, 1)
        label(r, f"{s['n']}", F_CAPS, TRIM_HI, width=2, anchor="w").pack(side="left")
        c = s["card"]
        if s["kind"] == "potion":
            icon(r, "potion", c["name"], CARD["Potion"], 16).pack(side="left", padx=(0, 6))
        else:
            tk.Frame(r, bg=CARD.get(c.get("type"), TRIM), width=3, height=16).pack(side="left", padx=(0, 7))
        name = title("potions" if s["kind"] == "potion" else "cards", c["name"]) + ("+" if c.get("upgraded") else "")
        label(r, name, F_NAME, TEXT).pack(side="left")
        if s["target"]:
            label(r, "→", F_BODY, DIM).pack(side="left", padx=(6, 3))
            if s["intent"]:
                icon(r, "intent", s["intent"], MUTED, 14).pack(side="left", padx=(0, 3))
            label(r, title("", s["target"]), F_BODY, MUTED).pack(side="left")
            if s.get("pos"):
                label(r, s["pos"], F_SMALL, TRIM_HI).pack(side="left", padx=(5, 0))
        if not s["ok"]:
            chip(r, "?", BG, WARN).pack(side="right")  # the solver doesn't model this play's effect


def rank_rows(parent, ranks):
    """Ranked options: icon (relic/potion) or a card-type bar, name, score bar scaled to the best, label."""
    vals = [v for _, _, v, _ in ranks if isinstance(v, (int, float))]
    lo, hi = (min(vals + [0]), max(vals + [0])) if vals else (0, 0)
    for i, (kind, name, v, extra) in enumerate(ranks):
        r = row(parent, 1)
        if kind in ("relic", "potion"):
            icon(r, kind, name, TRIM, 16).pack(side="left", padx=(0, 6))
        elif kind == "card":
            ctype = GAMEDATA.get("cards", {}).get(name, {}).get("type")
            tk.Frame(r, bg=CARD.get(ctype, TRIM), width=3, height=14).pack(side="left", padx=(0, 7))
        text = title({"relic": "relics", "potion": "potions", "card": "cards"}.get(kind, ""), name) if kind else name
        label(r, text, F_NAME if i == 0 else F_BODY, TEXT if i == 0 else MUTED).pack(side="left")
        if extra:  # beside the name, so the score column stays aligned
            label(r, extra, F_SMALL, BAD if "dilutes" in extra else DIM).pack(side="left", padx=(6, 0))
        if isinstance(v, (int, float)):
            label(r, f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}", F_SMALL, MUTED, width=4, anchor="e").pack(side="right")
            if hi > lo:
                bar(r, (v - lo) / (hi - lo), TRIM_HI if i == 0 else TRIM, 60, 5).pack(side="right", padx=(6, 4))


def combat_stats(state):
    p = state["player"]

    def draw(f):
        r = row(f, (4, 1))
        chip(r, f"COMBAT · {fight_type(state).upper()}", TRIM_HI).pack(side="left")
        label(r, f"Turn {state['turn']}", F_SMALL, MUTED).pack(side="right")
        r = row(f, (4, 2))
        icon(r, "ui", "hp", HP_RED, 16, "♥").pack(side="left")
        label(r, f"{p['hp']}/{p['max_hp']}", F_NAME, TEXT).pack(side="left", padx=(3, 5))
        bar(r, p["hp"] / max(p["max_hp"], 1), HP_RED, 70, 7, HP_TRACK,
            extra=min(p["block"], p["max_hp"]) / max(p["max_hp"], 1)).pack(side="left", pady=(2, 0))
        if p["block"]:
            icon(r, "ui", "block", STEEL, 16, "B").pack(side="left", padx=(8, 2))
            label(r, f"{p['block']}", F_NAME, STEEL).pack(side="left")
        ch = character(state)
        label(r, f"{state['energy']}/{state['max_energy']}", F_NAME, TEXT).pack(side="right")
        icon(r, "energy", ch or "colorless", ACCENT.get(ch, TRIM_HI), 18, "E").pack(side="right", padx=(0, 2))
        pets = state.get("pets") or []
        if state.get("stars") or state.get("orbs") or pets:
            r = row(f, (0, 1))
            if state.get("stars"):
                icon(r, "ui", "star", WARN, 14, "★").pack(side="left")
                label(r, f"{state['stars']}", F_SMALL, TEXT).pack(side="left", padx=(2, 10))
            for o in reversed(state.get("orbs") or []):  # oldest (evoked first) on the right, like the game
                icon(r, "orb", o["name"], STEEL, 16, o["name"][:1]).pack(side="left", padx=(0, 2))
            for pet in reversed(pets):  # packed from the right: first pet leftmost
                label(r, f"{pet['creature']['hp']} HP", F_SMALL, MUTED).pack(side="right", padx=(2, 0))
                icon(r, "monster", pet["name"], MUTED, 18, pet["name"][:1]).pack(side="right", padx=(8, 0))
    return draw


def run_stats(state, chip_text):
    """Out-of-combat context: screen chip, HP, gold, deck size."""
    def draw(f):
        r = row(f, (4, 2))
        chip(r, chip_text.upper(), TRIM_HI).pack(side="left")
        p = state.get("player") or {}
        if "gold" in state:
            label(r, f"{state['gold']}", F_NAME, WARN).pack(side="right")
            icon(r, "ui", "gold", WARN, 16, "G").pack(side="right", padx=(8, 2))
        if p.get("max_hp"):
            label(r, f"{p['hp']}/{p['max_hp']}", F_NAME, TEXT).pack(side="right")
            icon(r, "ui", "hp", HP_RED, 16, "♥").pack(side="right", padx=(8, 2))
        if state.get("deck"):
            label(r, f"{len(state['deck'])}", F_NAME, TEXT).pack(side="right")
            icon(r, "ui", "deck", TRIM, 16, "D").pack(side="right", padx=(8, 2))
    return draw


def kit(state):
    """(notes, extra lines): things the solver doesn't model / missing game data; potions, relics, boss tip."""
    out, extra = [], []
    game, data = state.get("game_version"), GAMEDATA.get("version")
    if game and game != "?" and data != game:
        out.append(f"no game data for {game} (using {data or 'default'}): run tools/gen_gamedata.py")
    if state.get("warnings"):
        out.append("reader: " + "; ".join(state["warnings"]))
    if "relics" in state:
        if u := Relics(state["relics"], GAMEDATA["relics"]).unmodeled():
            out.append("relics not modeled: " + ", ".join(title("relics", n) for n in u))
        if foes := unmodeled_powers(state):
            out.append("enemy powers not modeled: " + ", ".join(foes))
        if state.get("status") == "combat" and (mine := unmodeled_player_powers(state)):
            out.append("your powers not modeled: " + ", ".join(mine))
    if tip := boss_tip(state):
        extra.append(f"boss tip: {tip}")
    extra += [f"counter: {t}" for t in wiki_tips(state)]
    return out, extra


try:  # tools/import_wiki.py output (local community text); optional
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki.json"), encoding="utf-8") as _f:
        WIKI = json.load(_f)
except (OSError, ValueError):
    WIKI = {}


def wiki_tips(state, n=1, width=150):
    """Elite/boss fights: the wiki's first "Useful Cards" note for each enemy type alive (markup stripped)."""
    if state.get("status") != "combat" or fight_type(state) == "Monster":
        return []
    out = []
    for name in dict.fromkeys(e["name"] for e in state.get("enemies", []) if e["creature"]["hp"] > 0):
        for t in WIKI.get("monsters", {}).get(name, {}).get("useful", [])[:n]:
            t = re.sub(r"\*\*|\[[^]]*intent\]\s*", "", t)
            out.append(t if len(t) <= width else t[:width - 1].rsplit(" ", 1)[0] + "…")
    return out


def boss_tip(state):
    """Spire-Forge boss tip for this fight: your character's, else the general one (tools/import_research.py)."""
    tips = KNOWLEDGE.get("boss_tips", {}).get(state.get("encounter")) if state.get("status") == "combat" else None
    return tips and (tips.get((state.get("character") or "").lower()) or tips.get("general"))


def ahead_line(state):
    """B2: what the act has rolled ahead (reader `rooms`): boss(es) with community death rate / HP lost, the next
    elite, hallway fight and "?" event (if that node turns out to be an event)."""
    r = state.get("rooms") or {}
    foe = lambda e: re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"(Boss|Elite|Normal|Weak)$", "", e))

    def boss(e):
        c = COMMUNITY.get("encounters", {}).get(e) or {}
        return foe(e) + (f" (~{c['dmg']:.0f} HP, {c['death']:.0%} deaths)" if c.get("dmg") else "")
    parts = ["boss " + " then ".join(boss(b) for b in (r.get("boss"), r.get("second_boss")) if b)] if r.get("boss") else []

    def crew(e):  # S4: who's in it, "2× Nibbit"; rolled picks (Ruby Raiders: 3 of 5) show as "3 rolled"
        ls = lineup(e)
        n = {}
        for m, w in ls:
            n[m] = n.get(m, 0) + (w == 1)
        rolled = round(sum(w for _, w in ls if w < 1))
        txt = ", ".join([f"{k}× {foe(m)}" if k > 1 else foe(m) for m, k in n.items() if k]
                        + ([f"{rolled} rolled"] if rolled else []))
        return f" ({txt})" if len(ls) > 1 else ""
    parts += [f"{k} {foe(r[f'next_{k}'])}{crew(r[f'next_{k}'])}" for k in ("elite", "fight") if r.get(f"next_{k}")]
    if (ev := next_events(state, 1)):
        parts.append("? event " + title("events", ev[0]))
    # H1 (user: hidden info): the next relic of each rarity from our own relic bag (elites / chests take the front;
    # the rarity is rolled 50% Common / 33% Uncommon / 17% Rare unless the source fixes it)
    bag = state.get("relic_bag") or {}
    nxt = [f"{k[0]} {title('relics', bag[k]['front'][0])}" for k in ("Common", "Uncommon", "Rare") if (bag.get(k) or {}).get("front")]
    if nxt:
        parts.append("next relic: " + " / ".join(nxt))
    return "ahead: " + " · ".join(parts) if parts else ""


def named(state, route):
    """[(room type, name, type on the map)]: the map type tells a "?" that rolls a fight from a plain fight."""
    return [(t, name, orig) for (t, name), orig in zip(route_names(state, route), route.types)]


def route_line(state, route):
    """B10: the best route's rooms by name, "?" rooms as what they roll into (seed reader). 8b: rolled groups
    (Ruby Raiders: 3 of 5) show the exact monsters from the run seed and the floor they're fought on."""
    if not route.rolled:
        return ""
    foe = lambda e: re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"(Boss|Elite|Normal|Weak)$", "", e))
    r = state.get("rooms") or {}
    parts = []
    for i, ((t, name), orig) in enumerate(zip(route_names(state, route), route.types)):
        label_ = (title("events", name) if t == "Event" else foe(name)) if name else title("", t)
        if name and r.get("total_floor") is not None and (ms := rolled_lineup(name, r.get("seed_hash"), r["total_floor"] + i + 1)):
            label_ += " (" + ", ".join(foe(m) for m in ms) + ")"
        parts.append(("?→" if orig == "Unknown" else "") + label_)
    return "route: " + " › ".join(parts[:8])


def deck_lines(state):
    """What the deck does per turn vs the act boss, and the run plan (target build, commitment, gaps)."""
    return [profile(state), describe_plan(plan(state))]


def advice_view(state, chip_text, head, body):
    """rest / event / shop: picker's (head, lines) as head + the first lines, the rest when expanded."""
    return {"stats": run_stats(state, chip_text), "head": head, "body": body[:3], "more": body[3:]}


def view_of(state, line):
    """The screen to draw for this state (the advice calls are the same as before the redesign)."""
    if state.get("choose_card"):  # Knowledge Demon curse, Attack Potion, Discovery...: click the pick
        best, rated = choose_card(state)
        return {"stats": run_stats(state, "choose a card" + (" · skip ok" if state["choose_card"]["can_skip"] else "")),
                "title": "pick", "ranks": [("card", n, None, lab) for n, _, lab in rated]}
    if state["status"] != "combat" and (g := state.get("card_grid")) and (g.get("prompt") or "Select" in (g.get("screen") or "")):
        verb, picks, ranked = deck_pick(state)  # event/rest/shop deck grid: which card to upgrade/remove/...
        return {"stats": run_stats(state, f"{verb} a card" if len(picks) < 2 else f"{verb} {len(picks)} cards"),
                "title": verb, "show": len(picks),  # user: "choose 4 to remove" lists all 4, not the top 3
                "ranks": [("card", n, None, "") for n in picks],
                "more": ["next: " + ", ".join(title("cards", n) for n in ranked if n not in picks)[:200]]}
    if state["status"] != "combat" and state.get("treasure"):  # chest: one relic per player, you vote for one
        ranked = sorted(state["treasure"], key=lambda r: -relic_value(r, state))
        return {"stats": run_stats(state, "treasure"), "title": "take",
                "ranks": [("relic", r, relic_value(r, state), relic_fit(r, plan(state), state.get("deck", []))[1])
                          for r in ranked]}
    if state.get("card_reward"):
        best, rated = pick(state["card_reward"], state["deck"], state.get("relics", []), state)
        rated = sorted(rated, key=lambda r: -r[1])
        ranks = [("card" if name in GAMEDATA.get("cards", {}) else None, name, score,
                  ("" if ok else "? ") + (why or ("" if saved is None else f"{saved:+.1f} HP" + (" · dilutes" if saved < 0 else ""))))
                 for name, score, ok, saved, why in rated]
        return {"stats": run_stats(state, "card reward"), "title": "pick", "ranks": ranks, "more": deck_lines(state),
                "head": "Skip: none is worth a deck slot" if best == "Skip" else None}
    if (state.get("rest_site") or {}).get("chosen") is False:
        return advice_view(state, "rest site", *rest_advice(state))
    if state.get("event") and not state.get("map_open"):
        return advice_view(state, "event", *event_advice(state))
    if state.get("shop") and not state.get("map_open"):
        head, body = shop_advice(state)
        v = advice_view(state, "shop", head, body)
        v["more"] += deck_lines(state)
        return v
    pots = [t.split(":", 2)[2] for t in (state.get("ui") or {}).get("targets", []) if t.startswith("reward:potion:")]
    if state["status"] != "combat" and (swaps := [(p, sw) for p in pots if (sw := potion_swap(state, p))]):
        p, (drop, gain) = max(swaps, key=lambda x: x[1][1])  # rewards with full slots: discard first, then claim
        return advice_view(state, "rewards", f"Swap: drop {title('potions', drop)} for {title('potions', p)}",
                           [f"worth +{gain:.1f} more than {title('potions', drop)}"])
    if state["status"] == "out_of_combat" and (routes := best_routes(state)):
        best = routes[0]
        return {"stats": run_stats(state, f"map · act {state['act'] + 1}"), "title": "next",
                "head": f"{title('', best.types[0])}  ({direction(state, best.coords[0])})",
                "route": (named(state, best), f"HP at boss ~{best.hp}"),
                # the other first steps, each with its route's rooms (elites / boss as their art)
                "alt_routes": [(named(state, r), f"{direction(state, r.coords[0]).split()[0]} ~{r.hp}"
                                + (" DIES" if r.score == DEATH else "")) for r in routes[1:]],
                "more": [x for x in [ahead_line(state), route_line(state, best)] if x]}
    if state["status"] != "combat":
        text = {"waiting_for_game": "waiting for game", "no_run": "no run in progress",
                "game_over": "run over"}.get(state["status"], "not in combat")
        return {"stats": run_stats(state, text)}
    if state.get("phase") == "Play":
        record(state, line)
    v = {"stats": combat_stats(state),
         "more": ([f"draw {len(state['draw'])} · discard {len(state.get('discard', []))} · exhaust "
                   f"{len(state.get('exhaust', []))} · next turn ~{next_turn_unblocked(state):.0f} unblocked"]
                  if "draw" in state else [])}
    if state.get("card_grid"):  # Headbutt, Hologram, Dredge...: pick from a pile
        grid = state["card_grid"]
        v.update(title=f"pick from {(grid.get('pile') or 'pile').lower()}",
                 ranks=[("card", n, None, "") for n in pile_pick(state)], body=[f"{grid['min']}-{grid['max']} cards"])
    elif state.get("hand_select"):  # a card asked to pick hand cards: that comes before any plan
        sel = state["hand_select"]
        _, names = hand_pick(state)
        v.update(title=(sel["prompt"] or sel["mode"]).replace("TO_", "").lower(),
                 ranks=[("card", n, None, "") for n in names] or [(None, "nothing - confirm", None, "")],
                 body=[f"{sel['min']}-{sel['max']} cards"])
    else:
        plans = solve(state)
        why = held_energy_reason(state, plans[0])
        nh = next_hand(state, plans[0])  # user: the real next hand (reader's draw order) after this plan
        # user: red = still in the draw pile, blue = comes back from the discard (the exact reshuffle)
        hand = nh and [("next hand: ", None)] + [
            (title("cards", c["name"]) + ("+" if c.get("upgraded") else "") + (", " if i < len(nh[0]) - 1 else ""),
             DRAW_RED if i < nh[2] else DISCARD_BLUE) for i, c in enumerate(nh[0])] + (
            [(f" + {nh[1]} from the reshuffle", None)] if nh[1] else [])
        v.update(title="plan", steps=steps_view(state, plans[0]), outcome=plans[0], body=[x for x in (why, hand) if x],
                 next_moves=next_moves(state),
                 alts=[f"{', '.join(title('potions' if s[5] == 'potion' else 'cards', s[3]['name']) for s in pl.steps) or 'End turn'}  ·  {outcome_text(pl)}"
                       for pl in plans[1:]])
    return v


def draw(v, state):
    for f in (context, primary, secondary, notes):
        clear(f)
    v["stats"](context)
    if v.get("title") or v.get("head"):
        section(primary, v.get("title") or "advice")
    if v.get("head"):
        label(row(primary), v["head"], F_BIG, TEXT, anchor="w", justify="left",
              wraplength=WIDTH - 2 * PAD).pack(fill="x")
    if "route" in v:
        r = row(primary, (2, 1))
        route_row(r, *v["route"], state)
        for rooms, tail in v.get("alt_routes", []):
            route_row(row(primary, (1, 1)), rooms, tail, state, size=13, color=MUTED)
    if "steps" in v:
        step_rows(primary, v["steps"])
        outcome_chips(primary, v["outcome"])
        next_move_rows(primary, v.get("next_moves"))
    ranks = v.get("ranks", [])
    shown = max(3, v.get("show", 0))  # a pick-N screen shows all N
    rank_rows(primary, ranks[:shown] if not expanded else ranks)
    lines(primary, v.get("body", []))
    hidden = len(ranks) > shown and not expanded
    if expanded:
        if v.get("alts"):
            section(secondary, "alternatives")
            lines(secondary, v["alts"], MUTED, F_SMALL)
        extra = v.get("more", []) + kit(state)[1]
        if extra:
            section(secondary, "details")
            lines(secondary, extra, MUTED, F_SMALL)
        if state.get("potions"):
            r = row(secondary, (4, 2))
            label(r, "potions", F_SMALL, DIM).pack(side="left", padx=(0, 6))
            for pot in state["potions"]:
                icon(r, "potion", pot["name"], CARD["Potion"], 18).pack(side="left", padx=(0, 3))
            label(r, f"{len(state.get('relics', []))} relics", F_SMALL, DIM).pack(side="right")
    note_list = kit(state)[0]
    if note_list and expanded:
        section(notes, "notes")
        lines(notes, ["⚠ " + n for n in note_list], WARN, F_SMALL)
    elif not expanded and (note_list or hidden or v.get("alts") or v.get("more")):
        r = row(notes, (4, 2))
        if note_list:
            label(r, f"⚠ {len(note_list)} note{'s' * (len(note_list) > 1)}", F_SMALL, WARN).pack(side="left")
        label(r, "F7 more ▾", F_SMALL, DIM).pack(side="right")
    layout(v)


def layout(v):
    """Pack only the sections in use, in order (an empty Frame would still leave a gap)."""
    for w in (accent, banner, header, context, primary, secondary, notes, err, footer, bottom):
        w.pack_forget()
    accent.pack(fill="x")
    if worker["alert"]:
        banner.pack(fill="x")
    header.pack(fill="x", padx=PAD, pady=(6, 0))
    for f in (context, primary, secondary, notes):
        if f.winfo_children():
            f.pack(fill="x")
    if err["text"]:
        err.pack(fill="x", padx=PAD, pady=(2, 0))
    if expanded:
        footer.pack(fill="x", padx=PAD, pady=(4, 0))
    bottom.pack(fill="x")


shown = None
passing = False  # overlay currently click-through
RECORDINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
recorded = None


def record(state, line):
    """Append each distinct decision state of a fight (our turn) for tools/replay.py."""
    global recorded
    if isinstance(proc, Demo):
        return  # a replay isn't a new fight
    key = lambda st: (st.get("seed"), st.get("encounter"), st["turn"], st["energy"], st["player"]["hp"],
                      tuple(c["name"] for c in st["hand"]), tuple(e["creature"]["hp"] for e in st["enemies"]))
    path = os.path.join(RECORDINGS, time.strftime("%Y-%m-%d") + ".jsonl")
    if recorded is None and os.path.exists(path):  # after an advisor restart: don't record the same state twice
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 200000))
                recorded = key(json.loads(f.read().decode("utf-8", "ignore").strip().splitlines()[-1]))
        except (ValueError, IndexError, KeyError, OSError):
            pass
    if key(state) == recorded:
        return
    recorded = key(state)
    os.makedirs(RECORDINGS, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def tick():
    """Runs every 100 ms. Any error in rendering a state is shown and logged, and the loop keeps going
    (an exception here used to stop all later updates, leaving a stale screen)."""
    try:
        render()
    except Exception:
        shown_err["text"] = traceback.format_exc().strip().splitlines()[-1]
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay_errors.log"), "a") as f:
            f.write(time.strftime("%H:%M:%S ") + traceback.format_exc() + "\n")
    text = (shown_err["text"] or latest["err"])[:160]
    if text != err["text"]:
        err.config(text=text)
        layout({})
    root.after(100, tick)


shown_err = {"text": ""}
last = {"alert": None, "msg": None}


def render():
    global shown, passing, expanded
    if f8.is_set():
        f8.clear()
        on_f8()
    if f7.is_set():
        f7.clear()
        expanded = not expanded
        shown = None  # redraw at the new density
    running = bool(worker["thread"] and worker["thread"].is_alive())
    pill.config(text="F8 AUTO ON" if running else "F8 auto off", fg=BG if running else DIM,
                bg=GOOD if running else BG, highlightthickness=1, highlightbackground=GOOD if running else TRIM)
    more.config(text="F7 ▴" if expanded else "F7 ▾")
    if running != passing:  # click-through exactly while autopilot runs (Tk calls stay on this thread)
        click_through(running)
        passing = running
    if (worker["alert"], worker["msg"]) != (last["alert"], last["msg"]):
        last.update(alert=worker["alert"], msg=worker["msg"])
        shown = None  # banner / autopilot message changed
    line = latest["line"]
    if proc.poll() is not None:
        shown_err["text"] = f"reader exited: {latest['err'] or proc.returncode}"
    elif line and line != shown:
        shown = line
        shown_err["text"] = ""
        state = json.loads(line)
        draw(view_of(state, line), state)
        if worker["msg"]:
            r = row(notes, (2, 2))
            label(r, worker["msg"][:90], F_SMALL, TEXT if worker["alert"] else WARN, anchor="w").pack(fill="x")
            layout({})


def refresh_learned():
    """L1: re-learn from finished runs when the game wrote a newer run file than learned.json (background)."""
    import picker
    here = os.path.dirname(os.path.abspath(__file__))
    out, tool = os.path.join(here, "learned.json"), os.path.join(here, "..", "tools", "learn.py")
    try:
        runs = history_runs()
        if runs and (not os.path.exists(out) or max(map(os.path.getmtime, runs)) > os.path.getmtime(out)):
            subprocess.run([sys.executable, "-I", tool], capture_output=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with open(out, encoding="utf-8") as f:
                picker.LEARNED = json.load(f)
    except Exception:
        traceback.print_exc()  # learning is optional: the advisor works without it


def history_runs():
    import glob
    return glob.glob(os.path.join(os.environ.get("APPDATA", ""), "SlayTheSpire2", "steam", "*", "profile*", "saves",
                                  "history", "*.run"))


def close(restart=False):
    with open(POS_FILE, "w") as f:
        f.write("+%d+%d" % (root.winfo_x(), root.winfo_y()))
    watch["restart"] = restart
    proc.kill()
    root.destroy()


# Auto-update: when the advisor's source changes (edits made while you play), restart on the new code once the files
# have been quiet for UPDATE_QUIET seconds, they compile, and autopilot isn't running. run.bat rebuilds the reader and
# relaunches on exit code RESTART_CODE.
RESTART_CODE, UPDATE_QUIET = 3, 15


def sources():
    import glob
    here = os.path.dirname(os.path.abspath(__file__))
    files = glob.glob(os.path.join(here, "*.py")) + glob.glob(os.path.join(here, "..", "reader", "*.cs*"))
    return {f: os.path.getmtime(f) for f in files if not os.path.basename(f).startswith("test_")}


def check_update():
    now = sources()
    if now != watch["seen"]:
        watch["seen"], watch["changed"] = now, time.time()
    busy = worker["thread"] and worker["thread"].is_alive()
    if watch["changed"] and time.time() - watch["changed"] >= UPDATE_QUIET and not busy:
        import py_compile
        try:
            for f in now:
                if f.endswith(".py"):
                    py_compile.compile(f, doraise=True)
            return close(restart=True)
        except py_compile.PyCompileError as e:
            shown_err["text"] = "update not loaded (doesn't compile): " + str(e.msg).strip().splitlines()[-1]
            watch["changed"] = 0  # wait for the next edit
    root.after(2000, check_update)


watch = {"seen": sources(), "changed": 0, "restart": False}
threading.Thread(target=refresh_learned, daemon=True).start()
tick()
root.after(2000, check_update)
root.mainloop()
sys.exit(RESTART_CODE if watch["restart"] else 0)
