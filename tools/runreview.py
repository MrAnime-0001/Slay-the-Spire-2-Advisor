"""Compare a finished run's card picks and upgrades (game run history) with the advisor's picker.
python runreview.py <run file> <player id>   (any folder)"""
import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "advisor"))
from relics import GAMEDATA
from icons import snake
from picker import pick, upgrade_gain, UPGRADES, upgraded
G = GAMEDATA
CARDS = {snake(n).upper(): n for n in G["cards"]}
RELICS = {snake(n).upper(): n for n in G["relics"]}
ACTS = {snake(n).upper(): n for n in G.get("acts", {})} if isinstance(G.get("acts"), dict) else {}
d = json.load(open(sys.argv[1], encoding="utf-8"))
pid = int(sys.argv[2])
me = next(p for p in d["players"] if p["id"] == pid)
cid = lambda s: CARDS.get(s.split(".", 1)[1])
def card(name, up=0):
    c = G["cards"][name]
    out = {"name": name, "upgraded": False, "type": c["type"], "target": c["target"], "cost": c["cost"], "x_cost": c.get("x_cost", False),
           "vars": {k: int(v) for k, v in c["vars"].items()}, "rarity": c["rarity"], "keywords": c.get("keywords") or []}
    return upgraded(out, UPGRADES[name]) if up and name in UPGRADES else out
# deck timeline: start from the final deck, walk backwards is messy; walk forwards from cards_gained/removed/upgrades
deck, ups, floor, char = [], {}, 0, me["character"].split(".")[1].title()
starters = [c for c in me["deck"] if c.get("floor_added_to_deck") == 1]
deck = [card(cid(c["id"])) for c in starters if cid(c["id"])]
# cards that left the deck before the end (removed/transformed) and were starters: added back from the history below
rel = [RELICS.get(r["id"].split(".", 1)[1]) for r in me["relics"]]
rel_floor = [(r.get("floor_added_to_deck", 1), RELICS.get(r["id"].split(".", 1)[1])) for r in me["relics"]]
lines, agree, total = [], 0, 0
for a, act in enumerate(d["map_point_history"]):
    for mp in act:
        floor += 1
        ps = next((p for p in mp["player_stats"] if p["player_id"] == pid), None)
        if not ps:
            continue
        relics = [{"name": n} for f, n in rel_floor if n and f <= floor]
        act_name = next((n for n in G.get("acts", {}) if snake(n).upper() == d["acts"][a].split(".", 1)[1]), None)
        state = {"status": "card_reward", "character": char, "ascension": d["ascension"], "act": a, "act_name": act_name,
                 "max_energy": 3, "energy": 3, "hp": ps["current_hp"], "max_hp": ps["max_hp"], "gold": ps["current_gold"],
                 "deck": deck, "relics": relics, "floor": floor}
        if "card_choices" in ps:
            offered = [cid(c["card"]["id"]) for c in ps["card_choices"]]
            took = next((cid(c["card"]["id"]) for c in ps["card_choices"] if c["was_picked"]), "Skip")
            if all(offered) and mp["map_point_type"] != "shop":
                best, rated = pick([card(n) for n in offered], deck, relics, state)
                total += 1; agree += best == took
                lines.append(f"F{floor:2} {mp['map_point_type']:9} took {took:18} advisor {best:18} "
                             + ", ".join(f"{n} {s}" for n, s, *_ in rated))
        for c in ps.get("cards_gained", []):
            n = cid(c["id"])
            if n: deck.append(card(n, c.get("current_upgrade_level", 0)))
        for c in ps.get("cards_removed", []) + [t.get("original_card", {}) for t in ps.get("cards_transformed", [])]:
            n = cid(c.get("id", "X.X"))
            k = next((i for i, x in enumerate(deck) if x["name"] == n), None)
            if k is not None:
                deck.pop(k); lines.append(f"F{floor:2} removed/transformed {n}")
        for t in ps.get("cards_transformed", []):
            n = cid(t.get("final_card", {}).get("id", "X.X"))
            if n: deck.append(card(n))
        for u in ps.get("upgraded_cards", []):
            n = CARDS.get(u.split(".", 1)[1]) if isinstance(u, str) else cid(u["id"])
            cands = [(upgrade_gain(c, deck, UPGRADES, relics, state), c["name"]) for c in deck if not c["upgraded"] and c["name"] in UPGRADES]
            best = max(cands, default=(0, "-"))
            mine = next((g for g, x in cands if x == n), None)
            lines.append(f"F{floor:2} upgrade  took {n:18} (gain {mine}) advisor {best[1]} (gain {best[0]})")
            k = next((i for i, x in enumerate(deck) if x["name"] == n and not x["upgraded"]), None)
            if k is not None: deck[k] = card(n, 1)
        if "rest_site_choices" in ps:
            lines.append(f"F{floor:2} rest     {ps['rest_site_choices']} hp {ps['current_hp']}/{ps['max_hp']}")
print("\n".join(lines)); print(f"card picks: advisor agreed {agree}/{total}")
print("final deck size", len(me["deck"]), "rebuilt", len(deck))
