"""Always-on-top advisor overlay. Spawns reader.exe, solves each new state, shows the best line.
F8 toggles autopilot (plays the plan and ends the turn, every turn, until combat ends). F7 shows more / less.
Drag to move, Ctrl+right-click to quit.
Run the game in windowed or borderless mode, and keep this window away from the enemies.
STS2_DEMO=<recordings .jsonl> replays recorded states instead of reading the game (design checks)."""
import ctypes
import json
import os
import re
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import winsound

import autoplay  # first: sets DPI awareness before any window exists
import icons
from pathing import DEATH, best_routes, direction
from archetypes import describe as describe_build
from deck import profile
from picker import choose_card, deck_pick, event_advice, hand_pick, pick, pile_pick, relic_value, rest_advice, shop_advice
from relics import GAMEDATA, KNOWLEDGE, Relics
from solver import fight_type, next_turn_unblocked, solve, unmodeled_player_powers, unmodeled_powers

READER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "reader", "bin", "Release", "net10.0", "Sts2Reader.exe")
latest = {"line": None, "err": ""}
ROOM = {"Monster": "M", "Elite": "E", "Unknown": "?", "Shop": "$", "Treasure": "T", "RestSite": "R", "Ancient": "A"}

# --- look: toned to the game (warm dark panel, parchment text, bronze/gold trim), drawn with system fonts only.
# It is a third-party tool: the header always says "unofficial"; game icons come from your own install (icons.py).
BG, SURFACE, TRIM, TRIM_HI = "#16110d", "#211a14", "#8a6a3a", "#c9a45c"
TEXT, MUTED, DIM = "#efe3c8", "#a8977a", "#6b5d4a"
GOOD, WARN, BAD, HP_RED, HP_TRACK, STEEL = "#7fae5a", "#d9a441", "#b8392f", "#c0392b", "#3a1512", "#7da3c4"
ACCENT = {"ironclad": "#c0392b", "silent": "#5c9c4a", "defect": "#3f88c5", "necrobinder": "#a86fb5",
          "regent": "#e08a2e"}
CARD = {"Attack": "#b8392f", "Skill": "#4f7fa8", "Power": "#c9a45c", "Status": "#6b6b6b", "Curse": "#5e3a6e",
        "Potion": "#4fa88a"}
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
root.geometry(os.environ.get("STS2_POS", "+40+40"))
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
tk.Label(header, text="unofficial", font=F_SMALL, fg=DIM, bg=BG).pack(side="left", padx=(6, 0), pady=(3, 0))
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
root.bind_all("<Control-Button-3>", lambda e: (proc.kill(), root.destroy()))


# --- names and icons ---
def title(kind, name):
    """The game's English name (cards:DEFEND_NECROBINDER.title -> "Defend"), else the class name spaced out."""
    t = GAMEDATA.get("text", {}).get(f"{kind}:{icons.snake(name).upper()}.title")
    return t or re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


_photos = {}


def photo(kind, name, size=ICON):
    """Tk image for an icon, or None while it isn't available (the caller draws a stand-in)."""
    key = (kind, name, size)
    if key not in _photos:
        from PIL import ImageTk
        img = icons.get(kind, name)
        if img is None:
            return None  # maybe still downloading: ask again next draw
        _photos[key] = ImageTk.PhotoImage(icons.fit(img, size))
    return _photos[key]


def icon(parent, kind, name, color=TRIM, size=ICON, letter=None):
    """The icon, or a drawn stand-in (a bronze-rimmed disc with the first letter), so nothing shows as missing."""
    img = photo(kind, name, size)
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
    for t in texts:
        label(row(parent, 0), t, font, fg, anchor="w", justify="left", wraplength=WIDTH - 2 * PAD).pack(fill="x")


# --- views: each screen becomes a dict, draw() paints it ---
# view keys: chip (screen name), stats (callable(frame) drawing the context row), title, head (big line),
# steps (combat plan rows), outcome (Plan), ranks [(kind, name, value, label)], body [str], more [str], notes [str]
def steps_view(state, plan):
    out = []
    by_id = {f.get("combat_id"): f for f in state.get("enemies", [])}
    for i, (name, tgt, ok, c, cid, kind) in enumerate(plan.steps, 1):
        foe = by_id.get(cid) if cid is not None else None
        intent = ((foe or {}).get("intents") or [{}])[0].get("kind") or ""
        out.append({"n": i, "name": name, "card": c, "kind": kind, "target": tgt, "ok": ok,
                    "intent": next((k for k in ("attack", "debuff", "buff", "defend") if k in intent.lower()), None)})
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
    if plan.win:
        chip(r, "LETHAL", BG, GOOD).pack(side="left", padx=(0, 4))
    chip(r, f"DMG {num(plan.damage)}").pack(side="left", padx=(0, 4))
    chip(r, f"BLOCK {num(plan.block)}", STEEL).pack(side="left", padx=(0, 4))
    loss = round(plan.hp_loss)
    chip(r, f"HP {-loss:+d}" if loss else "HP ±0", BAD if loss > 0 else GOOD).pack(side="left")


def outcome_text(plan):
    return ("LETHAL  " if plan.win else "") + f"{num(plan.damage)} dmg · {num(plan.block)} block · {-round(plan.hp_loss):+d} HP"


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
        pets = [f"{pet['name']} {pet['creature']['hp']} HP" for pet in state.get("pets") or []]
        if state.get("stars") or state.get("orbs") or pets:
            r = row(f, (0, 1))
            if state.get("stars"):
                icon(r, "ui", "star", WARN, 14, "★").pack(side="left")
                label(r, f"{state['stars']}", F_SMALL, TEXT).pack(side="left", padx=(2, 10))
            for o in state.get("orbs") or []:  # channeled orbs, oldest first (evoked first)
                icon(r, "orb", o["name"], STEEL, 16, o["name"][:1]).pack(side="left", padx=(0, 2))
            if pets:
                label(r, "  ·  ".join(pets), F_SMALL, MUTED).pack(side="right")
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
    return out, extra


def boss_tip(state):
    """Spire-Forge boss tip for this fight: your character's, else the general one (tools/import_research.py)."""
    tips = KNOWLEDGE.get("boss_tips", {}).get(state.get("encounter")) if state.get("status") == "combat" else None
    return tips and (tips.get((state.get("character") or "").lower()) or tips.get("general"))


def deck_lines(state):
    """What the deck does per turn vs the act boss, and which build it is (for picks and the shop)."""
    build = describe_build(state.get("deck", []), state.get("relics", []))
    return [profile(state)] + ([f"build: {build}"] if build else [])


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
        return {"stats": run_stats(state, f"{verb} a card"), "title": verb,
                "ranks": [("card", n, None, "") for n in picks],
                "more": ["next: " + ", ".join(title("cards", n) for n in ranked if n not in picks)[:200]]}
    if state["status"] != "combat" and state.get("treasure"):  # chest: one relic per player, you vote for one
        ranked = sorted(state["treasure"], key=lambda r: -relic_value(r, state))
        return {"stats": run_stats(state, "treasure"), "title": "take",
                "ranks": [("relic", r, relic_value(r, state), "") for r in ranked]}
    if state.get("card_reward"):
        best, rated = pick(state["card_reward"], state["deck"], state.get("relics", []), state)
        rated = sorted(rated, key=lambda r: -r[1])
        ranks = [("card" if name in GAMEDATA.get("cards", {}) else None, name, score,
                  ("" if ok else "? ") + ("" if saved is None else f"{saved:+.1f} HP" + (" · dilutes" if saved < 0 else "")))
                 for name, score, ok, saved in rated]
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
    if state["status"] == "out_of_combat" and (routes := best_routes(state)):
        best = routes[0]
        return {"stats": run_stats(state, f"map · act {state['act'] + 1}"), "title": "next",
                "head": f"{title('', best.types[0])}  ({direction(state, best.coords[0])})",
                "route": (best.types, f"HP at boss ~{best.hp}"),
                "more": [f"alt: {r.types[0]} ({direction(state, r.coords[0])})  HP at boss ~{r.hp}"
                         + ("  DIES" if r.score == DEATH else f"  score {r.score - best.score:+.0f}") for r in routes[1:]]}
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
        v.update(title="plan", steps=steps_view(state, plans[0]), outcome=plans[0],
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
        types, tail = v["route"]
        for t in types:
            icon(r, "room", t, TRIM, 18, ROOM.get(t, t[:1])).pack(side="left", padx=(0, 2))
        label(r, tail, F_SMALL, MUTED).pack(side="right")
    if "steps" in v:
        step_rows(primary, v["steps"])
        outcome_chips(primary, v["outcome"])
    ranks = v.get("ranks", [])
    rank_rows(primary, ranks[:3] if not expanded else ranks)
    lines(primary, v.get("body", []))
    hidden = len(ranks) > 3 and not expanded
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
    key = (state.get("encounter"), state["turn"], state["energy"], state["player"]["hp"],
           tuple(c["name"] for c in state["hand"]), tuple(e["creature"]["hp"] for e in state["enemies"]))
    if key == recorded:
        return
    recorded = key
    os.makedirs(RECORDINGS, exist_ok=True)
    with open(os.path.join(RECORDINGS, time.strftime("%Y-%m-%d") + ".jsonl"), "a", encoding="utf-8") as f:
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


tick()
root.mainloop()
