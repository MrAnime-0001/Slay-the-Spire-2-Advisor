"""Learn from finished runs: the game's own run history (every player's deck, yours and teammates'), per character.
    python -I tools/learn.py            -> advisor/learned.json (+ a short report)
For each card / relic: how often it was offered, taken, and in a final deck; the win rate of decks that ended with
it vs the character's base rate, shrunk toward 0 with few samples (SHRINK pseudo-runs). It's evidence, not truth:
strong decks pick more cards, so the picker caps it and the deck sim's dilution check still decides.
Run history files are the game's data (untrusted input): parsed as JSON only, results kept local.
"""
import glob
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUT = os.path.join(ROOT, "advisor", "learned.json")
SHRINK = 10  # pseudo-runs at the base win rate: 3 wins in 3 runs isn't proof


def history_files():
    base = os.path.join(os.environ.get("APPDATA", ""), "SlayTheSpire2", "steam", "*", "profile*", "saves", "history")
    return sorted(glob.glob(os.path.join(base, "*.run")))


def class_names():
    """Game ID tail (BALL_LIGHTNING) -> class name (BallLightning), over every gamedata build."""
    out = {"cards": {}, "relics": {}}
    for f in glob.glob(os.path.join(ROOT, "advisor", "gamedata", "*.json")):
        g = json.load(open(f, encoding="utf-8"))
        for kind in out:
            for n in g.get(kind, {}):
                out[kind][re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", n).upper()] = n
    return out


def learn(files=None):
    names = class_names()
    tail = lambda kind, gid: names[kind].get(gid.split(".", 1)[-1])
    stats, runs = {}, 0
    for f in files if files is not None else history_files():
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("was_abandoned"):
            continue
        runs += 1
        win = bool(d.get("win"))
        for p in d.get("players", []):
            ch = stats.setdefault(p.get("character", "?").split(".")[-1].title(),
                                  {"runs": 0, "wins": 0, "cards": {}, "relics": {}})
            ch["runs"] += 1
            ch["wins"] += win
            for kind, items in (("cards", p.get("deck", [])), ("relics", p.get("relics", []))):
                for n in {tail(kind, x.get("id", "")) for x in items} - {None}:
                    s = ch[kind].setdefault(n, {"decks": 0, "won": 0, "offered": 0, "taken": 0})
                    s["decks"] += 1
                    s["won"] += win
            for act in d.get("map_point_history", []):
                for mp in act:
                    for ps in mp.get("player_stats", []):
                        if ps.get("player_id") != p.get("id"):
                            continue
                        for c in ps.get("card_choices", []):
                            n = tail("cards", c.get("card", {}).get("id", ""))
                            if n:
                                s = ch["cards"].setdefault(n, {"decks": 0, "won": 0, "offered": 0, "taken": 0})
                                s["offered"] += 1
                                s["taken"] += bool(c.get("was_picked"))
    for ch in stats.values():
        base = ch["wins"] / max(ch["runs"], 1)
        ch["base"] = round(base, 3)
        for kind in ("cards", "relics"):
            for s in ch[kind].values():
                s["lift"] = round((s["won"] + SHRINK * base) / (s["decks"] + SHRINK) - base, 3)
    return {"runs": runs, "chars": stats}


if __name__ == "__main__":
    data = learn(sys.argv[1:] or None)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=0)
    print(f"{data['runs']} runs learned -> {OUT}")
    for name, ch in sorted(data["chars"].items()):
        top = sorted(ch["cards"].items(), key=lambda kv: -kv[1]["lift"])
        print(f"{name}: {ch['runs']} decks, win rate {ch['base']:.0%}; best cards "
              + ", ".join(f"{n} {s['lift']:+.0%}" for n, s in top[:4])
              + "; worst " + ", ".join(f"{n} {s['lift']:+.0%}" for n, s in top[-3:]))
