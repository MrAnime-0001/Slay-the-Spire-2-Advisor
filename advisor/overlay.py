"""Always-on-top advisor overlay. Spawns reader.exe, solves each new state, shows the best line.
Drag to move, right-click to quit. Run the game in windowed or borderless mode."""
import json
import os
import subprocess
import threading
import tkinter as tk

from solver import solve

READER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "reader", "bin", "Release", "net10.0", "Sts2Reader.exe")
latest = {"line": None, "err": ""}


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
main = tk.Label(root, font=("Consolas", 13, "bold"), fg="#fff", bg="#111", justify="left", anchor="w")
alts = tk.Label(root, font=("Consolas", 9), fg="#888", bg="#111", justify="left", anchor="w")
err = tk.Label(root, font=("Consolas", 8), fg="#c55", bg="#111", justify="left", anchor="w")
for w in (status, main, alts, err):
    w.pack(fill="x", padx=10, pady=2)


def start_drag(e):
    root._dx, root._dy = e.x_root - root.winfo_x(), e.y_root - root.winfo_y()


root.bind_all("<ButtonPress-1>", start_drag)
root.bind_all("<B1-Motion>", lambda e: root.geometry(f"+{e.x_root - root._dx}+{e.y_root - root._dy}"))
root.bind_all("<Button-3>", lambda e: (proc.kill(), root.destroy()))


def describe(plan):
    if not plan.steps:
        return "End turn"
    return "\n".join(f"{i}. {name}{' -> ' + tgt if tgt else ''}{'' if ok else '  (?)'}"
                     for i, (name, tgt, ok) in enumerate(plan.steps, 1))


def outcome(plan):
    return ("LETHAL  " if plan.win else "") + f"dmg {plan.damage}  block {plan.block}  hp loss {plan.hp_loss}"


shown = None


def tick():
    global shown
    line = latest["line"]
    if proc.poll() is not None:
        status.config(text=f"reader exited: {latest['err'] or proc.returncode}")
    elif line != shown and line:
        shown = line
        state = json.loads(line)
        if state["status"] != "combat":
            status.config(text={"waiting_for_game": "waiting for game", "no_run": "no run in progress"}
                          .get(state["status"], "not in combat"))
            main.config(text="")
            alts.config(text="")
        else:
            p = state["player"]
            status.config(text=f"HP {p['hp']}/{p['max_hp']}  block {p['block']}  "
                               f"energy {state['energy']}/{state['max_energy']}  turn {state['turn']}")
            plans = solve(state)
            main.config(text=f"{describe(plans[0])}\n{outcome(plans[0])}")
            alts.config(text="\n".join(f"alt: {', '.join(s[0] for s in pl.steps) or 'End turn'}  [{outcome(pl)}]"
                                       for pl in plans[1:]))
    err.config(text=latest["err"][:120])
    root.after(100, tick)


tick()
root.mainloop()
