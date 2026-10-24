from solver import solve


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
