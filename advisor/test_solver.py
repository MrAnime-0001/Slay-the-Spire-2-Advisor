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
UPPERCUT = card("Uppercut", 2, "Attack", "AnyEnemy", Damage=13, WeakPower=1, VulnerablePower=1)
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
assert best.types[0] == "Elite" and direction(st, best.coords[0]) == "left", best

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
#     Without foresight (no move known) the same hand attacks.
eel = {**foe("TerrorEel", "THRASH_MOVE"), "combat_id": 1, "creature": {"hp": 140, "max_hp": 140, "block": 0,
       "powers": []}, "intents": [{"kind": "x", "damage": 3, "hits": 1}]}
assert [s[0] for s in solve(fight([STRIKE, DEFEND], [eel], hp=50))[0].steps] == ["Defend"]
assert [s[0] for s in solve(fight([STRIKE, DEFEND], [{**eel, "move": None}], hp=50))[0].steps] == ["Strike"]

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
assert unmodeled_powers({"enemies": [boss(50, [("ArtifactPower", 1), ("VitalSparkPower", 1)])]}) == ["VitalSpark"]

# 62. Sloth 3 with 2 cards already played: only one more card this turn.
slow_me = fight([STRIKE, STRIKE, STRIKE], [boss(100, [])], energy=3)
slow_me["player"]["powers"] = [{"name": "SlothPower", "amount": 3, "played": 2}]
assert len(solve(slow_me)[0].steps) == 1

# 63. Ringing card after a card was played this turn: unplayable; Smog: never playable.
ring = fight([{**STRIKE, "affliction": "Ringing"}, {**DEFEND, "affliction": "Smog"}], [boss(100, [])], energy=3)
assert solve({**ring, "cards_played": 1})[0].steps == []
assert [s[0] for s in solve(ring)[0].steps] == ["Strike"]

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


# 52. Tier list fixes the stats heuristic: Tremble (S) over Setup Strike (C); Feel No Pain (A) worth taking.
assert rate(real("Tremble"), DECK)[0] > rate(real("SetupStrike"), DECK)[0]
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
