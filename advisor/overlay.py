"""Always-on-top advisor overlay. Spawns reader.exe, solves each new state, shows the best line.
F8 toggles autopilot (plays the plan and ends the turn, every turn, until combat ends).
Drag to move, Ctrl+right-click to quit.
Run the game in windowed or borderless mode, and keep this window away from the enemies."""
import ctypes
import json
import os
import subprocess
import threading
import time
import tkinter as tk
import traceback
import winsound

import autoplay  # first: sets DPI awareness before any window exists
from pathing import DEATH, best_routes, direction
from archetypes import describe as describe_build
from deck import profile
from picker import choose_card, event_advice, hand_pick, pick, rest_advice, shop_advice
from relics import GAMEDATA, Relics
from solver import fight_type, solve, unmodeled_player_powers, unmodeled_powers

READER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "reader", "bin", "Release", "net10.0", "Sts2Reader.exe")
latest = {"line": None, "err": ""}
ROOM = {"Monster": "M", "Elite": "E", "Unknown": "?", "Shop": "$", "Treasure": "T", "RestSite": "R", "Ancient": "A"}


def pump(stream, key):
    for line in stream:
        latest[key] = line.strip()
        if key == "line":
            latest["err"] = ""  # a good read clears one-off read errors


proc = subprocess.Popen([READER], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
threading.Thread(target=pump, args=(proc.stdout, "line"), daemon=True).start()
threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True).start()

root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True, "-alpha", 0.85)
root.geometry("+40+40")
root.configure(bg="#111")
status = tk.Label(root, font=("Consolas", 9), fg="#aaa", bg="#111", justify="left", anchor="w")
kit = tk.Label(root, font=("Consolas", 8), fg="#7aa2c7", bg="#111", justify="left", anchor="w", wraplength=420)
main = tk.Label(root, font=("Consolas", 13, "bold"), fg="#fff", bg="#111", justify="left", anchor="w")
alts = tk.Label(root, font=("Consolas", 9), fg="#888", bg="#111", justify="left", anchor="w")
auto = tk.Label(root, font=("Consolas", 9), fg="#e5c07b", bg="#111", justify="left", anchor="w")
err = tk.Label(root, font=("Consolas", 8), fg="#c55", bg="#111", justify="left", anchor="w")
hint = tk.Label(root, font=("Consolas", 8), fg="#666", bg="#111", anchor="w",
                text="")
for w in (status, kit, main, alts, auto, err, hint):
    w.pack(fill="x", padx=10, pady=2)

# --- F8 auto-play: hotkey thread sets a flag; tick() starts or cancels the worker ---
f8 = threading.Event()
cancel = threading.Event()
worker = {"thread": None, "msg": "", "alert": False}
autoplay.listen_hotkey(f8.set)
# ponytail: runpilot.py (full-run autopilot) is parked: its out-of-combat clicks weren't reliable live.


def get_state():
    return json.loads(latest["line"]) if latest["line"] else {}


def play():
    def log(m):  # overlay line + autopilot_debug.log, so a stop can be diagnosed afterwards
        worker.update(msg="autopilot: " + m)
        with open(autoplay.DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {m}\n")
    result = autoplay.autopilot(get_state, lambda s: solve(s)[0], hand_pick, cancel, log,
                                choose=lambda g, c, l: autoplay.click_choice(g, c, l, lambda s: choose_card(s)[0]))
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


def describe(plan):
    if not plan.steps:
        return "End turn"
    return "\n".join(f"{i}. {name}{' -> ' + tgt if tgt else ''}{'' if ok else '  (?)'}"
                     for i, (name, tgt, ok, *_) in enumerate(plan.steps, 1))


def describe_kit(state):
    """potions + relics line; flags relics that change in-turn math but have no solver rule."""
    if "relics" not in state:
        return ""
    potions = ", ".join(p["name"] for p in state.get("potions", [])) or "none"
    unmodeled = Relics(state["relics"], GAMEDATA["relics"]).unmodeled()
    foes = unmodeled_powers(state)
    mine = unmodeled_player_powers(state) if state.get("status") == "combat" else []
    return (f"potions: {potions} · relics: {len(state['relics'])}"
            + (f" (not modeled: {', '.join(unmodeled)})" if unmodeled else "")
            + (f"\nenemy powers not modeled: {', '.join(foes)}" if foes else "")
            + (f"\nyour powers not modeled: {', '.join(mine)}" if mine else ""))


def deck_lines(state):
    """What the deck does per turn vs the act boss, and which build it is (for picks and the shop)."""
    build = describe_build(state.get("deck", []), state.get("relics", []))
    return [profile(state)] + ([f"build: {build}"] if build else [])


def outcome(plan):
    return ("LETHAL  " if plan.win else "") + f"dmg {plan.damage}  block {plan.block}  hp loss {plan.hp_loss}"


shown = None
passing = False  # overlay currently click-through
RECORDINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
recorded = None


def record(state, line):
    """Append each distinct decision state of a fight (our turn) for tools/replay.py."""
    global recorded
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
    err.config(text=(shown_err["text"] or latest["err"])[:120])
    root.after(100, tick)


shown_err = {"text": ""}


def render():
    global shown, passing
    if f8.is_set():
        f8.clear()
        on_f8()
    hint.config(text=f"F8: combat autopilot {'ON' if worker['thread'] and worker['thread'].is_alive() else 'off'}"
                     " · Ctrl+right-click: quit")
    running = bool(worker["thread"] and worker["thread"].is_alive())
    if running != passing:  # click-through exactly while autopilot runs (Tk calls stay on this thread)
        click_through(running)
        passing = running
    line = latest["line"]
    if proc.poll() is not None:
        status.config(text=f"reader exited: {latest['err'] or proc.returncode}")
    elif line != shown and line:
        shown = line
        shown_err["text"] = ""
        state = json.loads(line)
        kit.config(text=describe_kit(state))
        if state.get("choose_card"):  # Knowledge Demon curse, Attack Potion, Discovery...: click the pick
            best, rated = choose_card(state)
            status.config(text="choose a card" + ("  (skip allowed)" if state["choose_card"]["can_skip"] else ""))
            main.config(text=f"Choose: {best}")
            alts.config(text="\n".join(f"{name:<16} {label}" for name, _, label in rated))
        elif state.get("card_reward"):
            best, rated = pick(state["card_reward"], state["deck"], state.get("relics", []), state)
            status.config(text=f"card reward  (deck {len(state['deck'])})")
            main.config(text=f"Pick: {best}")
            alts.config(text="\n".join([f"{name:<16} {score:>5}{'' if ok else '  (?)'}" for name, score, ok in rated]
                                       + deck_lines(state)))
        elif (state.get("rest_site") or {}).get("chosen") is False:
            head, lines = rest_advice(state)
            status.config(text=f"rest site  (HP {state['player']['hp']}/{state['player']['max_hp']})")
            main.config(text=head)
            alts.config(text="\n".join(lines))
        elif state.get("event") and not state.get("map_open"):
            head, lines = event_advice(state)
            status.config(text=f"event  HP {state['player']['hp']}/{state['player']['max_hp']}  gold {state['gold']}")
            main.config(text=head)
            alts.config(text="\n".join(lines))
        elif state.get("shop") and not state.get("map_open"):
            head, lines = shop_advice(state)
            status.config(text=f"shop  gold {state['gold']}  (deck {len(state['deck'])})")
            main.config(text=head)
            alts.config(text="\n".join(lines + deck_lines(state)))
        elif state["status"] == "out_of_combat" and (routes := best_routes(state)):
            p, best = state["player"], routes[0]
            status.config(text=f"map  act {state['act'] + 1}  HP {p['hp']}/{p['max_hp']}  gold {state['gold']}")
            main.config(text=f"Next: {best.types[0]} ({direction(state, best.coords[0])})")
            alts.config(text="\n".join(
                [f"Route: {' '.join(ROOM.get(t, t) for t in best.types)}  · HP at boss ~{best.hp}"]
                + [f"alt: {r.types[0]} ({direction(state, r.coords[0])})  HP at boss ~{r.hp}"
                   + ("  DIES" if r.score == DEATH else f"  score {r.score - best.score:+.0f}") for r in routes[1:]]))
        elif state["status"] != "combat":
            status.config(text={"waiting_for_game": "waiting for game", "no_run": "no run in progress",
                                "game_over": "run over - you died"}
                          .get(state["status"], "not in combat"))
            main.config(text="")
            alts.config(text="")
        else:
            p = state["player"]
            if state.get("phase") == "Play":
                record(state, line)
            status.config(text=f"HP {p['hp']}/{p['max_hp']}  block {p['block']}  "
                               f"energy {state['energy']}/{state['max_energy']}  turn {state['turn']}  "
                               f"[{fight_type(state).lower()}]"
                               + (f"  stars {state['stars']}" if state.get("stars") else "")
                               + (f"  orbs {' '.join(o['name'].removesuffix('Orb') for o in state['orbs'])}"
                                  if state.get("orbs") else "")
                               + "".join(f"  {p['name']} {p['creature']['hp']}hp" for p in state.get("pets", [])))
            if state.get("hand_select"):  # a card asked to pick hand cards: that comes before any plan
                sel = state["hand_select"]
                _, names = hand_pick(state)
                main.config(text=f"Pick: {', '.join(names) or 'nothing - confirm'}")
                alts.config(text=f"({(sel['prompt'] or sel['mode']).replace('TO_', '').lower()}, "
                                 f"{sel['min']}-{sel['max']} cards)")
            else:
                plans = solve(state)
                main.config(text=f"{describe(plans[0])}\n{outcome(plans[0])}")
                alts.config(text="\n".join(f"alt: {', '.join(s[0] for s in pl.steps) or 'End turn'}  [{outcome(pl)}]"
                                           for pl in plans[1:]))
    auto.config(text=worker["msg"] + ("\n>>> AUTOPILOT STOPPED - TAKE OVER <<<" if worker["alert"] else ""),
                fg="#fff" if worker["alert"] else "#e5c07b", bg="#a11" if worker["alert"] else "#111",
                font=("Consolas", 11 if worker["alert"] else 9, "bold" if worker["alert"] else "normal"))


tick()
root.mainloop()
