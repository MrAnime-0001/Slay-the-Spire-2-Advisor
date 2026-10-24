"""SIM_BIAS calibration (pathing.deck_factor): median of deck.hp_lost / the recorded Elite+Boss fight cost over the
recorded decks (advisor/recordings). Rerun after any change to the deck sim; --old ignores per-enemy HP (8b)."""
import glob, json, os, statistics, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "advisor"))
import deck
import pathing

if "--old" in sys.argv:
    _f = deck.fight
    deck.fight = lambda opts, hp, incs, multi, ps, pd, foes=(): _f(opts, hp, incs, multi, ps, pd)
pathing.deck_factor = lambda state, costs: 1
seen, ratios = set(), []
for path in sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "advisor", "recordings", "*.jsonl"))):
    for line in open(path, encoding="utf-8"):
        s = json.loads(line)
        acts = deck.GAMEDATA["encounters"].get(s.get("encounter"), {}).get("acts") or []
        if not s.get("deck") or not acts:
            continue
        s = {**s, "act_name": s.get("act_name") or acts[0]}
        key = (s["act_name"], tuple(sorted(c["name"] + "+" * c.get("upgraded", False) for c in s["deck"])))
        if key in seen:
            continue
        seen.add(key)
        base = pathing.fight_costs(s)  # deck_factor off below: the recorded cost alone
        ref = (base["Elite"][0] + base["Boss"][0]) / 2
        ratios.append(deck.hp_lost(s["deck"], s) / ref)
print(f"{len(ratios)} decks, median sim / recorded = {statistics.median(ratios):.2f}")
