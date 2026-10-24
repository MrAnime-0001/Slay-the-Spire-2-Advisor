"""One-turn card-sequence solver for the JSON state printed by reader.exe."""
import math
from dataclasses import dataclass

from deck import exchange_rate
from enemies import predict
from relics import (GAMEDATA, NO_PLAYS, Plays, Relics, after_play, block_mult, dmg_add, dmg_mult,
                    end_turn, x_bonus)

# Score weights. Tune these.
W_WIN, W_KILL, W_HP, W_DEATH = 100, 8, 2, 1000  # kills: tie-breaker; their saved hits are in the future terms
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
           "Energy", "HpLoss", "PoisonPower", "DoomPower", "Summon", "OstyDamage", "Stars"} | HEAL
MODELED = EFFECTS | {"Repeat"}
# Character mechanics (mechanics.md). Poison ticks at the enemy's turn start (can kill before it attacks), then
# keeps ticking 1 less each turn (that future damage counts POISON_FUTURE). Doom kills at the end of the enemy
# turn when HP <= Doom (it still attacks). Osty soaks unblocked attack damage; its HP is worth OSTY_HP of ours.
# Orbs: passives fire at our turn end (before enemies act); channeling into full slots evokes the oldest.
POISON_FUTURE, OSTY_HP, ETHEREAL_LOSS = 0.5, 0.4, 2
POST_FIGHT_HEAL = {"BurningBlood", "BlackBlood"}  # heal after every won fight (HealVar)
# Exhaust: a played Exhaust card is gone for the fight, so it costs its value x the times it would have been
# drawn again (turns left x 5 / deck size, at most REDRAW_CAP), discounted. Exhausting junk (Slimed...) is a
# small gain. Retain: a held Retain card is a sure play next turn, worth RETAIN_KEEP of its value.
REDRAW_CAP, EXHAUST_WEIGHT, EXHAUST_JUNK, RETAIN_KEEP, HAND = 2, 0.6, 2, 0.6, 5
ORB_HELD = {"Plasma": 6}  # an orb held at turn end: Plasma's +1 energy next turn; Dark = its stored evoke (below)
ORB_PASSIVE = {"Lightning": 3, "Frost": 2, "Dark": 6, "Plasma": 1, "Glass": 4}
ORB_EVOKE = {"Lightning": 8, "Frost": 5, "Plasma": 2}  # Dark: its grown value; Glass: 2x passive, to ALL
# ponytail: orb hits go to the first living enemy (the game picks at random); Osty's own Strength and
# "kill Osty" cards (Bone Shards) aren't modeled.
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


def keywords(card):
    """Live keywords when the reader has them (added Ethereal/Retain...), else the printed ones."""
    kw = card.get("keywords")
    return kw if kw is not None else GAMEDATA.get("cards", {}).get(card["name"], {}).get("keywords", [])


def star_cost(card):
    sc = card.get("star_cost")
    return sc if sc is not None and sc >= 0 else GAMEDATA.get("cards", {}).get(card["name"], {}).get("star_cost", 0)


def creates(card):
    """(card dict, count) this card puts into your hand when played, or None."""
    made = GAMEDATA.get("cards", {}).get(card["name"], {}).get("creates")
    if not made or card["type"] == "Potion":
        return None
    d = GAMEDATA["cards"].get(made[0])
    if not d:
        return None
    n = int(card["vars"].get(made[1], 1)) if made[1] else 1
    return ({"name": made[0], "upgraded": False, "type": d["type"], "target": d["target"], "cost": d["cost"],
             "x_cost": False, "vars": {k: int(x) for k, x in d["vars"].items()}, "rarity": d["rarity"],
             "keywords": d["keywords"]}, n)


def channels(card):
    return GAMEDATA.get("cards", {}).get(card["name"], {}).get("channel", [])


# Enemy powers the solver/predict read. Anything else on an enemy is shown in the overlay as "not modeled".
ENEMY_POWERS = {"StrengthPower", "VigorPower", "TerritorialPower", "VulnerablePower", "WeakPower", "SkittishPower",
                "HardenedShellPower", "SlipperyPower", "IntangiblePower", "ArtifactPower", "SlowPower",
                "PersonalHivePower", "EnragePower", "ShriekPower", "SandpitPower", "MinionPower", "CurlUpPower",
                "HardToKillPower", "FlutterPower", "SoarPower", "ThornsPower", "RitualPower", "PoisonPower",
                "DoomPower"}


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
                               "HexPower", "FocusPower", "AccuracyPower"}


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


def intent_damage(f, it, asc):
    """The intent's damage, or the code's formula for the attacks the reader can't read (closure calls a
    property): The Forgotten's Dread = 13 (15 at A9) + its Dexterity; Gas Bomb's explode = 8 (9 at A9)."""
    if it["damage"] is not None:
        return it["damage"]
    if f["name"] == "TheForgotten" and it["kind"] == "SingleAttackIntent":
        return (15 if asc >= 9 else 13) + amount(f["creature"]["powers"], "DexterityPower")
    if f["name"] == "GasBomb" and it["kind"] == "DeathBlowIntent":
        return 9 if asc >= 9 else 8
    return None


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
    # Turn-end cards (gamedata `turn_end`): Damage = blockable (Burn, Infection), HpLoss = unblockable (Beckon,
    # Bad Luck); playable ones (Beckon, Toxic) can be played to dodge it.
    turn_end = lambda c: GAMEDATA.get("cards", {}).get(c["name"], {}).get("turn_end") or junk(c) and "Damage" in c["vars"]
    end_dmg = lambda c: c["vars"].get("Damage", 0) if turn_end(c) else 0
    end_loss = lambda c: c["vars"].get("HpLoss", 0) if turn_end(c) else 0
    stuck = sum(end_dmg(c) for c in state["hand"] if junk(c) and c["cost"] < 0)
    stuck_loss = sum(end_loss(c) for c in state["hand"] if junk(c) and c["cost"] < 0)
    regret = sum(c["name"] == "Regret" for c in state["hand"])
    normality = any(c["name"] == "Normality" for c in state["hand"])
    enthralled = sum(c["name"] == "Enthralled" for c in state["hand"])
    def add(c, n=1):
        k = (c["type"] == "Potion", c["name"], c["upgraded"], c["cost"], c.get("affliction"), tuple(sorted(c["vars"].items())))
        if k not in keys:
            keys[k] = len(groups)
            groups.append([c, 0])
        groups[keys[k]][1] += n
        return keys[k]

    for c in playable + potion_actions(state):
        add(c)
    # Cards that put cards in hand (Blade Dance -> Shivs, Compact -> Fuel...): the made cards get a group (count 0)
    # so a plan can play them the same turn. makes[group] = (made group, how many).
    makes = {}
    for g, (c, _) in enumerate(list(groups)):
        made = creates(c)
        if made:
            makes[g] = (add(made[0], 0), made[1])
    deck_names = {c["name"] for c in state.get("deck") or state["hand"]}  # Exhaust only costs cards that come back

    # state: energy, group counts, enemies (hp, block, vuln, weak, skittish, shell left (-1 = none),
    # slippery, intangible, Dazed added to our deck, artifact, poison, doom), block, strength, dex, Plays,
    # potions used, our HP (after self-damage and heals), Osty HP, orbs ((name, value), ...), stars
    def pw(f, name):
        return amount(f["creature"]["powers"], name)

    start = (
        state["energy"],
        tuple(n for _, n in groups),
        tuple((f["creature"]["hp"], f["creature"]["block"], pw(f, "VulnerablePower"), pw(f, "WeakPower"),
               pw(f, "SkittishPower") + pw(f, "CurlUpPower"), pw(f, "HardenedShellPower") or -1, pw(f, "SlipperyPower"),
               pw(f, "IntangiblePower"), 0, pw(f, "ArtifactPower"), pw(f, "PoisonPower"), pw(f, "DoomPower"))
              for f in foes),
        me["block"],
        amount(me["powers"], "StrengthPower"),
        amount(me["powers"], "DexterityPower"),
        NO_PLAYS,
        0,
        me["hp"],
        next((p["creature"]["hp"] for p in state.get("pets", []) if p["name"] == "Osty"), 0),
        tuple((o["name"].removesuffix("Orb"), o.get("value")) for o in state.get("orbs", [])),
        state.get("stars", 0),
    )
    focus = amount(me["powers"], "FocusPower")
    slots = state.get("orb_slots", 0)
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
                base = intent_damage(f, it, state.get("ascension", 0))
                if base is not None:
                    per = math.floor((base + s + vigor) * (0.75 if e[3] > 0 else 1) * (1.5 if vuln else 1))
                    total += per * max(it["hits"], 1)
                    vigor = 0
            out.append(total)
        return out

    def incoming(en, skills=0):
        return sum(incoming_each(en, skills))

    # The Insatiable's Sandpit: a countdown that kills you at 0; each Frantic Escape played adds 1.
    sandpit = max((pw(f, "SandpitPower") for f in foes), default=0)
    escape_groups = [g for g, (c, _) in enumerate(groups) if c["name"] == ESCAPE]

    # Damage is worth what the full-fight sim's best pilot pays for it in HP (deck.exchange_rate); fallback: tempo
    # Ironclad's Burning Blood / Black Blood heal after a won fight; only the part that would overflow max HP
    # makes damage free (at 80/80 a 3 hit costs nothing; at 70/80 it does). Everyone else pays full price.
    after_heal = sum(GAMEDATA["relics"].get(r["name"], {}).get("vars", {}).get("Heal", 0)
                     for r in state.get("relics", []) if r["name"] in POST_FIGHT_HEAL)
    free_hp = max(0, min(after_heal, me["hp"] + after_heal - me["max_hp"]))
    lam = exchange_rate(state)
    value_per_damage = W_HP * lam if lam is not None else tempo(state, incoming(start[2]))
    turns_left = sum(f["creature"]["hp"] for f in foes) / deck_dpt(state)
    redraws = min(REDRAW_CAP, max(0.0, turns_left - 1) * HAND / max(len(state.get("deck") or state["hand"]), 1))

    def worth(c):  # rough value of one play of a card: its damage + half its Block, in score
        v = c["vars"]
        return value_per_damage * v.get("Damage", 0) * max(v.get("Repeat", 1), 1) + W_HP * 0.5 * v.get("Block", 0)

    def exhausts(c):
        return "Exhaust" in keywords(c) or (c["type"] == "Skill" and "CorruptionPower" in mine)
    forecast = [predict(f, state.get("ascension", 0), FUTURE_TURNS) for f in foes]
    future = [sum(t["dmg"] * FUTURE_DISCOUNT ** k for k, t in enumerate(fc)) for fc in forecast]
    next_turn = [fc[0]["dmg"] if fc else 0 for fc in forecast]
    threat = [n + fu for n, fu in zip(incoming_each(start[2]), future)]
    mean_threat = sum(threat) / max(len(threat), 1)
    weight = [min(THREAT_MAX, max(THREAT_MIN, t / mean_threat)) if mean_threat else 1 for t in threat]
    # Potions get cheaper when THIS turn's hits would leave us low. A big hit predicted for next turn
    # is a reason to keep the potion for that turn, not to spend it now.
    pot_cost = potion_cost(state, me["hp"] + me["block"] - incoming(start[2]))

    def orb_hit(en, n, everyone=False, lowest=False):
        """Orb damage: everyone (Glass), the lowest-HP enemy (Dark), else a random enemy = expected n/alive each."""
        alive = [i for i, e in enumerate(en) if e[0] > 0]
        if lowest and alive:
            alive = [min(alive, key=lambda i: en[i][0])]
        for i in alive:
            hit(en[i], n if everyone or lowest else n / len(alive))

    def evoke(orb, en, block, energy):
        name, val = orb
        if name in ("Lightning", "Dark"):
            orb_hit(en, (val or 6) if name == "Dark" else ORB_EVOKE[name] + focus, lowest=name == "Dark")
        elif name == "Glass":
            orb_hit(en, 2 * ((val or 4) + focus), everyone=True)
        elif name == "Frost":
            block += ORB_EVOKE["Frost"] + focus
        elif name == "Plasma":
            energy += ORB_EVOKE["Plasma"]
        return block, energy

    def play(card, tgt, st, g=None):
        energy, counts, en, block, strength, dex, plays, pots, hp, osty, orbs, stars = st
        potion = card["type"] == "Potion"
        cost = energy if card["x_cost"] else cost_of(card, plays)
        times = energy + x_bonus(R) if card["x_cost"] else 1
        v = {} if junk(card) else card["vars"]
        en = [list(e) for e in en]
        alive = [i for i, e in enumerate(en) if e[0] > 0]
        targets = alive if card["target"] in ("AllEnemies", "RandomEnemy") else [tgt] if tgt is not None else []
        # RandomEnemy: each hit lands on a random enemy, so each takes the expected share of the hits.
        share = 1 / len(targets) if card["target"] == "RandomEnemy" and targets else 1
        hp -= v.get("HpLoss", 0)  # self-damage (e.g. Bloodletting) happens on play
        heal = v.get("Heal", 0) + me["max_hp"] * v.get("HealPercent", 0) // 100 + v.get("RegenPower", 0)
        hp = min(me["max_hp"], hp + heal) if hp > 0 else hp  # ponytail: Regen counted as one tick now
        if "Damage" in v:
            if potion:  # potion damage ignores Strength/Weak
                base = v["Damage"]
            else:
                acc = mine.get("AccuracyPower", 0) if card["name"] == "Shiv" else 0  # Accuracy: Shivs hit harder
                base = (v["Damage"] + acc + strength + dmg_add(card, R)) * (0.75 if weak else 1) * dmg_mult(card, R, plays)
            for i in targets:
                slow = 1 + 0.1 * (before + plays.cards) if slowed[i] and not potion else 1  # Bygone Effigy
                mult = 1 if potion else halved[i] * shrink
                per = min(math.floor(base * (1.5 if en[i][2] > 0 else 1) * slow * mult), hard_cap[i])
                n_hits = max(v.get("Repeat", 1), 1) * times
                hits = [hit(en[i], per) for _ in range(n_hits)] if share == 1 else [hit(en[i], per * n_hits * share)]
                if thorns[i] and not potion:  # Thorns: our block soaks it first
                    back = thorns[i] * n_hits * share
                    soaked = min(block, back)
                    block -= soaked
                    hp -= back - soaked
                if sum(hits) and en[i][4] and not potion:  # Skittish: block after the first damaging attack
                    en[i][1] += en[i][4]
                    en[i][4] = 0
                if not potion:  # Personal Hive (Entomancer): each attack hit that lands adds Dazed
                    en[i][8] += hive[i] * sum(1 for h in hits if h)
        if "OstyDamage" in v and osty > 0:  # Osty attacks: no player Strength/Weak
            for i in targets:
                hit(en[i], math.floor(v["OstyDamage"] * (1.5 if en[i][2] > 0 else 1)))
        if "Summon" in v:  # raises Osty's HP, or revives it
            osty += v["Summon"]
        stars += v.get("Stars", 0) - (0 if potion else star_cost(card))
        for name in [] if potion else channels(card):
            if slots and len(orbs) >= slots:
                block, energy = evoke(orbs[0], en, block, energy)
                orbs = orbs[1:]
            if slots:
                orbs = orbs + ((name, {"Dark": 6, "Glass": 4}.get(name)),)
        if "Block" in v:
            gained = v["Block"] if potion else max(0, math.floor((v["Block"] + dex) * (0.75 if frail else 1))) * times
            block += gained * (1 if potion else block_mult(R, plays))
        if card["type"] == "Power" or card["target"] in ("Self", "None"):
            strength += v.get("StrengthPower", 0)
            dex += v.get("DexterityPower", 0)
            energy += v.get("Energy", 0)
        else:
            for i in targets:
                for slot, key in ((2, "VulnerablePower"), (3, "WeakPower"), (10, "PoisonPower"), (11, "DoomPower")):
                    if v.get(key):
                        if en[i][9] > 0:  # Artifact blocks one debuff per stack
                            en[i][9] -= 1
                        elif slot == 10:  # Bouncing Flask: Repeat bounces, split over the enemies it can hit
                            en[i][slot] += v[key] * max(v.get("Repeat", 1), 1) * times // max(len(targets), 1)                                 if card["target"] == "RandomEnemy" else v[key] * times
                        else:
                            en[i][slot] += v[key]
        energy -= cost
        if g in makes:  # made cards land in hand, playable later this turn
            mg, n = makes[g]
            counts = counts[:mg] + (counts[mg] + n * times,) + counts[mg + 1:]
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
        return (energy, counts, tuple(map(tuple, en)), block, strength, dex, plays, pots, hp, osty, orbs, stars)

    def evaluate(st, steps):
        _, counts, en, block, _, _, plays, pots, hp, osty, orbs, _ = st
        extra, relic_dmg = end_turn(R, block, len(state["hand"]) - plays.cards, state.get("turn", 0), plays)
        block += extra
        en = [list(e) for e in en]
        for e in en:
            if e[0] > 0:
                hit(e, relic_dmg)
        for name, val in orbs:  # orb passives at our turn end
            if name == "Lightning":
                orb_hit(en, ORB_PASSIVE["Lightning"] + focus)
            elif name == "Glass":
                orb_hit(en, (val or 4) + focus, everyone=True)
            elif name == "Frost":
                block += ORB_PASSIVE["Frost"] + focus
        poison_future = 0
        for e in en:  # poison ticks at the enemy's turn start: a kill here means no attack
            if e[0] > 0 and e[10] > 0:
                e[0] -= min(e[0], e[10])
                poison_future += min(e[0], e[10] * (e[10] - 1) // 2)
        if leaders and all(e[0] == 0 or m for e, m in zip(en, minion)):
            for e in en:
                e[0] = 0
        start_en = start[2]
        kills = sum(1 for a, b in zip(start_en, en) if a[0] > 0 and b[0] == 0)
        win = all(e[0] == 0 for e in en) and hp > 0
        held = stuck + sum(n * end_dmg(c) for (c, _), n in zip(groups, counts))
        attacks = 0 if win else incoming(en, plays.skills)
        soaked = min(osty, max(0, attacks - block))  # Osty takes unblocked attack damage first
        hp_after = hp - (0 if win else max(0, attacks + burn + held - block - soaked)
                         + regret * (len(state["hand"]) - plays.cards) + stuck_loss
                         + sum(n * end_loss(c) for (c, _), n in zip(groups, counts)))
        escapes = sum(start[1][g] - counts[g] for g in escape_groups)
        sand_left = sandpit + escapes
        if sandpit and sand_left <= 1 and not win:  # ticks down at enemy turn start; at 0 it kills you
            hp_after = min(hp_after, 0)  # the Sandpit swallows you
        net_loss = me["hp"] - hp_after
        doomed = [e[0] > 0 and e[11] >= e[0] for e in en]  # dies at the end of the enemy turn
        kills += sum(doomed)
        damage = sum(a[0] - b[0] for a, b in zip(start_en, en))
        weighted = sum(w * (a[0] - b[0] + (b[0] if d else 0)) for w, a, b, d in zip(weight, start_en, en, doomed))
        alive = [e[0] > 0 and not d for e, d in zip(en, doomed)]
        ethereal = sum(n for (c, _), n in zip(groups, counts) if not junk(c) and "Ethereal" in keywords(c))
        played = [(c, start[1][g] - counts[g]) for g, (c, _) in enumerate(groups) if c["type"] != "Potion"]
        exhaust_cost = sum(n * (-EXHAUST_JUNK if junk(c) else EXHAUST_WEIGHT * redraws * worth(c))
                           for c, n in played if n > 0 and exhausts(c) and (junk(c) or c["name"] in deck_names))
        retained = sum(n * RETAIN_KEEP * worth(c) for (c, _), n in zip(groups, counts)
                       if n and not junk(c) and "Retain" in keywords(c) and "Ethereal" not in keywords(c))
        future_left = sum(fu for fu, a in zip(future, alive) if a)
        projected = min(hp_after, me["hp"]) - sum(n for n, a in zip(next_turn, alive) if a)
        unknown = sum(1 for s in steps if not s[2])
        score = (W_WIN * win + W_KILL * kills + value_per_damage * weighted
                 - W_HP * max(0, net_loss - free_hp) * danger(projected, me["max_hp"])
                 - W_HP * FUTURE_WEIGHT * future_left * (not win)
                 + UNKNOWN_VALUE * unknown - W_DEATH * (hp_after <= 0) - pot_cost * pots
                 + (SANDPIT_VALUE * escapes / max(sand_left, 1) if sandpit else 0)
                 - DAZED_COST * sum(e[8] for e in en)
                 + value_per_damage * POISON_FUTURE * poison_future - W_HP * OSTY_HP * soaked
                 - ETHEREAL_LOSS * ethereal - exhaust_cost + retained
                 + sum(ORB_HELD.get(n, 0) + (value_per_damage * FUTURE_WEIGHT * ((v or 6) + ORB_PASSIVE["Dark"] + focus)
                                             if n == "Dark" else 0) for n, v in orbs))
        return Plan(list(steps), damage, block - start[3], net_loss, kills, win, score)

    def allowed(card, plays, steps):
        """Play limits: Sloth cap, Smog (unplayable), Ringing (only as the first card of the turn),
        Bound (one per turn; ponytail: earlier Bound plays this turn aren't read)."""
        aff = card.get("affliction")
        if normality and before + plays.cards >= 3:  # Normality in hand: max 3 cards this turn
            return False
        if card["name"] != "Enthralled" and enthralled > sum(s[3]["name"] == "Enthralled" for s in steps):
            return False  # Enthralled in hand must be played before anything else
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
            if card["type"] != "Potion" and star_cost(card) > st[11]:
                continue
            if card["type"] != "Potion" and not allowed(card, st[6], steps):
                continue
            nxt = counts[:g] + (counts[g] - 1,) + counts[g + 1:]
            potion = card["type"] == "Potion"
            modeled = potion or junk(card) or ((bool(set(card["vars"]) & MODELED) or bool(channels(card)))
                                               and set(card["vars"]) <= MODELED)
            label = ("POTION " if potion else "") + card["name"] + ("+" if card["upgraded"] else "")
            targets = [i for i, e in enumerate(en) if e[0] > 0] if card["target"] == "AnyEnemy" else [None]
            for t in targets:
                s2 = play(card, t, (st[0], nxt) + st[2:], g)
                dfs(s2, steps + [(label, names[t] if t is not None else None, modeled, card,
                                  foes[t].get("combat_id") if t is not None else None,
                                  "potion" if potion else "card")])

    dfs(start, [])
    plans.sort(key=lambda p: (-p.score, len(p.steps)))
    return plans[:top]
