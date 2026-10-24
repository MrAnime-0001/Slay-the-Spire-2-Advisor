"""One-turn card-sequence solver for the JSON state printed by reader.exe."""
import math
from dataclasses import dataclass

# Score weights. Tune these.
W_WIN, W_KILL, W_HP, W_DEATH = 100, 30, 2, 1000
UNKNOWN_VALUE = 2  # flat credit for effects we don't model (draw, exhaust, most powers)
MODELED = {"Damage", "Repeat", "Block", "StrengthPower", "DexterityPower", "VulnerablePower", "WeakPower"}
# ponytail: one-turn horizon; ignores draw, card generation, relics and power cards' future value.
# Add lookahead / draw sampling once one-turn advice proves useful.


@dataclass
class Plan:
    steps: list      # [(card name, target name or None, modeled: bool)]
    damage: int      # enemy HP removed
    block: int
    hp_loss: int
    kills: int
    win: bool
    score: float


def amount(powers, name):
    return next((p["amount"] for p in powers if p["name"] == name), 0)


def solve(state, top=3):
    if state.get("status") != "combat":
        return []
    me = state["player"]
    foes = state["enemies"]
    names = [f["name"] + (f" #{i + 1}" if sum(g["name"] == f["name"] for g in foes) > 1 else "")
             for i, f in enumerate(foes)]

    # Group identical cards so Strike/Strike/Strike isn't explored 3! ways.
    groups, keys = [], {}
    for c in state["hand"]:
        if c["type"] not in ("Attack", "Skill", "Power"):
            continue
        k = (c["name"], c["upgraded"], c["cost"], tuple(sorted(c["vars"].items())))
        if k not in keys:
            keys[k] = len(groups)
            groups.append([c, 0])
        groups[keys[k]][1] += 1

    start = (
        state["energy"],
        tuple(n for _, n in groups),
        tuple((f["creature"]["hp"], f["creature"]["block"],
               amount(f["creature"]["powers"], "VulnerablePower"),
               amount(f["creature"]["powers"], "WeakPower")) for f in foes),
        me["block"],
        amount(me["powers"], "StrengthPower"),
        amount(me["powers"], "DexterityPower"),
    )
    weak = amount(me["powers"], "WeakPower") > 0
    frail = amount(me["powers"], "FrailPower") > 0

    def play(card, tgt, st):
        energy, counts, en, block, strength, dex = st
        cost = energy if card["x_cost"] else card["cost"]
        times = energy if card["x_cost"] else 1
        v = card["vars"]
        en = [list(e) for e in en]
        alive = [i for i, e in enumerate(en) if e[0] > 0]
        hit = alive if card["target"] in ("AllEnemies", "RandomEnemy") else [tgt] if tgt is not None else []
        # ponytail: RandomEnemy treated as hitting every enemy; fine for one target, optimistic for many.
        if "Damage" in v:
            base = (v["Damage"] + strength) * (0.75 if weak else 1)
            for i in hit:
                per = math.floor(base * (1.5 if en[i][2] > 0 else 1))
                for _ in range(max(v.get("Repeat", 1), 1) * times):
                    absorbed = min(en[i][1], per)
                    en[i][1] -= absorbed
                    en[i][0] = max(0, en[i][0] - (per - absorbed))
        if "Block" in v:
            block += max(0, math.floor((v["Block"] + dex) * (0.75 if frail else 1))) * times
        self_buff = card["type"] == "Power" or card["target"] in ("Self", "None")
        if self_buff:
            strength += v.get("StrengthPower", 0)
            dex += v.get("DexterityPower", 0)
        else:
            for i in hit:
                en[i][2] += v.get("VulnerablePower", 0)
                en[i][3] += v.get("WeakPower", 0)
        return (energy - cost, counts, tuple(map(tuple, en)), block, strength, dex)

    def evaluate(st, steps):
        _, _, en, block, _, _ = st
        start_en = start[2]
        kills = sum(1 for a, b in zip(start_en, en) if a[0] > 0 and b[0] == 0)
        win = all(e[0] == 0 for e in en)
        vuln = amount(me["powers"], "VulnerablePower") > 0
        incoming = 0
        if not win:
            for f, e in zip(foes, en):
                if e[0] == 0:
                    continue
                s = amount(f["creature"]["powers"], "StrengthPower")
                for it in f["intents"]:
                    if it["damage"] is not None:
                        per = math.floor((it["damage"] + s) * (0.75 if e[3] > 0 else 1) * (1.5 if vuln else 1))
                        incoming += per * max(it["hits"], 1)
        hp_loss = max(0, incoming - block)
        damage = sum(a[0] - b[0] for a, b in zip(start_en, en))
        unknown = sum(1 for _, _, ok in steps if not ok)
        score = (W_WIN * win + W_KILL * kills + damage - W_HP * hp_loss + UNKNOWN_VALUE * unknown
                 - W_DEATH * (hp_loss >= me["hp"]))
        return Plan(list(steps), damage, block - start[3], hp_loss, kills, win, score)

    plans, seen = [], set()

    def dfs(st, steps):
        if st in seen:
            return
        seen.add(st)
        plans.append(evaluate(st, steps))
        energy, counts, en = st[0], st[1], st[2]
        for g, (card, _) in enumerate(groups):
            if counts[g] == 0 or (card["cost"] > energy and not card["x_cost"]) or (card["x_cost"] and energy == 0):
                continue
            nxt = counts[:g] + (counts[g] - 1,) + counts[g + 1:]
            modeled = bool(set(card["vars"]) & MODELED) and set(card["vars"]) <= MODELED
            targets = [i for i, e in enumerate(en) if e[0] > 0] if card["target"] == "AnyEnemy" else [None]
            for t in targets:
                s2 = play(card, t, (st[0], nxt) + st[2:])
                dfs(s2, steps + [(card["name"] + ("+" if card["upgraded"] else ""),
                                  names[t] if t is not None else None, modeled)])

    dfs(start, [])
    plans.sort(key=lambda p: (-p.score, len(p.steps)))
    return plans[:top]
