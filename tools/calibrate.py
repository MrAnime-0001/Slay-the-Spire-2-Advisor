"""Learn real fight costs from your own run history (the game's .run files) into advisor/knowledge.json:
per encounter, how much HP you lost (mean, 90th percentile), turns taken, and deaths. Re-run any time:

    python tools/calibrate.py
"""
import glob
import json
import os
import statistics
from collections import defaultdict

HISTORY = os.path.expandvars(r"%APPDATA%\SlayTheSpire2\steam\*\*\saves\history\*.run")
KNOWLEDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "advisor", "knowledge.json")


def pascal(model_id):
    """ENCOUNTER.TERROR_EEL_ELITE -> TerrorEelElite (the class names gamedata uses)."""
    return "".join(w.capitalize() for w in model_id.split(".", 1)[1].split("_"))


def main():
    fights, deaths, runs = defaultdict(list), defaultdict(int), glob.glob(HISTORY)
    for path in runs:
        with open(path, encoding="utf-8") as f:
            run = json.load(f)
        for act in run["map_point_history"]:
            for point in act:
                taken = point["player_stats"][0].get("damage_taken", 0)
                for room in point.get("rooms", []):
                    if room.get("model_id", "").startswith("ENCOUNTER."):
                        fights[pascal(room["model_id"])].append((taken, room.get("turns_taken", 0)))
        killer = run.get("killed_by_encounter", "NONE.NONE")
        if killer != "NONE.NONE" and not run.get("was_abandoned"):
            deaths[pascal(killer)] += 1
    with open(KNOWLEDGE, encoding="utf-8") as f:
        knowledge = json.load(f)
    for name, rows in fights.items():
        dmg = sorted(t for t, _ in rows)
        knowledge["encounters"].setdefault(name, {})["dmg"] = {
            "n": len(rows), "mean": round(statistics.mean(dmg), 1), "p90": dmg[int(0.9 * (len(dmg) - 1))],
            "turns": round(statistics.mean(t for _, t in rows), 1), "deaths": deaths.get(name, 0),
            "src": "your run history"}
    with open(KNOWLEDGE, "w", encoding="utf-8") as f:
        json.dump(knowledge, f, indent=1, sort_keys=True)
    print(f"{len(runs)} runs, {len(fights)} encounters, {sum(deaths.values())} deaths -> {os.path.normpath(KNOWLEDGE)}")


if __name__ == "__main__":
    main()
