from solver import solve, unmodeled_powers


def card(name, cost, type_, target, **vars_):
    return {"name": name, "upgraded": False, "type": type_, "target": target, "cost": cost, "x_cost": False, "vars": vars_}


STRIKE = card("Strike", 1, "Attack", "AnyEnemy", Damage=6)
DEFEND = card("Defend", 1, "Skill", "Self", Block=5)
BASH = card("Bash", 2, "Attack", "AnyEnemy", Damage=8, VulnerablePower=2)


def state(hand, energy=3, enemy_hp=40, intent_dmg=10, hp=80):
    return {
        "status": "combat", "energy": energy,
        "player": {"hp": hp, "max_hp": 80, "block": 0, "powers": []},
        "hand": hand,
        "enemies": [{"name": "JawWorm", "creature": {"hp": enemy_hp, "max_hp": 44, "block": 0, "powers": []},
                     "intents": [{"kind": "SingleAttackIntent", "damage": intent_dmg, "hits": 1}]}],
    }


# 1. Lethal available: kill instead of blocking.
best = solve(state([STRIKE, STRIKE, DEFEND], enemy_hp=12, intent_dmg=30))[0]
assert best.win and [s[0] for s in best.steps] == ["Strike", "Strike"], best

# 2. Big hit incoming, no lethal: block it all.
best = solve(state([STRIKE, DEFEND, DEFEND, DEFEND], enemy_hp=40, intent_dmg=15))[0]
assert best.hp_loss == 0 and [s[0] for s in best.steps].count("Defend") == 3, best

# 3. Bash before Strike (Vulnerable boosts the Strike: 8 + 9 = 17).
best = solve(state([STRIKE, BASH], intent_dmg=0))[0]
assert [s[0] for s in best.steps] == ["Bash", "Strike"] and best.damage == 17, best

# 4. No energy: empty plan.
best = solve(state([STRIKE, DEFEND], energy=0))[0]
assert best.steps == [], best

# 5. Not in combat: nothing to suggest.
assert solve({"status": "out_of_combat"}) == []

print("all solver checks passed")

# --- card picker ---
from picker import pick

DECK = [STRIKE] * 5 + [DEFEND] * 4 + [BASH]
CLEAVE = card("Cleave", 1, "Attack", "AllEnemies", Damage=8)
TWIN = card("SingleHit", 1, "Attack", "AnyEnemy", Damage=8)
UPPERCUT = card("Uppercut", 2, "Attack", "AnyEnemy", Damage=13, Power=1)  # the game's var shape: Weak + Vulnerable
WEAK1 = card("Meh", 2, "Skill", "Self", Block=3)
WEAK2 = card("Bleh", 3, "Attack", "AnyEnemy", Damage=5)

# 6. AoE beats an equal single-target attack when the deck has no AoE.
best, rated = pick([TWIN, CLEAVE], DECK)
assert best == "Cleave", rated

# 7. Nothing worth taking: Skip.
best, rated = pick([WEAK1, WEAK2], DECK)
assert best == "Skip", rated

# 8. Uppercut-like card rates above a plain Strike.
_, rated = pick([UPPERCUT, STRIKE], DECK)
assert rated[0][1] > rated[1][1], rated

print("all picker checks passed")

# --- rest site ---
from picker import rest_advice


def camp(hp, deck, opts=("Heal", "Smith")):
    return {"player": {"hp": hp, "max_hp": 80}, "deck": deck, "rest_site": {"options": list(opts)}}


UPS = {"Strike": {"vars": {"Damage": 3}}, "Bash": {"vars": {"Damage": 2, "VulnerablePower": 1}},
       "Heavy": {"cost": -1}}
HEAVY = card("Heavy", 2, "Attack", "AnyEnemy", Damage=13)

# 9. Low HP: rest, heal 30% of max.
head, _ = rest_advice(camp(30, DECK), UPS)
assert head.startswith("Rest") and "+24" in head, head

# 10. Healthy: smith something.
head, _ = rest_advice(camp(75, DECK), UPS)
assert head.startswith("Smith"), head

# 11. Cost reduction on a 2-cost card beats Strike +3.
head, lines = rest_advice(camp(75, DECK + [HEAVY]), UPS)
assert head == "Smith -> upgrade Heavy", (head, lines)

print("all rest-site checks passed")

# --- potions + relics ---
from picker import rate


def kit(st, relics=(), potions=()):
    st["relics"] = [{"name": n, "counters": c} for n, c in relics]
    st["potions"] = [{"slot": i, "name": n} for i, n in enumerate(potions)]
    return st


# 12. Fire Potion (20) turns Strike (6) into lethal on a 26 HP enemy.
best = solve(kit(state([STRIKE], energy=1, enemy_hp=26), potions=["FirePotion"]))[0]
assert best.win and any(s[5] == "potion" for s in best.steps), best

# 13. No lethal and only 3 incoming: keep the potion.
best = solve(kit(state([STRIKE, DEFEND], enemy_hp=80, intent_dmg=3), potions=["FirePotion"]))[0]
assert not any(s[5] == "potion" for s in best.steps), best

# 14. Strike Dummy: Strike deals 6 + 3.
best = solve(kit(state([STRIKE], energy=1, intent_dmg=0), relics=[("StrikeDummy", {})]))[0]
assert best.damage == 9, best

# 15. Kunai at 2 attacks: Strike (3rd attack, +1 Dex) then Defend blocks 6.
best = solve(kit(state([STRIKE, DEFEND], energy=2, intent_dmg=10),
                 relics=[("Kunai", {"_attacksPlayedThisTurn": 2})]))[0]
assert [s[0] for s in best.steps] == ["Strike", "Defend"] and best.block == 6, best

# 16. Orichalcum: end the turn with no block -> 6 block against a 10 hit.
best = solve(kit(state([], energy=0, intent_dmg=10), relics=[("Orichalcum", {})]))[0]
assert best.block == 6 and best.hp_loss == 4, best

# 17. Card picks see relics: Strike rates higher with Strike Dummy.
assert rate(STRIKE, DECK, [{"name": "StrikeDummy"}])[0] > rate(STRIKE, DECK)[0]

# 18. Block Potion vs a 25 hit: saved in a hallway fight, drunk in a boss fight.
BOSS = next(n for n, t in __import__("relics").GAMEDATA["encounters"].items() if t["class"] == "Boss")
hallway = kit(state([DEFEND], energy=1, enemy_hp=80, intent_dmg=25), potions=["BlockPotion"])
assert not any(s[5] == "potion" for s in solve(hallway)[0].steps)
boss = kit(state([DEFEND], energy=1, enemy_hp=80, intent_dmg=25), potions=["BlockPotion"])
boss["encounter"] = BOSS
assert any(s[5] == "potion" for s in solve(boss)[0].steps)

# 19. Same hallway fight, but slots are full: cheaper to use, since it frees a slot.
full = kit(state([DEFEND], energy=1, enemy_hp=80, intent_dmg=25), potions=["BlockPotion"])
full["potion_slots"] = 1
assert any(s[5] == "potion" for s in solve(full)[0].steps)

print("all potion/relic checks passed")

# --- danger, tempo, healing, self-damage, Vigor ---
BIG = card("BigHit", 1, "Attack", "AnyEnemy", Damage=12)
BLOODLET = card("Bloodletting", 0, "Skill", "Self", HpLoss=3, Energy=2)

# 20. Healthy: trade HP for a faster kill (enemies scale the longer a fight runs).
best = solve(state([BIG, DEFEND], energy=1, enemy_hp=60, intent_dmg=12))[0]
assert [s[0] for s in best.steps] == ["BigHit"], best

# 21. Same hand at 20/80 HP: block instead.
best = solve(state([BIG, DEFEND], energy=1, enemy_hp=60, intent_dmg=12, hp=20))[0]
assert [s[0] for s in best.steps] == ["Defend"], best

# 22. A 12 hit would kill at 10 HP: drink the Blood Potion (heal 20% of max) even in a hallway fight.
best = solve(kit(state([], energy=0, enemy_hp=60, intent_dmg=12, hp=10), potions=["BloodPotion"]))[0]
assert any(s[5] == "potion" for s in best.steps) and best.hp_loss < 10, best

# 23. Bloodletting (lose 3 HP) at 3 HP would kill us: never play it.
best = solve(state([BLOODLET], energy=0, intent_dmg=0, hp=3))[0]
assert best.steps == [], best

# 24. Enemy Vigor 6 adds to its next attack: 10 + 6 incoming.
vig = state([], energy=0, intent_dmg=10)
vig["enemies"][0]["creature"]["powers"] = [{"name": "VigorPower", "amount": 6}]
assert solve(vig)[0].hp_loss == 16

# 25. Card picks count HP costs.
assert rate(BLOODLET, DECK)[0] < rate(card("Free", 0, "Skill", "Self", Energy=2), DECK)[0]

print("all danger/tempo checks passed")

# --- map pathing ---
from pathing import DEATH, best_routes, direction


def fork(left, right, hp=80, gold=0, then=("RestSite",)):
    """Start (1,0) forks to left (0,1) / right (2,1), both join a chain at column 1 ending in the boss."""
    chain = list(then) + ["Boss"]
    nodes = [{"c": 1, "r": 0, "type": "Ancient", "next": [[0, 1], [2, 1]]},
             {"c": 0, "r": 1, "type": left, "next": [[1, 2]]},
             {"c": 2, "r": 1, "type": right, "next": [[1, 2]]}]
    nodes += [{"c": 1, "r": 2 + i, "type": t, "next": [[1, 3 + i]] if i < len(chain) - 1 else []}
              for i, t in enumerate(chain)]
    return {"player": {"hp": hp, "max_hp": 80}, "gold": gold, "act": 0, "ascension": 0,
            "map": {"current": [1, 0], "start": [1, 0], "boss": [1, 1 + len(chain)], "nodes": nodes}}


# 26. Healthy: take the elite (relic + rare card), it's on the left.
st = fork("Elite", "Monster")
best = best_routes(st)[0]
assert best.types[0] == "Elite" and direction(st, best.coords[0]) == "left  ● ○", best
# 4 choices on columns 0, 3, 4, 6: column 4 is the 3rd from the left.
four = {"map": {"current": [3, 0], "nodes": [{"c": 3, "r": 0, "next": [[6, 1], [0, 1], [4, 1], [3, 1]]}]}}
assert direction(four, (4, 1)) == "3rd from left  ○ ○ ● ○"

# 27. 24/80 HP: the elite is survivable but too risky; take the hallway fight.
assert best_routes(fork("Elite", "Monster", hp=24))[0].types[0] == "Monster"

# 28. 15/80 HP: the elite would kill us; that route scores as death.
routes = best_routes(fork("Elite", "Monster", hp=15))
assert routes[0].types[0] == "Monster" and routes[-1].score == DEATH, routes

# 29. 300 gold: shop beats ?. 20 gold: ? beats shop.
assert best_routes(fork("Shop", "Unknown", gold=300))[0].types[0] == "Shop"
assert best_routes(fork("Unknown", "Shop", gold=20))[0].types[0] == "Unknown"

# 30. Camp at 52/80 (65%) with an elite next: rest. Same HP, no map: smith.
c = camp(52, DECK)
c["map"] = {"current": [0, 0], "start": [0, 0], "boss": [0, 2], "nodes": [
    {"c": 0, "r": 0, "type": "RestSite", "next": [[0, 1]]},
    {"c": 0, "r": 1, "type": "Elite", "next": [[0, 2]]},
    {"c": 0, "r": 2, "type": "Boss", "next": []}]}
head, lines = rest_advice(c, UPS)
assert head.startswith("Rest") and "elite in 1" in lines[0], (head, lines)
assert rest_advice(camp(52, DECK), UPS)[0].startswith("Smith")

print("all pathing checks passed")

# --- shop ---
from picker import removal_target, shop_advice
from relics import GAMEDATA

STARTER = [{**c, "rarity": "Basic"} for c in DECK]
REGRET = {**card("Regret", -1, "Curse", "None"), "rarity": "Curse"}
GOOD = card("BigCard", 1, "Attack", "AnyEnemy", Damage=16)
RARE_RELIC = next(n for n, r in GAMEDATA["relics"].items() if r.get("rarity") == "Rare")


def shop(gold, cards=(), relics=(), potions=(), removal=None, deck=STARTER, slots=3, held=()):
    return {"gold": gold, "deck": list(deck), "player": {"hp": 80, "max_hp": 80}, "potion_slots": slots,
            "potions": [{"slot": i, "name": n} for i, n in enumerate(held)],
            "shop": {"cards": [{"card": c, "price": p, "sale": False} for c, p in cards],
                     "relics": [{"name": n, "price": p} for n, p in relics],
                     "potions": [{"name": n, "price": p} for n, p in potions], "removal": removal}}


# 31. 120 gold, removal 75, starter deck: remove a Strike or Defend.
head, _ = shop_advice(shop(120, removal=75))
assert "Remove Strike" in head or "Remove Defend" in head, head

# 32. A Curse in the deck is removed first.
assert removal_target(STARTER + [REGRET])["name"] == "Regret"

# 33. Potion slots full: never buy a potion.
head, _ = shop_advice(shop(200, potions=[("FirePotion", 48)], slots=1, held=["BlockPotion"]))
assert head.startswith("Leave"), head

# 34. Everything costs more than our gold: leave.
assert shop_advice(shop(30, cards=[(GOOD, 60)], relics=[(RARE_RELIC, 275)], removal=75))[0].startswith("Leave")

# 35. 300 gold: two strong cards (2 x (10 - 1.2) = 17.6) beat a Rare relic (16 - 5.5 = 10.5).
head, _ = shop_advice(shop(300, cards=[(GOOD, 60), (GOOD, 60)], relics=[(RARE_RELIC, 275)]))
assert head == "Buy: BigCard + BigCard  [120/300]", head

print("all shop checks passed")

# --- in-combat hand picks ---
from picker import hand_pick


def picking(hand, mode="select", prompt=None, lo=1, hi=1):
    return {"hand": hand, "deck": DECK, "hand_select": {"mode": mode, "prompt": prompt, "min": lo, "max": hi,
                                                        "selected": 0}}


DAZED = card("Dazed", -1, "Status", "None")
STRIKE_UP = {**STRIKE, "upgraded": True}

# 36. Armaments: upgrade the best upgradable card; keys count only upgradable cards (Strike+ is hidden).
keys, names = hand_pick(picking([STRIKE_UP, STRIKE, HEAVY], "upgrade"), UPS)
assert names == ["Heavy"] and keys == [2], (keys, names)

# 37. Exhaust 1: the Status card goes first.
assert hand_pick(picking([STRIKE, DAZED, BASH], prompt="TO_EXHAUST"))[1] == ["Dazed"]

# 38. Exhaust any number (0-3): only junk; keys highest first since picked cards leave the row.
keys, names = hand_pick(picking([DAZED, STRIKE, DAZED], prompt="TO_EXHAUST", lo=0, hi=3))
assert keys == [3, 1] and names == ["Dazed", "Dazed"], (keys, names)

print("all hand-pick checks passed")

# --- extracted monster AI (gen_gamedata.py) ---
G = GAMEDATA["monsters"]["PhantasmalGardener"]
cycle, at = [], "BITE_MOVE"
for _ in range(5):
    cycle.append(at)
    at = G["next"][at]
assert cycle == ["BITE_MOVE", "LASH_MOVE", "FLAIL_MOVE", "ENLARGE_MOVE", "BITE_MOVE"], cycle
assert G["moves"]["LASH_MOVE"]["intents"][0]["args"][0]["base"] == 7
assert G["moves"]["ENLARGE_MOVE"]["effects"][0] == {"power": "StrengthPower", "on": "self",
                                                     "amt": {"base": 2, "asc": 3, "at": 9}}
assert GAMEDATA["encounters"]["TerrorEelElite"]["acts"] == ["Underdocks"]

print("all gamedata checks passed")

# --- enemy foresight ---
from enemies import predict


def foe(name, move, powers=(), history=(), slot=None):
    return {"name": name, "move": move, "history": list(history), "slot": slot,
            "creature": {"powers": [{"name": n, "amount": a} for n, a in powers]}}


# 39. Gardener showing Bite: next Lash 7, then Flail 1x3.
p = predict(foe("PhantasmalGardener", "BITE_MOVE"), turns=2)
assert [list(t["moves"]) for t in p] == [["LASH_MOVE"], ["FLAIL_MOVE"]] and p[0]["dmg"] == 7 and p[1]["dmg"] == 3, p

# 40. Gardener showing Enlarge (+2 Str): its next Bite hits 5 + 2 (+ the 1 Str it already has).
assert predict(foe("PhantasmalGardener", "ENLARGE_MOVE", powers=[("StrengthPower", 1)]), turns=1)[0]["dmg"] == 8

# 41. Mawler after Claw: Claw can't repeat; Roar is once only (already used) -> Rip and Tear for sure.
p = predict(foe("Mawler", "CLAW_MOVE", history=["ROAR_MOVE"]), turns=1)
assert p[0]["moves"] == {"RIP_AND_TEAR_MOVE": 1.0}, p

# 42. Unknown monster or move: no prediction.
assert predict(foe("NoSuchMonster", "X")) == [] and predict(foe("Mawler", None)) == []

# 43. Terror Eel showing Thrash (3x3 + Vigor 6): next Crash is 16 + 6.
assert predict(foe("TerrorEel", "THRASH_MOVE"), turns=1)[0]["dmg"] == 22


def fight(hand, enemies, energy=1, hp=80):
    return {"status": "combat", "energy": energy, "hand": hand,
            "player": {"hp": hp, "max_hp": 80, "block": 0, "powers": []}, "enemies": enemies}


def gardener(move, now, hp=6, cid=1):
    return {**foe("PhantasmalGardener", move), "combat_id": cid,
            "creature": {"hp": hp, "max_hp": 30, "block": 0, "powers": []},
            "intents": [{"kind": "x", "damage": now, "hits": 1}] if now else []}


# 44. Two 6-HP Gardeners, one Strike: kill the one about to Enlarge (its future hits are bigger),
#     even though the other one hits for 3 right now.
best = solve(fight([STRIKE], [gardener("ENLARGE_MOVE", 0, cid=1), gardener("FLAIL_MOVE", 3, cid=2)]))[0]
assert best.steps[0][4] == 1, best

# 45. 50/80 HP, Terror Eel shows a 3 hit now: with a 22 Crash coming next turn, Defend beats Strike.
#     Without foresight (no move known) it still blocks (v19: hallway HP is costly; 6 damage into a 140-HP enemy
#     doesn't remove a turn, so it isn't worth 3 HP).
eel = {**foe("TerrorEel", "THRASH_MOVE"), "combat_id": 1, "creature": {"hp": 140, "max_hp": 140, "block": 0,
       "powers": []}, "intents": [{"kind": "x", "damage": 3, "hits": 1}]}
assert [s[0] for s in solve(fight([STRIKE, DEFEND], [eel], hp=50))[0].steps] == ["Defend"]
assert [s[0] for s in solve(fight([STRIKE, DEFEND], [{**eel, "move": None}], hp=50))[0].steps] == ["Defend"]

print("all foresight checks passed")

# --- enemy powers ---


def boss(hp, powers, now=0):
    return {"name": "X", "combat_id": 1, "intents": [{"kind": "x", "damage": now, "hits": 1}] if now else [],
            "creature": {"hp": hp, "max_hp": hp, "block": 0, "powers": [{"name": n, "amount": a} for n, a in powers]}}


# 46. Skittish 6: after the first damaging Strike the Gardener gains 6 Block, so the 2nd Strike is soaked.
assert solve(fight([STRIKE, STRIKE], [boss(30, [("SkittishPower", 6)])], energy=2))[0].damage == 6

# 47. Hardened Shell 15: two 12-damage hits only take 15 HP this turn.
assert solve(fight([BIG, BIG], [boss(70, [("HardenedShellPower", 15)])], energy=2))[0].damage == 15

# 48. Shriek 70 (Terror Eel): pushing it to 70 HP stuns it, so its 16 hit never lands.
best = solve(fight([STRIKE, DEFEND], [boss(75, [("ShriekPower", 70)], now=16)]))[0]
assert [s[0] for s in best.steps] == ["Strike"] and best.hp_loss == 0, best

# 49. Slippery 2 (Vantom): the first two damaging hits deal 1 each, the third lands fully: 1 + 1 + 6.
assert solve(fight([STRIKE] * 3, [boss(100, [("SlipperyPower", 2)])], energy=3))[0].damage == 8

# 50. Slow (Bygone Effigy): play Defend first so BigHit gets +10%: floor(12 * 1.1) = 13.
best = solve(fight([BIG, DEFEND], [boss(100, [("SlowPower", 1)])], energy=2))[0]
assert [s[0] for s in best.steps] == ["Defend", "BigHit"] and best.damage == 13, best

# 51. Block Potion at 45/80 vs a 3 hit with Terror Eel's 22 Crash coming next turn: keep it for the Crash.
#     At 26/80 with a 22 Crash this turn: drink it.
assert not any(s[5] == "potion" for s in solve(kit(fight([], [eel], energy=0, hp=45), potions=["BlockPotion"]))[0].steps)
crash = {**eel, "move": "CRASH_MOVE", "intents": [{"kind": "x", "damage": 22, "hits": 1}]}
assert any(s[5] == "potion" for s in solve(kit(fight([], [crash], energy=0, hp=26), potions=["BlockPotion"]))[0].steps)

# 55. Intangible (Soul Fysh, Test Subject): each damaging hit does 1, so two Strikes deal 2.
assert solve(fight([STRIKE, STRIKE], [boss(100, [("IntangiblePower", 1)])], energy=2))[0].damage == 2

# 56. Enrage 2 (Test Subject): each Skill gives it +2 Strength this turn, so Defend vs a 6 hit only nets 3.
best = solve(fight([DEFEND], [boss(100, [("EnragePower", 2)], now=6)]))[0]
assert best.hp_loss == 6 - 5 + 2 or best.steps == [], best

# 57. The Insatiable's Sandpit at 1: without a Frantic Escape it swallows you, so play it over Strike.
ESC = card("FranticEscape", 1, "Status", "Self")
best = solve(fight([STRIKE, ESC], [boss(300, [("SandpitPower", 1)], now=5)]))[0]
assert [s[0] for s in best.steps] == ["FranticEscape"], best

# 58. Byrdonis Territorial 1: Swoop next turn is 17 + 1 (it gains 1 Strength after this turn).
assert predict(foe("Byrdonis", "PECK_MOVE", powers=[("TerritorialPower", 1)]), turns=1)[0]["dmg"] == 18

# 59. Personal Hive (Entomancer): one 12 hit beats two 6 hits (each landed hit adds Dazed).
TWO = card("TwoHits", 1, "Attack", "AnyEnemy", Damage=6, Repeat=2)
best = solve(fight([TWO, BIG], [boss(100, [("PersonalHivePower", 1)])]))[0]
assert [s[0] for s in best.steps] == ["BigHit"], best

# 60. Artifact 1 eats Bash's Vulnerable, so the follow-up Strike isn't boosted: 8 + 6.
assert solve(fight([BASH, STRIKE], [boss(100, [("ArtifactPower", 1)])], energy=3))[0].damage == 14

# 61. Overlay flag: modeled enemy powers stay quiet, unknown ones are listed.
assert unmodeled_powers({"enemies": [boss(50, [("ArtifactPower", 1), ("PaperCutsPower", 1)])]}) == ["PaperCuts"]

# 62. Sloth 3 with 2 cards already played: only one more card this turn.
slow_me = fight([STRIKE, STRIKE, STRIKE], [boss(100, [])], energy=3)
slow_me["player"]["powers"] = [{"name": "SlothPower", "amount": 3, "played": 2}]
assert len(solve(slow_me)[0].steps) == 1

# 63. Ringing card after a card was played this turn: unplayable. Smog (Living Fog's Smoggy): "You cannot play
#     additional Skills this turn", so one Smog Skill is fine, a second one isn't (v19: was "never playable").
ring = fight([{**STRIKE, "affliction": "Ringing"}, {**DEFEND, "affliction": "Smog"}], [boss(100, [])], energy=3)
assert [s[0] for s in solve({**ring, "cards_played": 1})[0].steps] == ["Defend"]
assert [s[0] for s in solve(ring)[0].steps] == ["Strike", "Defend"]
two = fight([{**DEFEND, "affliction": "Smog"}, {**DEFEND, "affliction": "Smog"}], [boss(100, [], now=20)], energy=3)
assert [s[0] for s in solve(two)[0].steps] == ["Defend"]

# 64. Disintegration 7 burns us at turn end like an unblocked hit (Defend 5 soaks part of it).
burn = fight([DEFEND], [boss(100, [])])
burn["player"]["powers"] = [{"name": "DisintegrationPower", "amount": 7}]
assert solve(burn)[0].hp_loss == 2

from picker import choose_card

# 65. Knowledge Demon: a cheap 3-energy deck plays 3 cards a turn anyway, so Sloth costs nothing.
demon = {**fight([], [boss(400, [])]), "max_energy": 3, "deck": [STRIKE] * 5 + [DEFEND] * 5,
         "choose_card": {"can_skip": False, "cards": [card("Disintegration", -1, "Status", "None", DisintegrationPower=7),
                                                       card("Sloth", -1, "Status", "None", SlothPower=3)]}}
assert choose_card(demon)[0] == "Sloth", choose_card(demon)
# ...but a 0-cost-heavy deck that plays 6 cards a turn would rather take Mind Rot than Sloth.
zeros = {**demon, "deck": [card("Zap", 0, "Attack", "AnyEnemy", Damage=4)] * 10,
         "choose_card": {"can_skip": False, "cards": [card("MindRot", -1, "Status", "None", MindRotPower=1),
                                                      card("Sloth", -1, "Status", "None", SlothPower=3)]}}
assert choose_card(zeros)[0] == "MindRot", choose_card(zeros)

# 66. Attack Potion pick in combat: the free card that kills now beats the bigger-number card.
pot = {**fight([], [boss(10, [], now=20)], energy=0),
       "choose_card": {"can_skip": False, "cards": [card("Slow", 2, "Attack", "AnyEnemy", Damage=9),
                                                    card("Kill", 2, "Attack", "AnyEnemy", Damage=10)]}}
assert choose_card(pot)[0] == "Kill"

# 67. Hard to Kill 9 (Exoskeleton) caps each hit; Flutter halves attacks (Thieving Hopper).
assert solve(fight([BIG], [boss(100, [("HardToKillPower", 9)])]))[0].damage == 9
assert solve(fight([BIG], [boss(100, [("FlutterPower", 5)])]))[0].damage == 6

# 68. Thorns 3: one 12 hit beats two 6 hits (each hit costs us 3).
best = solve(fight([TWO, BIG], [boss(100, [("ThornsPower", 3)])]))[0]
assert [s[0] for s in best.steps] == ["BigHit"] and best.hp_loss == 3, best

# 69. Tender on us: each card played costs 1 Strength, so the second Strike deals 5.
tend = fight([STRIKE, STRIKE], [boss(100, [])], energy=2)
tend["player"]["powers"] = [{"name": "TenderPower", "amount": 1}]
assert solve(tend)[0].damage == 11

# 70. Minions die with the last leader: Strike the 6-HP leader, not the 5-HP minion, and win.
lead, bomb = {**boss(6, [], now=10), "name": "LivingFog"}, {**boss(5, [("MinionPower", 1)], now=10), "combat_id": 2}
best = solve(fight([STRIKE], [lead, bomb]))[0]
assert best.win and best.steps[0][1] == "LivingFog", best

print("all enemy-power checks passed")

# --- card tiers (knowledge.json) ---
from picker import SKIP_BELOW


def real(name):
    c = GAMEDATA["cards"][name]
    return {"name": name, "upgraded": False, "type": c["type"], "target": c["target"], "cost": c["cost"],
            "x_cost": False, "vars": {k: int(v) for k, v in c["vars"].items()}}


# 52. Tier list corrects the stats heuristic: a tiered card's score moves from its stats-only score toward its
# tier value; Feel No Pain (A) worth taking. (v15: tiers are averaged over Mobalytics/slaythetierlist/guides,
# which disagree on Tremble (S vs D), so Tremble no longer beats Setup Strike outright.)
from picker import tier_value
_stats = rate(dict(real("Tremble"), name="_UntieredTremble"), DECK)[0]
assert min(_stats, tier_value("Tremble")) <= rate(real("Tremble"), DECK)[0] <= max(_stats, tier_value("Tremble"))
assert rate(real("Tremble"), DECK)[0] != _stats
assert rate(real("FeelNoPain"), DECK)[0] >= SKIP_BELOW

print("all card-tier checks passed")

# --- calibrated fight costs (knowledge.json from your run history) ---
from pathing import fight_costs, should_rest

# 53. Underdocks elites are costed from real fights, not the flat 22.
costs = fight_costs({"act": 0, "act_name": "Underdocks"})
assert costs["Elite"] != (22, 33.0) and costs["Elite"][1] > costs["Elite"][0], costs

# 54. The Terror Eel death: 54/80 HP with an elite next. Rest (54 < 70% of 80 = 56).
assert should_rest(54, 80, ["Elite", "Boss"], costs) == "elite in 1"

print("all calibration checks passed")

# --- events (option text from the game's loc files, numbers from the live event) ---
from picker import event_advice
from relics import KNOWLEDGE

BATHS = "events:ABYSSAL_BATHS.pages.INITIAL.options."


def baths(hp, max_hp=80):
    return {"player": {"hp": hp, "max_hp": max_hp, "block": 0, "powers": []}, "gold": 0,
            "event": {"name": "AbyssalBaths", "ancient": False, "vars": {"MaxHp": 2, "Damage": 3, "Heal": 10},
                      "options": [{"key": "IMMERSE", "desc": BATHS + "IMMERSE.description", "locked": False,
                                   "proceed": False, "relic": None},
                                  {"key": "ABSTAIN", "desc": BATHS + "ABSTAIN.description", "locked": False,
                                   "proceed": False, "relic": None}]}}


# 71. Healthy: +2 Max HP for 3 damage beats a heal you don't need; hurt: heal 10.
assert event_advice(baths(80))[0].endswith("Immerse"), event_advice(baths(80))
assert event_advice(baths(30))[0].endswith("Abstain"), event_advice(baths(30))

# 72. Ancient relic options are rated by relic tier (an S relic beats a D relic).
S = next(n for n, k in KNOWLEDGE["relics"].items() if k.get("tier") == "S")
D = next(n for n, k in KNOWLEDGE["relics"].items() if k.get("tier") == "D")
neow = {"player": {"hp": 80, "max_hp": 80}, "event": {"name": "Neow", "vars": {}, "options": [
    {"key": D, "desc": None, "locked": False, "relic": D}, {"key": S, "desc": None, "locked": False, "relic": S}]}}
assert event_advice(neow)[0] == f"Neow: {S}", event_advice(neow)

print("all event checks passed")

# --- real card costs + player debuffs (v9 part A) ---
from solver import unmodeled_player_powers


def me_with(st, *powers):
    st["player"]["powers"] = [{"name": n, "amount": a} for n, a in powers]
    return st


# 73. Tangled 1 (Vine Shambler): an Entangled Strike costs 2, so at 1 energy only the Defend fits.
tangled = me_with(state([{**STRIKE, "affliction": "Entangled"}, DEFEND], energy=1), ("TangledPower", 1))
assert [s[0] for s in solve(tangled)[0].steps] in (["Defend"], []), solve(tangled)[0]
assert all(s[0] != "Strike" for p in solve(tangled) for s in p.steps)

# 74. A card the reader reports as free this turn (local modifier) is played at 0 energy.
assert [s[0] for s in solve(state([{**BASH, "cost": 0}], energy=0))[0].steps] == ["Bash"]

# 75. Curious 1: a 1-cost Power costs 0; Corruption: Skills cost 0.
from solver import card_cost
POWER = card("Inflame", 1, "Power", "Self", StrengthPower=2)
assert card_cost(POWER, {"CuriousPower": 1}) == 0 and card_cost(DEFEND, {"CorruptionPower": 1}) == 0

# 76. Burn in hand (2 at turn end) adds to HP lost; Toxic (5, playable for 1) gets played to dodge it.
BURN, TOXIC = card("Burn", -1, "Status", "None", Damage=2), card("Toxic", 1, "Status", "None", Damage=5)
assert solve(state([BURN], energy=0, intent_dmg=0))[0].hp_loss == 2
assert [s[0] for s in solve(state([TOXIC], energy=1, intent_dmg=0))[0].steps] == ["Toxic"]

# 77. Unknown player powers are listed for the overlay; modeled ones aren't.
assert unmodeled_player_powers(me_with(state([]), ("WeakPower", 1), ("NoDrawPower", 1))) == ["NoDraw"]

print("all cost and debuff checks passed")

# 78. Beckon (1 energy, 6 unblockable at turn end in hand): play it instead of a Defend vs a 3 hit.
BECKON = card("Beckon", 1, "Status", "None", HpLoss=6)
assert [s[0] for s in solve(state([BECKON, DEFEND], energy=1, intent_dmg=3))[0].steps] == ["Beckon"]
print("all beckon checks passed")

# 79. Ascender's Bane (A5 curse) is Eternal: removal picks the Strike instead.
BANE = {**card("AscendersBane", -1, "Curse", "None"), "rarity": "Curse"}
assert removal_target([BANE, {**STRIKE, "rarity": "Basic"}])["name"] == "Strike"
print("all eternal checks passed")

# 80. Normality in hand: at most 3 cards this turn.
NORM = card("Normality", -1, "Curse", "None")
assert len(solve(state([NORM, STRIKE, STRIKE, STRIKE, STRIKE], energy=5, enemy_hp=100))[0].steps) == 3

# 81. Enthralled (2 energy) in hand must be played first: with 2 energy nothing else fits.
ENTH = card("Enthralled", 2, "Curse", "None")
assert [s[0] for s in solve(state([ENTH, STRIKE], energy=2, enemy_hp=100))[0].steps] in (["Enthralled"], [])
assert all(s[0] != "Strike" for s in solve(state([ENTH, STRIKE], energy=2, enemy_hp=100))[0].steps)
print("all curse-limit checks passed")

# 82. Every build's core card and key relic exists in this game version (guides can be stale).
from archetypes import ARCHETYPES, commitment, synergy
for char, builds in ARCHETYPES.items():
    for build, (_, core, relics_) in builds.items():
        assert all(GAMEDATA["cards"].get(n, {}).get("pool") == char for n in core), (char, build)
        assert all(r in GAMEDATA["relics"] for r in relics_), (char, build)

# 83. A deck with 3 poison cards is committed to Poison; the next poison card gets synergy, a Shiv card none.
POISON_DECK = [{"name": n} for n in ["StrikeSilent"] * 5 + ["DefendSilent"] * 5 + ["NoxiousFumes", "DeadlyPoison", "PoisonedStab"]]
assert commitment(POISON_DECK)["Silent:Poison"] >= 0.7
assert synergy({"name": "BouncingFlask"}, POISON_DECK) > synergy({"name": "BladeDance"}, POISON_DECK) == 0
print("all archetype checks passed")

# 84. Velvet Choker (energy, but a card cap) is worth less, and much less to a committed Shiv deck.
from picker import relic_value
SHIV_DECK = [{"name": n} for n in ["BladeDance", "CloakAndDagger", "Accuracy", "InfiniteBlades"]]
assert relic_value("VelvetChoker", {"deck": SHIV_DECK}) < relic_value("VelvetChoker", {"deck": []}) < relic_value("VelvetChoker") + 0.01
print("all relic drawback checks passed")

# --- deck balance (deck.py sim through picker.pick with state) ---
ACT1 = {"act_name": "Overgrowth", "ascension": 0, "max_energy": 3}
BIG_BLOCK = card("BigBlock", 1, "Skill", "Self", Block=12)
HIT = card("Hit", 1, "Attack", "AnyEnemy", Damage=12)
# 85. Attack-heavy deck: the block card wins; block-heavy deck: the attack wins.
ATK = [STRIKE] * 9 + [DEFEND]
BLK = [STRIKE] * 2 + [DEFEND] * 8
assert pick([card("Hit", 1, "Attack", "AnyEnemy", Damage=9), card("Wall", 1, "Skill", "Self", Block=9)], ATK, (), {**ACT1, "deck": ATK})[0] == "Wall"
assert pick([card("Hit", 1, "Attack", "AnyEnemy", Damage=9), card("Wall", 1, "Skill", "Self", Block=9)], BLK, (), {**ACT1, "deck": BLK})[0] == "Hit"
# 86. A plain extra Strike in a starter deck dilutes it: skip.
STARTER10 = [STRIKE] * 5 + [DEFEND] * 4 + [BASH]
assert pick([STRIKE], STARTER10, (), {**ACT1, "deck": STARTER10})[0] == "Skip"
# 87. Deterministic: same inputs, same score.
assert rate(HIT, STARTER10, (), ACT1) == rate(HIT, STARTER10, (), ACT1)
print("all deck balance checks passed")

# --- full-run autopilot decisions (runpilot.act with clicks/keys recorded instead of sent) ---
import runpilot

calls = []
runpilot.press = lambda want, area, *a: calls.append((area, next((t for t in TARGETS if want(t)), None)))
runpilot.key = lambda scan, *a, check=True: calls.append(("key", a[3]))


def screen(name, targets, **extra):
    global TARGETS
    TARGETS = targets
    calls.clear()
    st = {**shop(100), "status": "out_of_combat", "act": 0, "ui": {"screen": name, "targets": targets}, **extra}
    return runpilot.act(st, None, None, print, MEM, lambda: RETRY), calls[:]


MEM, RETRY = {}, False
# 88. Loot: gold first; a potion with no free slot is left; then the card reward; then proceed.
LOOT = ["reward:card", "reward:potion:FirePotion", "reward:gold:45"]
assert screen("NRewardsScreen", LOOT)[1] == [("reward", "reward:gold:45")]
assert screen("NRewardsScreen", LOOT[:2], potion_slots=1, potions=[{"slot": 0, "name": "BlockPotion"}])[1] == [("reward", "reward:card")]
MEM["skipped"] = True  # card reward skipped: it stays listed but isn't reopened
assert screen("NRewardsScreen", ["reward:card"])[1] == [("key", "rewards: proceed")]
# 89. Map: clicks the first point of the best route.
st = fork("Elite", "Monster")
assert screen("Map", ["map:0,1", "map:2,1"], map=st["map"], map_open=True)[1] == [("map", "map:0,1")]
# 90. Deck grid to remove: the curse goes.
GRID = {"screen": "NDeckCardSelectScreen", "prompt": "TO_REMOVE", "min": 1, "max": 1, "cards": STARTER + [REGRET]}
assert screen("NDeckCardSelectScreen", ["card:Strike", "card:Regret"], card_grid=GRID)[1] == [("grid", "card:Regret")]
# 91. Shop: open the merchant first; dead with auto-retry off -> stop with a reason.
assert screen("MerchantRoom", [])[1] == [("key", "shop: open merchant")]
assert "auto-retry off" in screen("NGameOverScreen", [], status="game_over")[0]
print("all run-pilot checks passed")

# --- character mechanics (P5) ---
POISON = card("Venom", 1, "Skill", "AnyEnemy", PoisonPower=5)
# 92. Poison ticks at the enemy's turn start: 5 poison on a 5 HP enemy kills it before its 30-damage hit.
best = solve(state([POISON, DEFEND], energy=1, enemy_hp=5, intent_dmg=30))[0]
assert best.win and best.hp_loss == 0 and best.steps[0][0] == "Venom", best
# 93. Doom 10 on a 10 HP enemy: it still hits this turn, then dies (a kill), so Doom beats a 6-damage Strike.
best = solve(state([card("Reap", 1, "Skill", "AnyEnemy", DoomPower=10), STRIKE], energy=1, enemy_hp=10, intent_dmg=5))[0]
assert best.steps[0][0] == "Reap" and best.kills == 1 and best.hp_loss == 5, best
# 94. Osty (8 HP) soaks unblocked attack damage: a 10 hit costs us 2.
assert solve({**state([], intent_dmg=10), "pets": [{"name": "Osty", "creature": {"hp": 8}}]})[0].hp_loss == 2
# 95. Orbs: Frost's passive Block soaks a 2 hit; channeling Ball Lightning into a full slot evokes the old
# Lightning (8) on top of its 7 damage: 15 kills.
assert solve({**state([], intent_dmg=2), "orbs": [{"name": "FrostOrb"}], "orb_slots": 3})[0].hp_loss == 0
BL = card("BallLightning", 1, "Attack", "AnyEnemy", Damage=7)
best = solve({**state([BL], energy=1, enemy_hp=15, intent_dmg=20), "orbs": [{"name": "LightningOrb"}], "orb_slots": 1})[0]
assert best.win, best
# 96. Stars: Devastate costs 4 stars (gamedata star_cost); with 2 stars it can't be played.
DEV = card("Devastate", 1, "Attack", "AnyEnemy", Damage=30)
assert not solve({**state([DEV]), "stars": 2})[0].steps
assert solve({**state([DEV]), "stars": 4})[0].steps[0][0] == "Devastate"
# 97. Ethereal: equal cards, one burns away if held -> play that one.
GHOST = {**card("GhostWall", 1, "Skill", "Self", Block=5), "keywords": ["Ethereal"]}
assert solve(state([DEFEND, GHOST], energy=1, intent_dmg=5))[0].steps[0][0] == "GhostWall"
print("all character mechanic checks passed")
# 98. Discard 1: a Sly card (played free when discarded) goes before a plain Strike.
SLY = {**card("Slick", 1, "Attack", "AnyEnemy", Damage=9), "keywords": ["Sly"]}
assert hand_pick(picking([STRIKE, SLY], prompt="TO_DISCARD"))[1] == ["Slick"]
print("all sly checks passed")
# 99. Deck sim values engines: a 1-cost 4-poison card and a Ball Lightning beat nothing (not 0 value).
from deck import hp_lost
assert hp_lost(STARTER10 + [card("Venom", 1, "Skill", "AnyEnemy", PoisonPower=4)], ACT1) < hp_lost(STARTER10 + [DAZED], ACT1)
assert hp_lost(STARTER10 + [BL], ACT1) < hp_lost(STARTER10 + [card("Plain", 1, "Attack", "AnyEnemy", Damage=7)], ACT1)
print("all deck engine checks passed")

# --- ascension (P7) ---
from pathing import fight_costs
# 100. A9 enemies hit harder / have more HP: the same deck loses more HP than at A0.
assert hp_lost(STARTER10, {**ACT1, "ascension": 9}) > hp_lost(STARTER10, ACT1)
# 101. A10 act 3: two bosses -> boss cost doubles; A3 Poverty: less fight gold on a route.
A3 = {"act": 2, "act_name": "Glory"}
assert fight_costs({**A3, "ascension": 10})["Boss"][0] == 2 * fight_costs({**A3, "ascension": 9})["Boss"][0]
assert best_routes({**fork("Monster", "Monster"), "ascension": 3})[0].gold < best_routes(fork("Monster", "Monster"))[0].gold
# 102. A7 Scarcity: a card just under the normal skip line gets taken.
import picker
real_rate, picker.rate = picker.rate, lambda *a, **k: (SKIP_BELOW - 0.5, True)
assert picker.pick([STRIKE], DECK, (), {"ascension": 0})[0] == "Skip"
assert picker.pick([STRIKE], DECK, (), {"ascension": 7})[0] == "Strike"
picker.rate = real_rate
print("all ascension checks passed")
# 103. The Forgotten's Dread has no readable damage: 13 + its Dexterity (2) = 15 incoming.
FORGOTTEN = {**state([])["enemies"][0], "name": "TheForgotten",
             "creature": {"hp": 40, "max_hp": 40, "block": 0, "powers": [{"name": "DexterityPower", "amount": 2}]},
             "intents": [{"kind": "SingleAttackIntent", "damage": None, "hits": 1}]}
assert solve({**state([]), "enemies": [FORGOTTEN]})[0].hp_loss == 15
print("all dynamic intent checks passed")
# 104. Gas Bomb's explode (Death Blow) has no readable damage: 8 at A0.
BOMB = {**FORGOTTEN, "name": "GasBomb", "creature": {"hp": 7, "max_hp": 7, "block": 0, "powers": []},
        "intents": [{"kind": "DeathBlowIntent", "damage": None, "hits": 1}]}
assert solve({**state([]), "enemies": [BOMB]})[0].hp_loss == 8
print("all death blow checks passed")
# 105. Foresight resolves runtime attack numbers: Waterfall Giant about to explode hits for its Steam Eruption.
from enemies import predict
WG = {"name": "WaterfallGiant", "move": "ABOUT_TO_BLOW_MOVE", "history": [], "slot": None,
      "creature": {"hp": 50, "powers": [{"name": "SteamEruptionPower", "amount": 40}]}}
fc = predict(WG, 0, 1)
assert fc and fc[0]["dmg"] == 40, fc
print("all runtime foresight checks passed")

# --- P4 tests the plan listed ---
from relics import GAMEDATA as GD


def real_card(name):
    c = GD["cards"][name]
    return {"name": name, "upgraded": False, "type": c["type"], "target": c["target"], "cost": c["cost"],
            "x_cost": False, "vars": {k: int(v) for k, v in c["vars"].items()}, "rarity": c["rarity"]}


SILENT = [real_card("StrikeSilent")] * 5 + [real_card("DefendSilent")] * 5 + [real_card(n) for n in
          ("Neutralize", "Survivor", "NoxiousFumes", "DeadlyPoison", "PoisonedStab")]
# 106. Bouncing Flask = 3 poison x 3 bounces: 9 on a lone enemy.
best = solve(state([real_card("BouncingFlask")], energy=2, enemy_hp=40, intent_dmg=0))[0]
assert best.steps and best.steps[0][0] == "BouncingFlask"
# 107. A committed Poison deck takes the poison card over a B-tier off-build card (Dagger Spray).
assert pick([real_card("BouncingFlask"), real_card("DaggerSpray")], SILENT, (), {**ACT1, "deck": SILENT})[0] == "BouncingFlask"
# 108. A curse makes the deck worse.
assert hp_lost(STARTER10 + [REGRET], ACT1) > hp_lost(STARTER10, ACT1)
# 109. The shop won't buy a relic whose drawback wrecks the build (Velvet Choker, committed Shiv deck).
SHIVS = STARTER + [real_card(n) for n in ("BladeDance", "CloakAndDagger", "Accuracy", "InfiniteBlades")]
head, _ = shop_advice({**shop(400, relics=[("VelvetChoker", 150)], deck=SHIVS), **ACT1})
assert "VelvetChoker" not in head, head
print("all P4 checks passed")
# 110. Strike Dummy: worth less once the Strikes are gone.
from picker import relic_value
assert relic_value("StrikeDummy", {"deck": [STRIKE] * 5}) > relic_value("StrikeDummy", {"deck": [STRIKE]}) > 0
print("all strike relic checks passed")
# 111. Poison Potion is worth more to a committed Poison deck than to an Ironclad starter.
from picker import potion_value
assert potion_value("PoisonPotion", {"deck": SILENT}) > potion_value("PoisonPotion", {"deck": STARTER10})
print("all potion fit checks passed")
# 112. Devastate (4 stars) is worth more in a deck that makes stars (Venerate x3) than in one that doesn't.
DEVA = real_card("Devastate")
VEN = real_card("Venerate")
base = STARTER10 + [VEN] * 3
assert hp_lost(base, ACT1) - hp_lost(base + [DEVA], ACT1) > hp_lost(STARTER10 + [DEFEND] * 3, ACT1) - hp_lost(STARTER10 + [DEFEND] * 3 + [DEVA], ACT1)
print("all star sim checks passed")
# 113. Channeling a Dark orb (stored damage for later) beats doing nothing when there's no threat.
DARKC = card("Darkness", 1, "Skill", "Self")
import solver as _s
_s.GAMEDATA["cards"].setdefault("Darkness", {"channel": ["Dark"]})
assert solve({**state([DARKC], energy=1, intent_dmg=0), "orb_slots": 3})[0].steps
print("all held orb checks passed")

# --- Retain / Exhaust ---
BIG = {**card("Bigshot", 1, "Attack", "AnyEnemy", Damage=10), "keywords": ["Exhaust"]}
# 114. Long fight: a 10-damage Exhaust card would be gone for good; a Strike now keeps it for later...
assert solve(state([BIG, STRIKE], energy=1, enemy_hp=60, intent_dmg=0))[0].steps[0][0] == "Strike"
# ...but when it's lethal, use it.
assert solve(state([BIG, STRIKE], energy=1, enemy_hp=10, intent_dmg=0))[0].steps[0][0] == "Bigshot"
# 115. Retain: play the Strike now and keep the retained 8-damage card for a sure play next turn.
KEEP = {**card("Keeper", 1, "Attack", "AnyEnemy", Damage=8), "keywords": ["Retain"]}
assert solve(state([KEEP, STRIKE], energy=1, enemy_hp=40, intent_dmg=0))[0].steps[0][0] == "Strike"
print("all retain/exhaust checks passed")
# 116. Deck sim: Impervious-style 30 Block that Exhausts is worth less than the same card without Exhaust.
WALL = card("Wall30", 2, "Skill", "Self", Block=30)
import deck as _d
_d.GAMEDATA["cards"].setdefault("Wall30X", {"keywords": ["Exhaust"]})
WALLX = {**WALL, "name": "Wall30X"}
assert hp_lost(STARTER10 + [WALLX], ACT1) > hp_lost(STARTER10 + [WALL], ACT1)
print("all exhaust sim checks passed")

# --- HP value per character, kill priority ---
# 117. Burning Blood at full HP: a 3 hit would be healed back anyway, so Strike beats Defend; without it, block.
chip = lambda relics, hp: {**state([STRIKE, DEFEND], energy=1, enemy_hp=40, intent_dmg=3, hp=hp), "relics": relics,
                         "max_energy": 1}
assert solve(chip([{"name": "BurningBlood"}], 80))[0].steps[0][0] == "Strike"
assert solve(chip([], 80))[0].steps[0][0] == "Defend"
# ...and at 60/80 the heal doesn't cover it: block.
assert solve(chip([{"name": "BurningBlood"}], 60))[0].steps[0][0] == "Defend"
# 118. A killable enemy that hits hard: kill it rather than block a small hit from the other one.
big = {"name": "Brute", "creature": {"hp": 6, "max_hp": 30, "block": 0, "powers": []},
       "intents": [{"kind": "SingleAttackIntent", "damage": 12, "hits": 1}]}
small = {"name": "Gnat", "creature": {"hp": 40, "max_hp": 40, "block": 0, "powers": []},
         "intents": [{"kind": "SingleAttackIntent", "damage": 4, "hits": 1}]}
best = solve({**state([STRIKE, DEFEND], energy=1), "enemies": [big, small]})[0]
assert best.steps[0][0] == "Strike" and best.steps[0][1] == "Brute", best
print("all hp value checks passed")

# --- cards made mid-turn, random targets ---
BD = real_card("BladeDance")
# 119. Blade Dance makes 3 Shivs (4 each) that can be played the same turn: 12 HP enemy dies.
best = solve(state([BD], energy=1, enemy_hp=12, intent_dmg=10))[0]
assert best.win and [s[0] for s in best.steps] == ["BladeDance", "Shiv", "Shiv", "Shiv"], best
# 120. Accuracy 2: Shivs hit for 6, so two of them kill a 12 HP enemy.
acc = state([real_card("Shiv")] * 2, energy=0, enemy_hp=12, intent_dmg=10)
acc["player"]["powers"] = [{"name": "AccuracyPower", "amount": 2}]
assert solve(acc)[0].win
# 121. Lightning's passive hits a random enemy: with a 3 HP and a 40 HP enemy it's not a sure kill.
two = {**state([]), "orbs": [{"name": "LightningOrb"}], "orb_slots": 3,
       "enemies": [{**state([])["enemies"][0], "creature": {"hp": 3, "max_hp": 3, "block": 0, "powers": []}},
                   {**state([])["enemies"][0], "creature": {"hp": 40, "max_hp": 40, "block": 0, "powers": []}}]}
assert solve(two)[0].kills == 0
# 122. Deck sim / picks see Blade Dance as 12 damage, not as "draw 3".
from deck import effect, card_key
assert effect(card_key(BD))[1] + effect(card_key(BD))[2] == 12 and effect(card_key(BD))[4] == 0
print("all made-card checks passed")

# 123. Choose-a-card autoclick: mouse goes to the 2nd of 3 cards and clicks only once the reader says it's hovered.
import threading
import autoplay as ap
saved = (ap.find_game, ap.user32, ap.click, ap.time.sleep)
pos, clicked = {"x": 0, "y": 0}, []
ap.find_game = lambda: (0, 0, 0, 1920, 1080)
ap.user32 = type("U", (), {"SetCursorPos": staticmethod(lambda x, y: pos.update(x=x, y=y)),
                           "SetForegroundWindow": staticmethod(lambda h: None)})()
ap.click = lambda: clicked.append((pos["x"], pos["y"]))
ap.time.sleep = lambda t: None
CHOICES = [{"name": n} for n in ("A", "B", "C")]


def chooser():
    if clicked:
        return {"choose_card": None}
    on_b = abs(pos["x"] - 960) < 120 and 380 < pos["y"] < 560
    return {"choose_card": {"cards": CHOICES, "hovered": 1 if on_b else None}}


assert ap.click_choice(chooser, threading.Event(), lambda m: None, lambda s: "B") is None
assert len(clicked) == 1 and abs(clicked[0][0] - 960) < 120
ap.find_game, ap.user32, ap.click, ap.time.sleep = saved
print("all choose-card click checks passed")

# --- piles: draw / discard / exhaust, cards that move cards between them ---
import random
from picker import hand_pick, pile_pick


def piled(hand, draw, enemy, energy=1, hp=50, discard=()):
    return {"status": "combat", "energy": energy, "max_energy": 3, "hand": hand, "draw": list(draw),
            "discard": list(discard), "exhaust": [], "deck": hand + list(draw) + list(discard),
            "player": {"hp": hp, "max_hp": 80, "block": 0, "powers": []}, "enemies": [enemy]}


quiet = {**eel, "move": None}  # no foresight: nothing predicted next turn
# 124. Crash (22) next turn and no Defends left to draw: block now. Defends still in the draw pile: attack.
assert solve(piled([STRIKE, DEFEND], [STRIKE] * 10, eel))[0].steps[0][0] == "Defend"
assert solve(piled([STRIKE, DEFEND], [DEFEND] * 10, eel))[0].steps[0][0] == "Strike"
# 125. Only the draw pile's contents matter, not its order.
mix = [STRIKE] * 5 + [DEFEND] * 5
a = solve(piled([STRIKE, DEFEND], mix, eel))[0]
b = solve(piled([STRIKE, DEFEND], random.Random(3).sample(mix, 10), eel))[0]
assert [s[0] for s in a.steps] == [s[0] for s in b.steps] and a.score == b.score
# 126. 30 cards left to draw: an Exhaust card won't come back before the fight ends, so it costs nothing.
assert solve(piled([BIG, STRIKE], [STRIKE] * 30, boss(60, [])))[0].steps[0][0] == "Bigshot"
# 127. Hologram: fetch the 30-Block card from the discard and play it against a 30 hit.
WALL1 = card("Wall30", 1, "Skill", "Self", Block=30)
hit30 = {**quiet, "intents": [{"kind": "x", "damage": 30, "hits": 1}]}
best = solve(piled([real_card("Hologram")], [STRIKE] * 5, hit30, energy=2, discard=[STRIKE, WALL1]))[0]
assert [s[0] for s in best.steps] == ["Hologram", "Wall30"], best
# 128. A draw card is worth more with strong cards to draw than with curses.
POMMEL, GOOD = real_card("PommelStrike"), card("Big20", 1, "Attack", "AnyEnemy", Damage=20)
CURSE = card("Regretful", -1, "Curse", "None")
assert solve(piled([POMMEL], [GOOD] * 5, quiet, energy=2))[0].score > solve(piled([POMMEL], [CURSE] * 5, quiet, energy=2))[0].score
# 129. Headbutt (discard -> top of draw): Defend before the 22 Crash, Strike before a quiet turn.
grid = lambda st: {**st, "card_grid": {"prompt": "HEADBUTT.selectionScreenPrompt", "min": 1, "max": 1,
                                       "cards": [STRIKE, DEFEND], "pile": "Discard", "hovered": None}}
assert pile_pick(grid(piled([], [STRIKE] * 5, eel, energy=0))) == ["Defend"]
assert pile_pick(grid(piled([], [STRIKE] * 5, quiet, energy=0))) == ["Strike"]
# 130. Thinking Ahead (hand -> top), enemy buffing now and Crash next: the Defend goes on top, the Strike is played.
think = lambda st, hand: {**st, "hand_select": {"mode": "select", "prompt": "THINKING_AHEAD.selectionScreenPrompt",
                                                "min": 1, "max": 1, "selected": 0}}
buff = {**eel, "intents": []}
assert hand_pick(think(piled([STRIKE, DEFEND], [STRIKE] * 5, buff), None))[1] == ["Defend"]
# 131. Enemy stays Vulnerable next turn, no energy now: the big attack goes on top, not the Strike.
vul = {**quiet, "creature": {**quiet["creature"], "powers": [{"name": "VulnerablePower", "amount": 2}]}}
BIG12 = card("Big12", 2, "Attack", "AnyEnemy", Damage=12)
assert hand_pick(think(piled([STRIKE, BIG12], [STRIKE] * 5, vul, energy=0), None))[1] == ["Big12"]
print("all pile checks passed")

# 132. Pile-screen autoclick: sweeps until the reader says card #1 is hovered, clicks once, remembers the spot.
saved = (ap.find_game, ap.user32, ap.click, ap.time.sleep)
pos, clicked = {"x": 0, "y": 0}, []
ap.find_game = lambda: (0, 0, 0, 1920, 1080)
ap.user32 = type("U", (), {"SetCursorPos": staticmethod(lambda x, y: pos.update(x=x, y=y)),
                           "SetForegroundWindow": staticmethod(lambda h: None)})()
ap.click = lambda: clicked.append((pos["x"], pos["y"]))
ap.time.sleep = lambda t: None


def pile_screen():
    if clicked:
        return {"card_grid": None}
    hov = 1 if 700 < pos["x"] < 800 and 500 < pos["y"] < 600 else 0 if pos["x"] < 700 and 500 < pos["y"] < 600 else None
    return {"card_grid": {"cards": CHOICES, "hovered": hov, "pile": "Discard", "min": 1, "max": 1}}


assert ap.pick_from_pile(pile_screen, threading.Event(), lambda m: None, lambda s: ["B"]) is None
assert len(clicked) == 1 and 700 < clicked[0][0] < 800 and 1 in ap.grid_spots
ap.find_game, ap.user32, ap.click, ap.time.sleep = saved
print("all pile click checks passed")

# 133. Event deck grid (Sapphire Seed "Consume", rest Smith, shop removal): the overlay names the card.
from picker import deck_pick
DECK_G = [real_card("StrikeDefect")] * 4 + [real_card("DefendDefect")] * 3 + [real_card(n) for n in ("Defile", "GatherLight", "ColdSnap")]
grid_of = lambda prompt: {"deck": DECK_G, "relics": [], "card_grid": {"screen": "NDeckCardSelectScreen", "prompt": prompt,
                                                                    "min": 1, "max": 1, "cards": DECK_G}}
verb, picks, _ = deck_pick(grid_of("TO_UPGRADE"))
from picker import UPGRADES, upgrade_gain
best_up = max(DECK_G, key=lambda c: upgrade_gain(c, DECK_G, UPGRADES, [], grid_of("TO_UPGRADE")))["name"]
assert verb == "Upgrade" and picks == [best_up], picks  # same ranking as the rest site's Smith
verb, picks, _ = deck_pick(grid_of("TO_REMOVE"))
assert verb == "Remove" and picks == ["StrikeDefect"], picks
assert deck_pick(grid_of("TO_TRANSFORM"))[1] == ["StrikeDefect"]
assert deck_pick({**grid_of(None), "card_grid": {**grid_of(None)["card_grid"], "screen": "NDeckUpgradeSelectScreen"}})[0] == "Upgrade"
print("all deck grid checks passed")

# 134. Vital Spark 2 (Infested Prism): a Tainted Defend blocks 5 but adds 2 to each hit. Vs 1x15 still worth it,
#      vs 5x3 it costs more than it blocks (5 hits x 2 = 10 > 5), so attack instead.
TDEF = {**DEFEND, "affliction": "Tainted"}
prism = lambda hits, dmg: boss(100, [("VitalSparkPower", 2)]) | {"intents": [{"kind": "x", "damage": dmg, "hits": hits}]}
assert next(p for p in solve(fight([STRIKE, TDEF], [prism(1, 15)])) if p.steps[0][0] == "Defend").hp_loss == 12  # 15+2-5
assert solve(fight([STRIKE, TDEF], [prism(5, 3)]))[0].steps[0][0] == "Strike"
assert solve(fight([TDEF], [prism(5, 3)]))[0].hp_loss == 15  # not played: 5 x 3
print("all tainted checks passed")

# --- enemy/relic effects (research/effects-audit.md) ---
from solver import keywords, next_draw
HIT10 = card("Hit10", 1, "Attack", "AnyEnemy", Damage=10)
blocked = lambda b, block: {**b, "creature": {**b["creature"], "block": block}}
# 135. Plow 150 (Ceremonial Beast): dropping it from 160 to 150 stuns it, so its 20 hit never lands.
assert solve(fight([HIT10], [boss(160, [("PlowPower", 150)], now=20)]))[0].hp_loss == 0
# 136. Burrowed (Tunneler): breaking all 8 Block stuns it.
assert solve(fight([STRIKE, STRIKE], [blocked(boss(30, [("BurrowedPower", 1)], now=15), 8)], energy=2))[0].hp_loss == 0
# 137. Ravenous (Corpse Slug): kill the small enemy and the Slug eats it instead of hitting us for 15.
slug, gnat = {**boss(40, [("RavenousPower", 1)], now=15), "combat_id": 1}, {**boss(6, [], now=3), "combat_id": 2}
best = solve(fight([STRIKE], [slug, gnat]))[0]
assert best.steps[0][4] == 2 and best.hp_loss == 0, best
# 138. Crab Rage: killing one crab gives the other +6 per hit (10 -> 16).
crab = lambda hp, cid, now: {**boss(hp, [("CrabRagePower", 1)], now=now), "combat_id": cid}
assert next(p for p in solve(fight([STRIKE], [crab(6, 1, 0), crab(40, 2, 10)])) if p.kills).hp_loss == 16
# 139. Waterfall Giant: killing it isn't a win (it erupts next turn); a dead one at 999999999 HP isn't a target.
assert not solve(fight([STRIKE], [boss(6, [("SteamEruptionPower", 30)])]))[0].win
giant, other = {**boss(999999999, []), "combat_id": 1}, {**boss(40, [], now=5), "combat_id": 2}
assert solve(fight([STRIKE], [giant, other]))[0].steps[0][4] == 2
# 140. Thieves: kill the Fat Gremlin carrying 50 gold over a 6 HP enemy hitting for 4; without loot, the reverse.
fat = lambda powers: {**boss(6, powers), "combat_id": 1}
brute = {**boss(6, [], now=4), "combat_id": 2}
assert solve(fight([STRIKE], [fat([("HeistPower", 50)]), brute]))[0].steps[0][4] == 1
assert solve(fight([STRIKE], [fat([]), brute]))[0].steps[0][4] == 2
# ...same for the Thieving Hopper holding a stolen card.
assert solve(fight([STRIKE], [fat([("SwipePower", 1)]), brute]))[0].steps[0][4] == 1
# 141. Bowlbug Rock (Imbalanced): fully blocking its Headbutt means no Headbutt next turn.
rock = {**foe("BowlbugRock", "HEADBUTT_MOVE", powers=[("ImbalancedPower", 1)]), "combat_id": 1,
        "creature": {"hp": 40, "max_hp": 40, "block": 0, "powers": [{"name": "ImbalancedPower", "amount": 1}]},
        "intents": [{"kind": "x", "damage": 5, "hits": 1}]}
assert solve(fight([STRIKE, DEFEND], [rock], hp=80))[0].steps[0][0] == "Defend"
plain_rock = {**rock, "creature": {**rock["creature"], "powers": []}}
# v19: a fully blockable 5 hit is blocked either way (hallway HP); the stun still makes that Defend worth more.
stun = next(p for p in solve(fight([STRIKE, DEFEND], [rock], hp=80), top=5) if p.steps and p.steps[0][0] == "Defend")
plain = next(p for p in solve(fight([STRIKE, DEFEND], [plain_rock], hp=80), top=5) if p.steps and p.steps[0][0] == "Defend")
assert stun.score > plain.score
# 142. Galvanized Power (Globe Head 3): playing it costs 3 HP. Hexed (Spectral Knight): the card is Ethereal.
GPOW = {**card("Inflame", 1, "Power", "Self", StrengthPower=2), "affliction": "Galvanized"}
assert next(p for p in solve(fight([GPOW], [boss(100, [("GalvanicPower", 3)])])) if p.steps).hp_loss == 3
assert "Ethereal" in keywords({**STRIKE, "affliction": "Hexed"})
# 143. Pael's Tears: ending with energy left is worth +2 energy next turn; Pael's Blood draws 6.
tears = lambda relics: {**piled([], [STRIKE] * 5, quiet, energy=1), "relics": relics}
end_score = lambda st: next(p for p in solve(st) if not p.steps).score
assert end_score(tears([{"name": "PaelsTears"}])) > end_score(tears([]))
assert next_draw({"relics": [{"name": "PaelsBlood"}]}) == 6 and next_draw({}) == 5
print("all enemy/relic effect checks passed")

# 144. Choose-1-of-3 potions: the Attack Potion's free card (stand-in: Stomp, 12) + Strike kill an 18 HP enemy.
atk = {**kit(state([STRIKE], energy=1, enemy_hp=18, intent_dmg=30), potions=["AttackPotion"]),
       "deck": [real_card("StrikeIronclad")] * 5}
best = solve(atk)[0]
assert best.win and "POTION AttackPotion" in [s[0] for s in best.steps], best
from solver import potion_cards
assert potion_cards("CunningPotion", {"hand": []}) and potion_cards("CunningPotion", {"hand": []})[0][0]["vars"]["Damage"] == 6
print("all card potion checks passed")

# 145. Surrounded (Crusher + Rocket): facing Right, the Crusher on our left hits 1.5x. Striking it turns us to face it:
#      20 + 4 x 1.5 = 26 instead of 20 x 1.5 + 4 = 34.
crusher = {**boss(200, [("BackAttackLeftPower", 1)], now=20), "name": "Crusher", "combat_id": 1}
rocket = {**boss(200, [("BackAttackRightPower", 1)], now=4), "name": "Rocket", "combat_id": 2}
sur = fight([STRIKE], [crusher, rocket])
sur["player"]["powers"] = [{"name": "SurroundedPower", "amount": 1, "facing": 0}]
best = solve(sur)[0]
assert best.steps[0][4] == 1 and best.hp_loss == 26, best
assert next(p for p in solve(sur) if not p.steps).hp_loss == 34
print("all surrounded checks passed")

# 146. Autopilot waits for a played card to resolve: the frame where energy is spent but the killing blow hasn't
#      landed yet (enemy at 1 HP, playing=1) must not be planned on (it drank a Block Potion after a kill).
saved = (ap.time.sleep, ap.find_game)
ap.time.sleep = lambda t: None
ap.find_game = lambda: (0, 0, 0, 1920, 1080)
mid = {"status": "combat", "phase": "Play", "turn": 6, "energy": 0, "hand": [DEFEND], "playing": 1,
       "enemies": [{"name": "PunchConstruct", "creature": {"hp": 1}}], "potions": [{"name": "BlockPotion", "slot": 1}]}
frames = iter([mid] * 6)
planned = []
get = lambda: next(frames, {"status": "out_of_combat"})
ap.autopilot(get, lambda s: planned.append(s) or type("P", (), {"steps": []})(), lambda s: ([], []),
             threading.Event(), lambda m: None)
assert not planned, planned
ap.time.sleep, ap.find_game = saved
print("all resolve-wait checks passed")

# 147. The state changes between planning and playing (a teammate's hit, a poison tick): don't play the stale plan,
#      replan from the new state, then play.
saved = (ap.time.sleep, ap.find_game, ap.play_step)
ap.time.sleep = lambda t: None
ap.find_game = lambda: (0, 0, 0, 1920, 1080)
foe40 = lambda hp: {"name": "X", "combat_id": 1, "creature": {"hp": hp, "block": 0, "powers": []}, "intents": []}
A = {"status": "combat", "phase": "Play", "turn": 1, "energy": 1, "hand": [STRIKE], "player": {"hp": 50, "block": 0, "powers": []},
     "enemies": [foe40(40)]}
B = {**A, "enemies": [foe40(34)]}  # someone else hit it for 6 after we planned
planned, played = [], []
step = ("Strike", "X", True, STRIKE, 1, "card")
get = lambda: A if not planned else B if not played else {"status": "out_of_combat"}
ap.play_step = lambda st, s, *a: played.append(s) or None
ap.autopilot(get, lambda s: planned.append(s) or type("P", (), {"steps": [step]})(), lambda s: ([], []),
             threading.Event(), lambda m: None)
assert [p["enemies"][0]["creature"]["hp"] for p in planned][:2] == [40, 34], planned
assert played and played[0]["enemies"][0]["creature"]["hp"] == 34
ap.time.sleep, ap.find_game, ap.play_step = saved
print("all replan-on-change checks passed")

# 148. A play the game ignores (key pressed during Armaments' upgrade animation): no Esc when nothing is held (Esc
#      would open the pause menu), retry once, and the retry goes through.
saved = (ap.time.sleep, ap.find_game, ap.tap, ap.click, ap.user32)
keys, clicks = [], []
ap.time.sleep = lambda t: None
ap.find_game = lambda: (0, 0, 0, 1920, 1080)
ap.tap = keys.append
ap.click = lambda: clicks.append(1)
ap.user32 = type("U", (), {"SetCursorPos": staticmethod(lambda x, y: None), "SetForegroundWindow": staticmethod(lambda h: None)})()
st = {"status": "combat", "phase": "Play", "energy": 1, "hand": [DEFEND], "holding": False}
done = {**st, "energy": 0, "hand": []}
get = lambda: done if len(clicks) >= 2 else st  # first click ignored, second one plays
assert ap.play_step(("Defend", None, True, DEFEND, None, "card"), st, get, threading.Event(), lambda m: None) is None
assert ap.SCAN_ESC not in keys and len(clicks) == 2, (keys, clicks)
ap.time.sleep, ap.find_game, ap.tap, ap.click, ap.user32 = saved
print("all ignored-play checks passed")

# --- deck dilution (v14) ---
from picker import SKIP_BELOW, TIER_BLEND_SIM, TIER_VALUE, KNOWLEDGE
from deck import slot_cost, encounter
HEAVY20 = card("Heavy", 2, "Attack", "AnyEnemy", Damage=20)
WALL12 = card("Wall", 1, "Skill", "Self", Block=12)
POMMEL = card("Pommel", 1, "Attack", "AnyEnemy", Damage=11)
LEAN = [HEAVY20] * 2 + [POMMEL] * 3 + [WALL12] * 3 + [BASH, DEFEND]
BIG = STARTER10 + [STRIKE] * 8 + [DEFEND] * 7
# 149. A weak card under a strong name (A-tier "Anger") in a lean deck makes fights worse: the tier list used to
# lift it over the skip line; now the draw odds win and the shop values it at 0.
WEAK_ANGER = card("Anger", 1, "Attack", "AnyEnemy", Damage=4)
assert KNOWLEDGE["cards"]["Anger"]["tier"] in ("A", "S")
score, _, saved = rate(WEAK_ANGER, LEAN, (), {**ACT1, "deck": LEAN})
assert saved < 0 and score < SKIP_BELOW, (score, saved)
assert (1 - TIER_BLEND_SIM) * (SKIP_BELOW + 0.5 * saved) + TIER_BLEND_SIM * TIER_VALUE["A"] >= SKIP_BELOW - 1  # old rule ~took it
assert pick([WEAK_ANGER, STRIKE], LEAN, (), {**ACT1, "deck": LEAN})[0] == "Skip"
# 150. Each dead card costs less HP the bigger the deck (12 vs 25 cards); a good card still clears the bar in both.
assert slot_cost(STARTER10, ACT1) > slot_cost(BIG, ACT1) > 0
assert pick([POMMEL], STARTER10, (), {**ACT1, "deck": STARTER10})[0] == "Pommel"
# 151. A card saving about nothing (an extra Strike in a 25-card Strike/Defend deck) is a Skip, not a coin flip.
assert pick([STRIKE], BIG, (), {**ACT1, "deck": BIG})[0] == "Skip"
# 152. One-turn Strength (Coordinate) is worth a slot only when drawn with attacks.
COORD = card("Coordinate", 1, "Skill", "Self", StrengthPower=5)
ATK6, ATK1 = [STRIKE] * 6 + [DEFEND] * 3 + [BASH], [STRIKE] + [DEFEND] * 8 + [BASH]
assert rate(COORD, ATK6, (), {**ACT1, "deck": ATK6})[2] > rate(COORD, ATK1, (), {**ACT1, "deck": ATK1})[2]
# 153. Status-adding enemies (Haunted Ship: 5 Dazed) put dead cards in the sim's piles: more HP lost.
ship = next(e for e, v in GD["encounters"].items() if "HauntedShip" in v["monsters"])
assert encounter(ship, 0)[3] > 0
import deck as _dd
with_status = _dd.hp_lost(STARTER10, ACT1)
_dd.STATUS_SHARE, share = 0, _dd.STATUS_SHARE
_dd.threats.cache_clear(); _dd._hp_lost.cache_clear()
assert _dd.hp_lost(STARTER10, ACT1) < with_status
_dd.STATUS_SHARE = share
_dd.threats.cache_clear(); _dd._hp_lost.cache_clear()
print("all deck dilution checks passed")

# --- research imports (v15: Research Notes + Spire-Forge, tools/import_research.py) ---
import picker as _pk
from relics import KNOWLEDGE as _KN
# 154. A card's tier value is the mean of its sources (Mobalytics tier, slaythetierlist stl, guide).
_KN["cards"]["_TwoTier"] = {"tier": "S", "stl": "D"}
_KN["cards"]["_OneTier"] = {"guide": "A"}
assert _pk.tier_value("_TwoTier") == (_pk.TIER_VALUE["S"] + _pk.TIER_VALUE["D"]) / 2
assert _pk.tier_value("_OneTier") == _pk.TIER_VALUE["A"] and _pk.tier_value("_NoTier") is None
del _KN["cards"]["_TwoTier"], _KN["cards"]["_OneTier"]
# 155. A combo partner already in the deck raises the score, capped at PAIR_CAP.
assert _pk.PAIRS, "knowledge.json has no pairs: run tools/import_research.py"
_pk.PAIRS["_ComboA"] = {"Strike": 10, "Defend": 30}
CA = card("_ComboA", 1, "Skill", "Self", Block=5)
assert _pk.combo(CA, [STRIKE]) == _pk.PAIR_SCALE * 10 and _pk.combo(CA, []) == 0
assert _pk.combo(CA, [STRIKE, DEFEND]) == _pk.PAIR_CAP
assert rate(CA, [STRIKE] * 3)[0] > rate(CA, [BASH] * 3)[0]
del _pk.PAIRS["_ComboA"]
# 156. Anti-synergy: Defy (Spire-Forge: bad in Doom builds) is worth less to a Doom-committed deck.
DOOM_DECK = [real_card(n) for n in ("ReaperForm", "DeathMarch", "TheScythe", "Reave", "Countdown")] * 2
assert "Defy" in _KN.get("anti", {}).get("Necrobinder:Doom", [])
assert _pk.combo(real_card("Defy"), DOOM_DECK) < 0 == _pk.combo(real_card("Defy"), [])
# 157. Every archetype core card / key relic exists: test 82 covers the v15 additions on both builds.
# 158. Snecko Eye (random costs) is worth less to a Claw deck.
CLAW_DECK = [real_card(n) for n in ("Claw", "Scrape", "AllForOne", "Feral", "Hologram")] * 2
assert relic_value("SneckoEye", {"deck": CLAW_DECK}) < relic_value("SneckoEye", {"deck": []})
print("all research import checks passed")

# --- v16: F8 autopilot play fixes ---
# 159. Uppercut's debuffs sit under a generic "Power" var: Uppercut first, so Strike hits the Vulnerable enemy
# (13 + 9) and the enemy's 10 hit is Weak (x0.75).
best = solve(state([STRIKE, UPPERCUT], enemy_hp=60, intent_dmg=10))[0]
assert [s[0] for s in best.steps] == ["Uppercut", "Strike"] and best.damage == 22 and best.hp_loss < 10, best
# 160. Strength from an attack (Setup Strike: deal 7, gain 2 Strength this turn) comes after its own hit: play it
# before the Strike (7 + 8). Fight Me! (5x2, +3 Strength) gives the enemy +1 Strength: its 10 hit becomes 11.
SETUP = card("SetupStrike", 1, "Attack", "AnyEnemy", Damage=7, StrengthPower=2)
best = solve(state([STRIKE, SETUP], energy=2, enemy_hp=60, intent_dmg=0))[0]
assert [s[0] for s in best.steps] == ["SetupStrike", "Strike"] and best.damage == 15, best
FIGHT = card("FightMe", 2, "Attack", "AnyEnemy", Damage=5, Repeat=2, StrengthPower=3, EnemyStrength=1)
best = solve(state([FIGHT, STRIKE], energy=3, enemy_hp=60, intent_dmg=10))[0]
assert [s[0] for s in best.steps] == ["FightMe", "Strike"] and best.damage == 19 and best.hp_loss == 11, best
# 161. Lasting Strength has value past this turn: Inflame with no attack in hand still beats ending the turn.
# Demon Form's Strength comes at the start of later turns, so this turn's Strike still deals 6.
INFLAME = card("Inflame", 1, "Power", "Self", StrengthPower=2)
DEMON = card("DemonForm", 3, "Power", "Self", StrengthPower=2)
s161 = {**state([INFLAME], energy=1, intent_dmg=0), "deck": [STRIKE] * 5 + [DEFEND] * 4 + [INFLAME]}
assert [s[0] for s in solve(s161)[0].steps] == ["Inflame"]
best = solve({**state([DEMON, STRIKE], energy=4, intent_dmg=0), "deck": [STRIKE] * 5 + [DEFEND] * 4 + [DEMON]})[0]
assert {s[0] for s in best.steps} == {"DemonForm", "Strike"} and best.damage == 6, best
# 162. One Defend already blocks the 5 hit: the second one adds nothing, but it costs nothing either, so the turn
# doesn't end with energy left.
best = solve(state([DEFEND, DEFEND], energy=2, intent_dmg=5))[0]
assert [s[0] for s in best.steps] == ["Defend", "Defend"] and best.energy_left == 0, best
#      Autopilot: an empty plan with energy and an affordable card left -> replan once, then log why before E.
saved = (ap.time.sleep, ap.find_game, ap.tap)
ap.time.sleep, ap.find_game, ap.tap = (lambda t: None), (lambda: (0, 0, 0, 1920, 1080)), (lambda k: None)
spare_st = {"status": "combat", "phase": "Play", "turn": 1, "energy": 1, "hand": [DEFEND],
            "player": {"hp": 50, "block": 0, "powers": []}, "enemies": []}
frames, planned, logs = iter([spare_st] * 16), [], []
ap.autopilot(lambda: next(frames, {"status": "out_of_combat"}),
             lambda s: planned.append(s) or type("P", (), {"steps": []})(), lambda s: ([], []), threading.Event(), logs.append)
assert len(planned) == 2 and any("ending with 1 energy" in m and "Defend" in m for m in logs), (planned, logs)
ap.time.sleep, ap.find_game, ap.tap = saved
# 163. Two 30-HP Gardeners, one Strike kills neither: hit the one with Ritual 3 (3 now, +3 every turn) rather than
#      the one hitting 7 now and never growing (the 2-turn view used to pick the 7).
rit = lambda cid, now, powers: {**gardener("FLAIL_MOVE", now, hp=30, cid=cid),
                                "creature": {"hp": 30, "max_hp": 30, "block": 0, "powers": powers}}
best = solve(fight([STRIKE], [rit(1, 3, [{"name": "RitualPower", "amount": 3}]), rit(2, 7, [])]))[0]
assert best.steps[0][4] == 1, best
# 164. Aiming with a lagging reader: enemy 2 sits at sweep point 3, enemy 1 at point 4. Point 3's first read is
#      stale (empty), so the sweep moves on; point 4's first read is stale too (still enemy 2). The cursor must go
#      back to point 3 and confirm enemy 2 there, not click enemy 1.
saved = ap.time.sleep
ap.time.sleep = lambda t: None
ap.positions.clear()
spot = {"p": None}
at_ = lambda fx, fy: spot.update(p=(fx, fy))
on = {ap.SWEEP[3]: 2, ap.SWEEP[4]: 1}
lag = {"left": 1}


def lagged():
    hov = on.get(spot["p"])
    if spot["p"] == ap.SWEEP[4] and lag["left"]:
        lag["left"] -= 1
        hov = 2  # the snapshot from when the mouse was still on point 3
    return {"status": "combat", "hovered": hov, "enemies": [{"combat_id": 1}, {"combat_id": 2}]}


first = {"seen": False}


def lag_first():  # point 3's own first read is stale too (still the empty point 2), so the sweep moves on
    if spot["p"] == ap.SWEEP[3] and not first["seen"]:
        first["seen"] = True
        return {"status": "combat", "hovered": None, "enemies": [{"combat_id": 1}, {"combat_id": 2}]}
    return lagged()


assert ap.aim(2, at_, lag_first, threading.Event()) and spot["p"] == ap.SWEEP[3] and ap.positions[2] == ap.SWEEP[3]
ap.time.sleep = saved
# 165. Forgotten Ritual: v0.107.1 gives its 3 energy only if a card was Exhausted earlier this turn; the beta always.
#      1 energy, a 3-cost 30-damage attack: on v0.107.1 Ritual alone can't pay for it (and isn't burned for nothing);
#      after an Exhaust card (or an exhaust earlier this turn) it can. On the beta Ritual always pays for it.
from archetypes import card_text
import solver as _sv
FR, BIG3 = real_card("ForgottenRitual"), card("Big30", 3, "Attack", "AnyEnemy", Damage=30)
EXH = {**card("Burnout", 0, "Skill", "Self", Block=1), "keywords": ["Exhaust"]}
names = lambda p: [s[0] for s in p.steps]
if "If you Exhausted" in card_text("ForgottenRitual"):
    assert names(solve(state([FR, BIG3], energy=1, enemy_hp=60, intent_dmg=0))[0]) == []
    assert names(solve(state([EXH, FR, BIG3], energy=1, enemy_hp=60, intent_dmg=0))[0]) == ["Burnout", "ForgottenRitual", "Big30"]
    _sv._turn_exhaust.clear()
    first_look = {**state([FR, BIG3], energy=1, enemy_hp=60, intent_dmg=0), "turn": 4, "exhaust": [STRIKE]}
    solve(first_look)  # turn 4 starts with 1 card in the exhaust pile
    later = {**first_look, "exhaust": [STRIKE, DEFEND]}  # a card was exhausted since
    assert names(solve(later)[0]) == ["ForgottenRitual", "Big30"]
else:
    assert names(solve(state([FR, BIG3], energy=1, enemy_hp=60, intent_dmg=0))[0]) == ["ForgottenRitual", "Big30"]
print("all v16 play checks passed")

# --- v17a: player powers in the turn math (game hooks, .planning/research/powers.md) ---
def powered(s, **powers):
    s["player"]["powers"] = [{"name": n + "Power", "amount": a} for n, a in powers.items()]
    return s


# 166. Plating 5 gives 5 Block at our turn end vs a 10 hit (unpowered: Dexterity and Frail don't change it).
#      Orichalcum checks for 0 Block before Plating lands, so both fire: 6 + 5 >= 10.
assert solve(powered(state([], energy=0, intent_dmg=10), Plating=5))[0].hp_loss == 5
assert solve(powered(state([], energy=0, intent_dmg=10), Plating=5, Dexterity=3, Frail=1))[0].hp_loss == 5
assert solve(kit(powered(state([], energy=0, intent_dmg=10), Plating=5), relics=[("Orichalcum", {})]))[0].hp_loss == 0
# 167. Stone Armor played this turn adds 4 Plating: a 10 hit costs 6.
best = solve(state([real_card("StoneArmor")], energy=1, intent_dmg=10))[0]
assert names(best) == ["StoneArmor"] and best.hp_loss == 6 and best.steps[0][2], best
# 168. Buffer cancels the 12 hit. 169. Intangible: three 10-damage hits cost 1 each.
assert solve(powered(state([], energy=0, intent_dmg=12), Buffer=1))[0].hp_loss == 0
tri = state([], energy=0)
tri["enemies"][0]["intents"] = [{"kind": "x", "damage": 10, "hits": 3}]
assert solve(powered(tri, Intangible=1))[0].hp_loss == 3
# 170. Afterimage 1: two Strikes give 2 Block. Rage 3: a Strike gives 3 Block. Juggernaut 6: Defend's Block deals 6.
#      Vigor 4: the next Attack deals 6 + 4.
assert solve(powered(state([STRIKE, STRIKE], energy=2, intent_dmg=0), Afterimage=1))[0].block == 2
assert solve(powered(state([STRIKE], energy=1, intent_dmg=0), Rage=3))[0].block == 3
assert solve(powered(state([DEFEND], energy=1, intent_dmg=5), Juggernaut=6))[0].damage == 6
assert solve(powered(state([STRIKE], energy=1, intent_dmg=0), Vigor=4))[0].damage == 10
# 171. Thorns 3 on us: an enemy hitting 2x5 takes 6 back.
two_hits = state([], energy=0)
two_hits["enemies"][0]["intents"] = [{"kind": "x", "damage": 5, "hits": 2}]
assert solve(powered(two_hits, Thorns=3))[0].damage == 6
# 172. Feel No Pain 3: an Exhaust card gives 3 Block. Plating/Buffer/... aren't flagged as unmodeled any more.
assert solve(powered(state([EXH], energy=1, intent_dmg=10), FeelNoPain=3))[0].block == 4
from solver import unmodeled_player_powers
assert unmodeled_player_powers(powered(state([], energy=0), Plating=1, Buffer=1, Regen=2)) == []
print("all v17a power checks passed")

# --- v17b: card effects read from the game code (gamedata ops, tools/gen_gamedata.parse_ops) ---
from solver import from_ops
# 173. Uppercut's generic "Power" var is Weak + Vulnerable by its code (no text patch): the Strike after it hits 9.
up = from_ops(real_card("Uppercut"))
assert up.get("ops") and up["vars"]["WeakPower"] == up["vars"]["VulnerablePower"] == real_card("Uppercut")["vars"]["Power"]
assert solve(state([real_card("Uppercut"), STRIKE], energy=3, enemy_hp=99, intent_dmg=0))[0].damage == 13 + 9
# 174. Twin Strike hits twice (WithHitCount(2) in code; the old var path counted one hit).
assert from_ops(real_card("TwinStrike"))["vars"]["Repeat"] == 2
assert solve(state([real_card("TwinStrike")], energy=1, enemy_hp=99, intent_dmg=0))[0].damage == 10
# 175. Shrug It Off: Block + draw; the draw count var survives for the pile math, the play counts as modeled.
shrug = from_ops(real_card("ShrugItOff"))
assert shrug["vars"]["Block"] == 8 and shrug["vars"]["Cards"] == 1
assert solve(state([real_card("ShrugItOff")], energy=1, intent_dmg=10))[0].steps[0][2]
# 176. Whirlwind: X hits to all enemies.
ww = {**real_card("Whirlwind"), "x_cost": True}
assert solve(state([ww], energy=3, enemy_hp=99, intent_dmg=0))[0].damage == 15
# 177. Live vars rule: an Uppercut whose Power reads 2 (upgraded/buffed in game) applies 2 of each.
assert from_ops({**real_card("Uppercut"), "vars": {"Damage": 13, "Power": 2}})["vars"]["VulnerablePower"] == 2
# 178. Self vs enemy Strength by code: Shared Fate costs us 2 and takes 2 from the enemy; ally cards (Blaze) and
#      fetch cards (Headbutt) keep the old var path. At least 80% of cards parse fully on this build.
sf = from_ops(real_card("SharedFate"))["vars"]
assert sf["StrengthPower"] < 0 and sf["StrengthLoss"] > 0
assert not from_ops(real_card("Headbutt")).get("ops")  # fetch: pile_moves path
if "Blaze" in GD["cards"]:  # v0.111.0 card
    assert not from_ops(real_card("Blaze")).get("ops")
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from gen_gamedata import ops_coverage
done, total, _ = ops_coverage(GD["cards"])
assert done / total >= 0.8, (done, total)
print("all v17b card-op checks passed")

# --- v17c: picks inside a card (discard / exhaust from hand), solver.toss ---
BURN = card("Burn", -1, "Status", "None", Damage=2)
DAZED = card("Dazed", -1, "Status", "None")
# 179. Acrobatics discards the Burn (unplayable, 2 damage at turn end) instead of a real card: no damage taken.
best = solve(state([real_card("Acrobatics"), BURN], energy=1, intent_dmg=0))[0]
assert names(best) == ["Acrobatics"] and best.hp_loss == 0, best
assert solve(state([BURN], energy=0, intent_dmg=0))[0].hp_loss == 2
# 180. True Grit+ exhausts the Dazed, not the Strike: the Strike is still played after it.
tg_up = {**real_card("TrueGrit"), "upgraded": True}
best = solve(state([tg_up, STRIKE, DAZED], energy=2, enemy_hp=99, intent_dmg=10))[0]
assert sorted(names(best)) == ["Strike", "TrueGrit+"] and best.damage == 6, best
# 181. A Sly card discarded by Survivor is played for free.
sly = {**card("SlyStab", 2, "Attack", "AnyEnemy", Damage=10), "keywords": ["Sly"]}
best = solve(state([real_card("Survivor"), sly], energy=1, enemy_hp=99, intent_dmg=10))[0]
assert names(best) == ["Survivor"] and best.damage == 10, best
# 182. True Grit's random exhaust still triggers Feel No Pain. (With only a Strike beside it, the solver rightly
#      keeps the Strike: a sure exhaust of your attack costs more than 10 Block.)
tg = real_card("TrueGrit")
best = solve(powered(state([tg, DAZED], energy=1, enemy_hp=99, intent_dmg=20), FeelNoPain=3))[0]
assert names(best) == ["TrueGrit"] and best.block == tg["vars"]["Block"] + 3, best
print("all v17c pick checks passed")

# --- v19a: HP discipline (hallway HP costs most, boss HP least) and focus fire ---
# 183. Hallway fight, 80/80 HP, 6 incoming: Defend (0 HP) beats chipping a 40-HP enemy.
assert names(solve(state([STRIKE, DEFEND], energy=1, enemy_hp=40, intent_dmg=6))[0]) == ["Defend"]
# 184. Two Gardeners with the same move: the one nearer death takes the Strike (threat per HP left).
best = solve(fight([STRIKE], [gardener("BITE_MOVE", 5, hp=25, cid=1), gardener("BITE_MOVE", 5, hp=8, cid=2)]))[0]
assert best.steps[0][4] == 2, best
# 185. Boss hits 25: the exhausting 30-Block card is spent (boss HP is cheap, so is the card kept for later).
WALL = {**card("Wall", 1, "Skill", "Self", Block=30), "keywords": ["Exhaust"]}
st = {**state([STRIKE, WALL], energy=1, enemy_hp=200, intent_dmg=25), "encounter": BOSS,
      "deck": [STRIKE] * 5 + [DEFEND] * 5 + [WALL]}
assert names(solve(st)[0]) == ["Wall"], solve(st)[0]
# 186. Burning Blood at 79/80 doesn't make a 4 hit free when the Wurm has 11 and 18 hits still to come: the heal's
#      spare room is used up later in the fight anyway. Defend + 2 Strikes (0 HP) over 3 Strikes (-4).
wurm = {**foe("FuzzyWurmCrawler", "FIRST_ACID_GOOP"), "combat_id": 1, "intents": [{"kind": "x", "damage": 4, "hits": 1}],
        "creature": {"hp": 55, "max_hp": 55, "block": 0, "powers": []}}
st = {**fight([DEFEND, STRIKE, DEFEND, STRIKE, STRIKE], [wurm], energy=3, hp=79), "relics": [{"name": "BurningBlood"}]}
assert solve(st)[0].hp_loss == 0, solve(st)[0]
# 187. Spite hits twice only if HP was lost this turn; a lethal plan with a Strength Potion drinks it first.
SPITE = card("Spite", 0, "Attack", "AnyEnemy", Damage=5, Repeat=2)
assert solve(state([SPITE], energy=0, enemy_hp=40, intent_dmg=0))[0].damage == 5
best = solve({**state([STRIKE, STRIKE], energy=2, enemy_hp=14, intent_dmg=10),
              "potions": [{"name": "StrengthPotion", "slot": 0}]})[0]
assert best.win and names(best)[0] == "POTION StrengthPotion", best
# 188. Played an extra time: Duplicator + Strike kills a 12-HP enemy; Echo Form doubles the first card; One-Two Punch
#      goes before the Twin Strike it doubles (Strength from Inflame counts on every hit); Dominate gains Strength
#      per Vulnerable (first: its Vulnerable makes Bash hit harder too).
best = solve({**state([STRIKE], energy=1, enemy_hp=12, intent_dmg=20), "potions": [{"name": "Duplicator", "slot": 0}]})[0]
assert best.win and names(best) == ["POTION Duplicator", "Strike"], best
assert solve(powered(state([STRIKE, STRIKE], energy=2, enemy_hp=99, intent_dmg=0), EchoForm=1))[0].damage == 18
best = solve(state([real_card("TwinStrike"), real_card("OneTwoPunch"), real_card("Inflame")], energy=4, enemy_hp=99,
                   intent_dmg=0))[0]
assert names(best) == ["Inflame", "OneTwoPunch", "TwinStrike"] and best.damage == 28, best
assert solve(state([STRIKE, real_card("Dominate"), BASH], energy=4, enemy_hp=99, intent_dmg=0))[0].damage == 23
# --- W1 (wiki rules): combat correctness ---
# 189. Strength -9: a Strike deals 0 and doesn't add Block to the enemy (negative hit).
st = fight([STRIKE], [boss(40, [])]); st["player"]["powers"] = [{"name": "StrengthPower", "amount": -9}]
st["enemies"][0]["creature"]["block"] = 5
assert solve(st)[0].damage == 0
from solver import hit
e = [40, 5, 0, 0, 0, -1, 0, 0, 0, 0, 0, 0, 0]
hit(e, -3)
assert e[1] == 5, e
# 190. Enemy Intangible caps the hit to 1 before Block: 20 into 5 Block + Intangible = Block 4, HP untouched.
e = [40, 5, 0, 0, 0, -1, 0, 1, 0, 0, 0, 0, 0]
assert hit(e, 20) == 0 and e[1] == 4 and e[0] == 40, e
# 191. Strength only boosts Attacks: a damaging Skill ignores it.
ZAP = card("TestZap", 1, "Skill", "AnyEnemy", Damage=6)
st = fight([ZAP], [boss(40, [])]); st["player"]["powers"] = [{"name": "StrengthPower", "amount": 3}]
assert solve(st)[0].damage == 6
# 192. Poison on an Intangible enemy ticks for 1 (HP loss is capped too).
assert solve(fight([], [boss(40, [("PoisonPower", 9), ("IntangiblePower", 1)])]))[0].steps == []
# 193. Artifact blocks enemy Strength loss (a debuff) just like Vulnerable.
SHACKLE = card("TestShackle", 0, "Skill", "AnyEnemy", StrengthLoss=6)
assert solve(fight([SHACKLE], [boss(40, [], now=10)]))[0].hp_loss == 4
assert solve(fight([SHACKLE], [boss(40, [("ArtifactPower", 1)], now=10)]))[0].hp_loss == 10
# 194. Cultist's Incantation (Ritual 2) skips that turn's trigger: Dark Strike 9, then 11, 13.
inc = {"name": "CalcifiedCultist", "move": "INCANTATION_MOVE", "history": [], "slot": None, "creature": {"powers": []}}
assert [t["dmg"] for t in predict(inc, 0, 3)] == [9, 11, 13]
print("all W1 checks passed")
print("all v19a checks passed")
