"""One-turn card-sequence solver for the JSON state printed by reader.exe."""
import math
from dataclasses import dataclass

from enemies import predict
from relics import (GAMEDATA, NO_PLAYS, Plays, Relics, after_play, block_mult, dmg_add, dmg_mult,
                    end_turn, x_bonus)

# Score weights. Tune these.
W_WIN, W_KILL, W_HP, W_DEATH = 100, 30, 2, 1000
UNKNOWN_VALUE = 2  # flat credit for effects we don't model (draw, exhaust, most powers)
# HP matters more as it gets low: below LOW_HP_FRACTION of max, each HP lost costs up to
# (1 + LOW_HP_WEIGHT)x more, so a low-HP character blocks/heals instead of trading.
LOW_HP_FRACTION, LOW_HP_WEIGHT = 0.5, 3
# Tempo: enemies scale the longer a fight runs, so damage dealt is worth the future hits it saves:
# value per damage = W_HP * enemy damage per turn / our deck's damage per turn, clamped.
TEMPO_MIN, TEMPO_MAX, PRESSURE_FLOOR, DPT_FLOOR = 0.5, 1.5, 6, 5
# Score charged per potion (W_HP=2, so 40 = "worth ~20 HP"): saved in hallway fights for later
# encounters, spent on bosses. Full slots halve it (an unused potion blocks new drops). Below
# POTION_FREE_HP of max HP the cost scales to the minimum: drink rather than die.
POTION_COST = {"Monster": 40, "Elite": 20, "Boss": 8}
FULL_SLOTS_FACTOR, MIN_POTION_COST, POTION_FREE_HP = 0.5, 4, 0.3
HEAL = {"Heal", "HealPercent", "RegenPower"}
EFFECTS = {"Damage", "Block", "StrengthPower", "DexterityPower", "VulnerablePower", "WeakPower",
           "Energy", "HpLoss"} | HEAL
MODELED = EFFECTS | {"Repeat"}
# Foresight (enemies.predict, from the game's AI tables): each enemy's expected damage over the next
# FUTURE_TURNS turns, discounted per turn. Enemies left alive keep that future damage (FUTURE_WEIGHT per
# HP), HP danger is judged after next turn's predicted hits, and damage counts more on high-threat enemies.
FUTURE_TURNS, FUTURE_DISCOUNT, FUTURE_WEIGHT = 2, 0.6, 0.5
THREAT_MIN, THREAT_MAX = 0.5, 2
# The Insatiable: Frantic Escape (Status, 1 energy) pushes its Sandpit countdown back by 1. Worth more the
# lower the countdown; letting it reach 0 kills you.
ESCAPE, SANDPIT_VALUE = "FranticEscape", 12
DAZED_COST = 1.5  # score per Dazed shuffled into our deck (Entomancer's Personal Hive): prefer big hits
# ponytail: plays one turn of cards; ignores draw, card generation and power cards' future value.
# Enemy foresight covers the enemy side; add draw sampling if our own future turns matter.


@dataclass
class Plan:
    steps: list      # [(label, target name, modeled, card/potion dict, target combat_id, kind "card"|"potion")]
    damage: int      # enemy HP removed
    block: int
    hp_loss: int     # net HP change after the enemy turn (negative = healed)
    kills: int
    win: bool
    score: float


def amount(powers, name):
    return next((p["amount"] for p in powers if p["name"] == name), 0)


# Enemy powers the solver/predict read. Anything else on an enemy is shown in the overlay as "not modeled".
ENEMY_POWERS = {"StrengthPower", "VigorPower", "TerritorialPower", "VulnerablePower", "WeakPower", "SkittishPower",
                "HardenedShellPower", "SlipperyPower", "IntangiblePower", "ArtifactPower", "SlowPower",
                "PersonalHivePower", "EnragePower", "ShriekPower", "SandpitPower", "MinionPower", "CurlUpPower",
                "HardToKillPower", "FlutterPower", "SoarPower", "ThornsPower", "RitualPower"}


def unmodeled_powers(state):
    return sorted({p["name"].removesuffix("Power") for e in state.get("enemies", [])
                   for p in e["creature"].get("powers", []) if p["name"] not in ENEMY_POWERS})


# Player powers that change card costs (Hook.ModifyEnergyCostInCombat, then the "Late" hooks that set 0).
COST_POWERS = {"TangledPower", "BorrowedTimePower", "CuriousPower", "CorruptionPower", "VoidFormPower",
               "FreeAttackPower", "FreeSkillPower", "FreePowerPower"}


def card_cost(card, powers, played=0, plays=NO_PLAYS):
    """Energy to play `card` now: the reader's cost (base + local modifiers like "free this turn") plus the
    player's cost powers. played = cards played this turn; plays = this plan's plays so far.
    ponytail: Veilpiercer (Ethereal cards free) skipped, card keywords aren't read."""
    c, t = card["cost"], card["type"]
    if t == "Potion" or card["x_cost"] or c < 0:
        return c
    c += powers.get("BorrowedTimePower", 0)
    if card.get("affliction") == "Entangled":  # Tangled (Vine Shambler): Attacks cost more
        c += powers.get("TangledPower", 0)
    if t == "Power" and c > 0:
        c = max(0, c - powers.get("CuriousPower", 0))
    free = ((t == "Skill" and "CorruptionPower" in powers) or played < powers.get("VoidFormPower", 0)
            or (t == "Attack" and plays.attacks < powers.get("FreeAttackPower", 0))
            or (t == "Skill" and plays.skills < powers.get("FreeSkillPower", 0))
            or (t == "Power" and plays.powers < powers.get("FreePowerPower", 0)))
    return 0 if free else c


# Player powers the solver reads (or whose effect is already in the live numbers: Mind Rot = fewer cards drawn,
# Waste Away = lower max energy; Ringing/Smoggy/Chains/Hex/Tangled act through card afflictions).
PLAYER_POWERS = COST_POWERS | {"StrengthPower", "DexterityPower", "WeakPower", "FrailPower", "VulnerablePower",
                               "SlothPower", "DisintegrationPower", "ConstrictPower", "ShrinkPower", "TenderPower",
                               "MindRotPower", "WasteAwayPower", "RingingPower", "SmoggyPower", "ChainsOfBindingPower",
                               "HexPower"}


def unmodeled_player_powers(state):
    return sorted({p["name"].removesuffix("Power") for p in state.get("player", {}).get("powers", [])
                   if p["name"] not in PLAYER_POWERS})


def potion_actions(state):
    """Held potions whose effects the solver can model, shaped like cards (type "Potion", cost 0)."""
    out = []
    for p in state.get("potions", []):
        d = GAMEDATA["potions"].get(p["name"])
        if not d:
            continue
        v = {k: int(n) for k, n in d["vars"].items()}
        if not set(v) & EFFECTS or not set(v) <= MODELED:
            continue
        if d["target"] != "Self" and "StrengthPower" in v:
            continue  # enemy Strength loss isn't modeled
        out.append({"name": p["name"], "type": "Potion", "target": d["target"], "cost": 0,
                    "x_cost": False, "upgraded": False, "vars": v, "slot": p["slot"]})
    return out


def fight_type(state):
    return GAMEDATA.get("encounters", {}).get(state.get("encounter"), {}).get("class", "Monster")


def potion_cost(state, hp=None):
    """hp: HP to judge by (solve passes HP after this turn's and next turn's predicted hits)."""
    cost = POTION_COST.get(fight_type(state), POTION_COST["Monster"])
    if state.get("potion_slots") and len(state.get("potions", [])) >= state["potion_slots"]:
        cost *= FULL_SLOTS_FACTOR
    me = state["player"]
    frac = (me["hp"] if hp is None else hp) / max(me["max_hp"], 1)
    cost *= min(1, max(0, (frac - POTION_FREE_HP) / POTION_FREE_HP))  # 0 at <=30% HP, full at >=60%
    return max(MIN_POTION_COST, cost)


def danger(hp, max_hp):
    """Multiplier on the cost of losing HP when ending at `hp`."""
    frac = max(0, hp) / max(max_hp, 1)
    return 1 + LOW_HP_WEIGHT * max(0, LOW_HP_FRACTION - frac) / LOW_HP_FRACTION


def deck_dpt(state):
    """Our deck's rough damage per turn: damage per energy times max energy."""
    deck = [c for c in (state.get("deck") or state["hand"]) if c["type"] in ("Attack", "Skill", "Power")]
    dmg = sum(c["vars"].get("Damage", 0) * max(c["vars"].get("Repeat", 1), 1) for c in deck)
    energy = sum(max(c["cost"], 1) for c in deck) or 1
    return max(DPT_FLOOR, dmg / energy * state.get("max_energy", state.get("energy") or 3))


def tempo(state, pressure):
    """Score per point of enemy HP removed: the future hits a faster kill saves."""
    return min(TEMPO_MAX, max(TEMPO_MIN, W_HP * max(pressure, PRESSURE_FLOOR) / deck_dpt(state)))


def hit(e, n):
    """Deal n damage to enemy row e = [hp, block, vuln, weak, skittish, shell_left, slippery, intangible].
    Block absorbs first; Slippery and Intangible turn a damaging hit into 1; Hardened Shell caps HP lost
    per turn. Returns the HP actually lost."""
    absorbed = min(e[1], n)
    e[1] -= absorbed
    lost = n - absorbed
    if lost >= 1 and e[6] > 0:
        lost, e[6] = 1, e[6] - 1
    if lost >= 1 and e[7] > 0:
        lost = 1
    if e[5] >= 0:
        lost = min(lost, e[5])
        e[5] -= lost
    lost = min(lost, e[0])
    e[0] -= lost
    return lost


def solve(state, top=3):
    if state.get("status") != "combat":
        return []
    me = state["player"]
    foes = state["enemies"]
    R = Relics(state.get("relics", []), GAMEDATA["relics"])
    names = [f["name"] + (f" #{i + 1}" if sum(g["name"] == f["name"] for g in foes) > 1 else "")
             for i, f in enumerate(foes)]

    # Group identical cards/potions so Strike/Strike/Strike isn't explored 3! ways.
    groups, keys = [], {}
    junk = lambda c: c["type"] in ("Status", "Curse")
    # Status/Curse with a cost (Toxic, Slimed, Frantic Escape) can be played to get them out of hand.
    playable = [c for c in state["hand"] if c["type"] in ("Attack", "Skill", "Power") or (junk(c) and c["cost"] >= 0)]
    # Burn, Wither, Infection, Decay, Toxic: their Damage hits us at turn end while in hand (blockable);
    # Regret: unblockable damage equal to the cards left in hand.
    end_dmg = lambda c: c["vars"].get("Damage", 0) if junk(c) else 0
    stuck = sum(end_dmg(c) for c in state["hand"] if junk(c) and c["cost"] < 0)
    regret = sum(c["name"] == "Regret" for c in state["hand"])
    for c in playable + potion_actions(state):
        k = (c["type"] == "Potion", c["name"], c["upgraded"], c["cost"], c.get("affliction"), tuple(sorted(c["vars"].items())))
        if k not in keys:
            keys[k] = len(groups)
            groups.append([c, 0])
        groups[keys[k]][1] += 1

    # state: energy, group counts, enemies (hp, block, vuln, weak, skittish, shell left (-1 = none),
    # slippery, intangible, Dazed added to our deck, artifact), block, strength, dex, Plays,
    # potions used, our HP (after self-damage and heals)
    def pw(f, name):
        return amount(f["creature"]["powers"], name)

    start = (
        state["energy"],
        tuple(n for _, n in groups),
        tuple((f["creature"]["hp"], f["creature"]["block"], pw(f, "VulnerablePower"), pw(f, "WeakPower"),
               pw(f, "SkittishPower") + pw(f, "CurlUpPower"), pw(f, "HardenedShellPower") or -1, pw(f, "SlipperyPower"),
               pw(f, "IntangiblePower"), 0, pw(f, "ArtifactPower")) for f in foes),
        me["block"],
        amount(me["powers"], "StrengthPower"),
        amount(me["powers"], "DexterityPower"),
        NO_PLAYS,
        0,
        me["hp"],
    )
    slowed = [pw(f, "SlowPower") > 0 for f in foes]
    hive = [pw(f, "PersonalHivePower") for f in foes]
    before = state.get("cards_played") or 0  # cards already played this turn (autopilot replans after each)
    # Knowledge Demon curses on us: Sloth caps cards per turn, Disintegration hits us at our turn end.
    sloth = next((p for p in me["powers"] if p["name"] == "SlothPower"), None)
    cap = sloth["amount"] - (sloth.get("played") or before) if sloth else math.inf
    mine = {p["name"]: p["amount"] for p in me["powers"]}

    def cost_of(card, plays):
        return card_cost(card, mine, before + plays.cards, plays)

    burn = amount(me["powers"], "DisintegrationPower") + amount(me["powers"], "ConstrictPower")  # at our turn end
    # Hallway powers: Hard to Kill caps each hit; Flutter/Soar halve our attacks on them; Thorns hit back per
    # attack hit; Shrink (on us) cuts our attacks to 70%; Tender (on us) costs 1 Str and 1 Dex per card played.
    # ponytail: Flutter's stun after enough unblocked hits isn't modeled.
    hard_cap = [pw(f, "HardToKillPower") or math.inf for f in foes]
    halved = [0.5 if pw(f, "FlutterPower") or pw(f, "SoarPower") else 1 for f in foes]
    thorns = [pw(f, "ThornsPower") for f in foes]
    # Minions (Gas Bomb, Illusions...) die when the last non-minion enemy dies (CreatureCmd.Kill).
    minion = [pw(f, "MinionPower") > 0 for f in foes]
    leaders = not all(minion)
    shrink = 0.7 if amount(me["powers"], "ShrinkPower") else 1
    tender = amount(me["powers"], "TenderPower") > 0
    weak = amount(me["powers"], "WeakPower") > 0
    frail = amount(me["powers"], "FrailPower") > 0

    def incoming_each(en, skills=0):
        """Damage each living enemy deals next enemy turn (Strength, Vigor, Weak, our Vulnerable;
        Enrage: +Strength per Skill we played)."""
        vuln = amount(me["powers"], "VulnerablePower") > 0
        out = []
        for f, e, e0 in zip(foes, en, start[2]):
            total = 0
            s = amount(f["creature"]["powers"], "StrengthPower") + skills * pw(f, "EnragePower")
            vigor = amount(f["creature"]["powers"], "VigorPower")  # adds to every hit of the next attack
            shriek = amount(f["creature"]["powers"], "ShriekPower")  # Terror Eel: stunned at this HP
            stunned = shriek and e[0] <= shriek < e0[0]
            for it in f["intents"] if e[0] > 0 and not stunned else []:
                if it["damage"] is not None:
                    per = math.floor((it["damage"] + s + vigor) * (0.75 if e[3] > 0 else 1) * (1.5 if vuln else 1))
                    total += per * max(it["hits"], 1)
                    vigor = 0
            out.append(total)
        return out

    def incoming(en, skills=0):
        return sum(incoming_each(en, skills))

    # The Insatiable's Sandpit: a countdown that kills you at 0; each Frantic Escape played adds 1.
    sandpit = max((pw(f, "SandpitPower") for f in foes), default=0)
    escape_groups = [g for g, (c, _) in enumerate(groups) if c["name"] == ESCAPE]

    value_per_damage = tempo(state, incoming(start[2]))
    forecast = [predict(f, state.get("ascension", 0), FUTURE_TURNS) for f in foes]
    future = [sum(t["dmg"] * FUTURE_DISCOUNT ** k for k, t in enumerate(fc)) for fc in forecast]
    next_turn = [fc[0]["dmg"] if fc else 0 for fc in forecast]
    threat = [n + fu for n, fu in zip(incoming_each(start[2]), future)]
    mean_threat = sum(threat) / max(len(threat), 1)
    weight = [min(THREAT_MAX, max(THREAT_MIN, t / mean_threat)) if mean_threat else 1 for t in threat]
    # Potions get cheaper when THIS turn's hits would leave us low. A big hit predicted for next turn
    # is a reason to keep the potion for that turn, not to spend it now.
    pot_cost = potion_cost(state, me["hp"] + me["block"] - incoming(start[2]))

    def play(card, tgt, st):
        energy, counts, en, block, strength, dex, plays, pots, hp = st
        potion = card["type"] == "Potion"
        cost = energy if card["x_cost"] else cost_of(card, plays)
        times = energy + x_bonus(R) if card["x_cost"] else 1
        v = {} if junk(card) else card["vars"]
        en = [list(e) for e in en]
        alive = [i for i, e in enumerate(en) if e[0] > 0]
        targets = alive if card["target"] in ("AllEnemies", "RandomEnemy") else [tgt] if tgt is not None else []
        # ponytail: RandomEnemy treated as hitting every enemy; fine for one target, optimistic for many.
        hp -= v.get("HpLoss", 0)  # self-damage (e.g. Bloodletting) happens on play
        heal = v.get("Heal", 0) + me["max_hp"] * v.get("HealPercent", 0) // 100 + v.get("RegenPower", 0)
        hp = min(me["max_hp"], hp + heal) if hp > 0 else hp  # ponytail: Regen counted as one tick now
        if "Damage" in v:
            if potion:  # potion damage ignores Strength/Weak
                base = v["Damage"]
            else:
                base = (v["Damage"] + strength + dmg_add(card, R)) * (0.75 if weak else 1) * dmg_mult(card, R, plays)
            for i in targets:
                slow = 1 + 0.1 * (before + plays.cards) if slowed[i] and not potion else 1  # Bygone Effigy
                mult = 1 if potion else halved[i] * shrink
                per = min(math.floor(base * (1.5 if en[i][2] > 0 else 1) * slow * mult), hard_cap[i])
                hits = [hit(en[i], per) for _ in range(max(v.get("Repeat", 1), 1) * times)]
                if thorns[i] and not potion:  # Thorns: our block soaks it first
                    back = thorns[i] * len(hits)
                    soaked = min(block, back)
                    block -= soaked
                    hp -= back - soaked
                if sum(hits) and en[i][4] and not potion:  # Skittish: block after the first damaging attack
                    en[i][1] += en[i][4]
                    en[i][4] = 0
                if not potion:  # Personal Hive (Entomancer): each attack hit that lands adds Dazed
                    en[i][8] += hive[i] * sum(1 for h in hits if h)
        if "Block" in v:
            gained = v["Block"] if potion else max(0, math.floor((v["Block"] + dex) * (0.75 if frail else 1))) * times
            block += gained * (1 if potion else block_mult(R, plays))
        if card["type"] == "Power" or card["target"] in ("Self", "None"):
            strength += v.get("StrengthPower", 0)
            dex += v.get("DexterityPower", 0)
            energy += v.get("Energy", 0)
        else:
            for i in targets:
                for slot, key in ((2, "VulnerablePower"), (3, "WeakPower")):
                    if v.get(key):
                        if en[i][9] > 0:  # Artifact blocks one debuff per stack
                            en[i][9] -= 1
                        else:
                            en[i][slot] += v[key]
        energy -= cost
        if potion:
            pots += 1
        else:
            t = card["type"]
            plays = Plays(plays.attacks + (t == "Attack"), plays.skills + (t == "Skill"),
                          plays.powers + (t == "Power"), plays.cards + 1, plays.block + ("Block" in v))
            fx = after_play(card, cost, R, plays)
            strength += fx.get("str", 0) - tender
            dex += fx.get("dex", 0) - tender
            block += fx.get("block", 0)
            energy += fx.get("energy", 0)
            for i in [i for i, e in enumerate(en) if e[0] > 0]:
                hit(en[i], fx.get("dmg_all", 0))
            alive = [i for i, e in enumerate(en) if e[0] > 0]
            if fx.get("dmg_one") and alive:  # ponytail: game picks a random enemy; we assume the first
                hit(en[alive[0]], fx["dmg_one"])
        return (energy, counts, tuple(map(tuple, en)), block, strength, dex, plays, pots, hp)

    def evaluate(st, steps):
        _, counts, en, block, _, _, plays, pots, hp = st
        extra, relic_dmg = end_turn(R, block, len(state["hand"]) - plays.cards, state.get("turn", 0), plays)
        block += extra
        en = [list(e) for e in en]
        for e in en:
            if e[0] > 0:
                hit(e, relic_dmg)
        if leaders and all(e[0] == 0 or m for e, m in zip(en, minion)):
            for e in en:
                e[0] = 0
        start_en = start[2]
        kills = sum(1 for a, b in zip(start_en, en) if a[0] > 0 and b[0] == 0)
        win = all(e[0] == 0 for e in en) and hp > 0
        held = stuck + sum(n * end_dmg(c) for (c, _), n in zip(groups, counts))
        hp_after = hp - (0 if win else max(0, incoming(en, plays.skills) + burn + held - block)
                         + regret * (len(state["hand"]) - plays.cards))
        escapes = sum(start[1][g] - counts[g] for g in escape_groups)
        sand_left = sandpit + escapes
        if sandpit and sand_left <= 1 and not win:  # ticks down at enemy turn start; at 0 it kills you
            hp_after = min(hp_after, 0)  # the Sandpit swallows you
        net_loss = me["hp"] - hp_after
        damage = sum(a[0] - b[0] for a, b in zip(start_en, en))
        weighted = sum(w * (a[0] - b[0]) for w, a, b in zip(weight, start_en, en))
        alive = [e[0] > 0 for e in en]
        future_left = sum(fu for fu, a in zip(future, alive) if a)
        projected = min(hp_after, me["hp"]) - sum(n for n, a in zip(next_turn, alive) if a)
        unknown = sum(1 for s in steps if not s[2])
        score = (W_WIN * win + W_KILL * kills + value_per_damage * weighted
                 - W_HP * net_loss * danger(projected, me["max_hp"])
                 - W_HP * FUTURE_WEIGHT * future_left * (not win)
                 + UNKNOWN_VALUE * unknown - W_DEATH * (hp_after <= 0) - pot_cost * pots
                 + (SANDPIT_VALUE * escapes / max(sand_left, 1) if sandpit else 0)
                 - DAZED_COST * sum(e[8] for e in en))
        return Plan(list(steps), damage, block - start[3], net_loss, kills, win, score)

    def allowed(card, plays, steps):
        """Play limits: Sloth cap, Smog (unplayable), Ringing (only as the first card of the turn),
        Bound (one per turn; ponytail: earlier Bound plays this turn aren't read)."""
        aff = card.get("affliction")
        return (plays.cards < cap and aff != "Smog" and not (aff == "Ringing" and before + plays.cards)
                and not (aff == "Bound" and any(s[3].get("affliction") == "Bound" for s in steps)))

    plans, seen = [], set()

    def dfs(st, steps):
        if st in seen:
            return
        seen.add(st)
        plans.append(evaluate(st, steps))
        energy, counts, en = st[0], st[1], st[2]
        for g, (card, _) in enumerate(groups):
            if counts[g] == 0 or (cost_of(card, st[6]) > energy and not card["x_cost"]) or (card["x_cost"] and energy == 0):
                continue
            if card["type"] != "Potion" and not allowed(card, st[6], steps):
                continue
            nxt = counts[:g] + (counts[g] - 1,) + counts[g + 1:]
            potion = card["type"] == "Potion"
            modeled = potion or junk(card) or (bool(set(card["vars"]) & MODELED) and set(card["vars"]) <= MODELED)
            label = ("POTION " if potion else "") + card["name"] + ("+" if card["upgraded"] else "")
            targets = [i for i, e in enumerate(en) if e[0] > 0] if card["target"] == "AnyEnemy" else [None]
            for t in targets:
                s2 = play(card, t, (st[0], nxt) + st[2:])
                dfs(s2, steps + [(label, names[t] if t is not None else None, modeled, card,
                                  foes[t].get("combat_id") if t is not None else None,
                                  "potion" if potion else "card")])

    dfs(start, [])
    plans.sort(key=lambda p: (-p.score, len(p.steps)))
    return plans[:top]
