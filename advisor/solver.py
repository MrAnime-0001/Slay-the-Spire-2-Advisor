"""One-turn card-sequence solver for the JSON state printed by reader.exe."""
import collections
import functools
import time
import math
import random
import re
from dataclasses import dataclass, replace as dc_replace

from archetypes import card_text, expand_power
from enemies import MONSTERS, ally_strength, attacks, follow, predict, replay_moves, summon_dmg
from relics import (GAMEDATA, NO_PLAYS, Plays, Relics, after_play, block_mult, dmg_add, dmg_mult,
                    end_turn, relic_draws, x_bonus)

# Score weights. Tune these.
W_WIN, W_KILL, W_HP, W_DEATH = 100, 8, 2, 1000  # kills: tie-breaker; their saved hits are in the future terms
UNKNOWN_VALUE = 2
HAND_MAX, DEATH_HP = 10, 10 ** 6  # hand size limit; The Gambit's "die" as HP lost  # flat credit for effects we don't model (draw, exhaust, most powers)
# HP matters more as it gets low: below LOW_HP_FRACTION of max, each HP lost costs up to
# (1 + LOW_HP_WEIGHT)x more, so a low-HP character blocks/heals instead of trading.
LOW_HP_FRACTION, LOW_HP_WEIGHT = 0.5, 3
# Tempo: damage is worth the HP a shorter fight saves. Each point removes 1/(our damage per turn) of a fight turn,
# and each enemy turn costs what our usual next hand can't block (mean over the forecast, so scaling enemies count).
# Blocking real hits beats damage that removes no turn. DMG_FLOOR: an enemy turn's hit when nothing is known about it
# (no AI table, no attack intent now): every enemy attacks eventually. DMG_MIN keeps attacking above idling.
# VPD_CAP: a damage point counts as at most this much HP saved: damage that doesn't remove a whole enemy turn is
# partly wasted (kills are valued on their own: W_KILL, future_left). Lowest in hallways: block there, chip less.
DMG_FLOOR, DMG_MIN, DPT_FLOOR = 6, 0.5, 5
VPD_CAP = {"Monster": 0.5, "Elite": 0.8, "Boss": 0.8}  # hallway 0.3 halves chip trades but deals 6% less damage
# HP is worth most in hallway fights (lost HP turns later rest sites from Smith into Rest and weakens the elite/boss
# fights) and least in a boss fight (the act ends). Multiplies W_HP for HP lost, future hits and potion cost.
HP_WEIGHT = {"Monster": 1.5, "Elite": 1.25, "Boss": 0.75}
# Boss HP relief (as CombatSolver's ActEndingBossPolicy): after an Act 1/2 boss the next Ancient heals missing HP
# (all of it; 80% from A2 Weary Traveler, AncientEventModel.cs:180), so 0.75 above. The run's last fight: HP left
# is worth nothing beyond surviving it (danger() and W_DEATH still apply). A10's first of two final bosses: HP
# carries into the second, priced like an elite.
# ponytail: 3 fixed weights; scale by the unhealed share if A2+ act bosses need it.
FINAL_BOSS_HP, A2_BOSS_HP, WEARY_TRAVELER = 0.5, 0.9, 2
# Score charged per potion (W_HP=2, so 40 = "worth ~20 HP"): saved in hallway fights for later
# encounters, spent on bosses. Full slots halve it (an unused potion blocks new drops). Below
# POTION_FREE_HP of max HP the cost scales to the minimum: drink rather than die.
POTION_COST = {"Monster": 40, "Elite": 20, "Boss": 8}
FULL_SLOTS_FACTOR, MIN_POTION_COST, POTION_FREE_HP = 0.5, 4, 0.3
HEAL = {"Heal", "HealPercent", "RegenPower"}
EFFECTS = {"Damage", "Block", "StrengthPower", "DexterityPower", "VulnerablePower", "WeakPower",
           "Energy", "HpLoss", "PoisonPower", "DoomPower", "Summon", "OstyDamage", "Stars",
           "EnemyStrength", "StrengthLoss"} | HEAL
MODELED = EFFECTS | {"Repeat"}
# Spite hits Repeat times only if you lost HP this turn. ponytail: only HP lost inside the plan counts (Bloodletting,
# Hemokinesis...); HP lost earlier this turn isn't in the state, so a replan after it counts 1 hit (under, not over).
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
# Piles (research/piles.md): we know the draw pile's contents, not its order. Next turn's hand is sampled
# EXPOSURE_SAMPLES times from it to estimate how much of next turn's predicted damage we can block.
PILE_VARS, EXPOSURE_SAMPLES = {"Cards", "PutBack", "WisdomCards"}, 40  # draw/fetch counts, not effects
# Enemy powers (research/effects-audit.md). Killing these doesn't end the fight (their ShouldStopCombatFromEnding):
# Waterfall Giant erupts for its Steam Eruption at the end of our next turn, Test Subject revives, Axebot restocks,
# Phrog Parasite bursts into Wrigglers, Gremlin Merc's Surprise spawns gremlins.
REVIVE_POWERS = {"SteamEruptionPower", "AdaptablePower", "StockPower", "InfestedPower", "SurprisePower"}
# Not in it (the fight ends when everything is dead): Illusions revive only while their leader lives; a Decimillipede
# segment killed while another lives reattaches (heals ReattachPower at its turn end), so that's no kill.
NO_END_POWERS = {"IllusionPower", "ReattachPower"}
BACK_ATTACK = 1.5  # Surrounded: hits from behind


def behind(f, facing):
    """True if enemy f is behind us (we face `facing`, it attacks from the other side)."""
    return (facing == "Right" and amount(f["creature"]["powers"], "BackAttackLeftPower") > 0
            or facing == "Left" and amount(f["creature"]["powers"], "BackAttackRightPower") > 0)


CRAB_RAGE_STR, CRAB_RAGE_BLOCK = 6, 99  # Crusher/Rocket: the survivor gains these when its partner dies (at once)
UNKILLABLE = 10 ** 8
ERUPT_STEP = 3  # Waterfall Giant: every move after Pressurize adds 3 Steam Eruption (WaterfallGiant.cs, both builds)
RESUMMON_TURNS = 1  # forecast turns a summoner's minion still counts: a killed one is replaced soon after
MINION_CHIP = 0.3  # damage weight on a summoner's minion (kills still count in full)  # a dead Waterfall Giant waiting to erupt sits at 999999999 HP: damage to it is wasted
# Thieves: a stolen card (Swipe, Thieving Hopper) or gold (Heist, Fat Gremlin) comes back only if the thief dies
# before it escapes; Gremlin Merc's Thievery steals more gold each attack. Valued like pathing: a card reward
# (4) / HP (0.3) ~ 13 HP; 6 per 100 gold -> 0.2 HP a gold.
CARD_LOOT_HP, GOLD_LOOT_HP = 13, 0.2
# The stolen card (reader: SwipePower `stolen`) is valued like the card picker rates it for this deck (build fit,
# synergies): LOOT_CARD_SCORE rates = CARD_LOOT_HP; a key build card is worth fighting for, a stolen Strike isn't.
LOOT_CARD_SCORE, LOOT_CARD_RANGE = 10, (0.15, 3)
STUN_KEEP = 0.5  # a stunned thief that was escaping now: share of its loot counted as kept (one more turn to kill it)


def card_loot_hp(f, state):
    """HP-equivalent of getting this thief's stolen card back (0 when it holds none)."""
    sw = next((p for p in f["creature"]["powers"] if p["name"] == "SwipePower"), None)
    if not sw:
        return 0
    card = sw.get("stolen")
    if not card or card.get("name") not in GAMEDATA["cards"]:
        return CARD_LOOT_HP  # older reader: unknown card
    from picker import rate  # picker imports solver
    score = rate(card, state.get("deck") or [], state.get("relics") or [], state)[0]
    lo, hi = LOOT_CARD_RANGE
    return CARD_LOOT_HP * min(hi, max(lo, score / LOOT_CARD_SCORE))
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
THREAT_TURNS_MAX, THREAT_DISCOUNT = 5, 0.8  # threat weight's own, longer forecast
# Lasting Strength/Dexterity (Inflame, Footwork, Demon Form each turn): worth the extra damage/Block of the attacks/
# Block cards a hand holds, over the fight's remaining turns (at most BUFF_TURNS), discounted like foresight.
BUFF_TURNS = 3
# Engine Powers (W4): what they add per later turn, per point of their amount, over the fight's remaining turns
# (at most ENGINE_TURNS). kind: "dmg" (one enemy), "all" (each enemy), "block". ramp: it builds up each turn
# (Noxious Fumes' Poison stacks, Countdown's Doom), so turn k is worth k times the first.
ENGINE_TURNS = 6
ENGINES = {"NoxiousFumesPower": ("all", 1, True), "InfernoPower": ("all", 1, False),  # Inferno: 1 HP each turn
           "InfiniteBladesPower": ("dmg", 4, False), "CountdownPower": ("dmg", 1, True),
           "SerpentFormPower": ("dmg", 3.5, False), "EnvenomPower": ("dmg", 2, True),  # ~cards / hits per turn
           "LethalityPower": ("dmg", 0.07, False), "EchoFormPower": ("dmg", 6, False),  # 50% / one card of ~7
           "RollingBoulderPower": ("all", 1, True), "HailstormPower": ("all", 0.5, False),
           "UnmovablePower": ("block", 5, False), "FastenPower": ("block", 1, False), "ShroudPower": ("block", 1, False),
           # P3, ponytail: guessed triggers per turn (~5 draws, ~1 Soul, ~1 debuff, ~4 damage per extra card)
           "SpeedsterPower": ("all", 5, False), "PanachePower": ("all", 0.8, False), "BlackHolePower": ("all", 1.5, False),
           "SmokestackPower": ("all", 0.5, False), "NecroMasteryPower": ("all", 5, False),
           "SleightOfFleshPower": ("dmg", 1, False), "HauntPower": ("dmg", 1, False), "PrepTimePower": ("dmg", 1, False),
           "MachineLearningPower": ("dmg", 4, False), "SpectrumShiftPower": ("dmg", 4, False),
           "GenesisPower": ("dmg", 1.5, False), "LoopPower": ("dmg", 3, False), "WellLaidPlansPower": ("dmg", 2, False)}


def engine_powers(card):
    """[(power, amount)] of the ENGINES a card gives us, from its ops (amount from its live vars)."""
    if card.get("ops"):  # from_ops moved the amounts out of vars into op_buffs
        return [(p, n) for p, n in card["op_buffs"].items() if p in ENGINES]
    ops = GAMEDATA["cards"].get(card["name"], {}).get("ops") or []
    return [(o["power"], o["amt"] if isinstance(o["amt"], (int, float)) else card["vars"].get(o["amt"], 0))
            for o in ops if o.get("op") == "power" and o.get("tgt") == "self" and o.get("power") in ENGINES]
# The Insatiable: Frantic Escape (Status, 1 energy) pushes its Sandpit countdown back by 1. Worth more the
# lower the countdown; letting it reach 0 kills you.
ESCAPE, SANDPIT_VALUE = "FranticEscape", 12
# It's a race (321/341 HP vs a countdown of 4): HP the pit owners would still have when the countdown ends, at our
# deck's damage per turn, is a loss. So an Escape is worth a turn of our damage while the race is short.
# ponytail: one deck_dpt per turn, no ramp-up from Powers/Strength; scale it with the fight if escapes run short.
SANDPIT_RACE = 1
# Curses held at turn end that debuff us (Doubt.cs / Shame.cs): Weak / Frail 1 for next turn, a quarter of next
# turn's attack / block output each.
DEBUFF_CURSES, DEBUFF_CUT = {"Doubt": "WeakPower", "Shame": "FrailPower"}, 0.25
DAZED_COST = 1.5  # score per Dazed shuffled into our deck (Entomancer's Personal Hive): prefer big hits
# P4 enemy powers (decompiled). Nemesis: Intangible every other enemy turn end, so an enemy without it now has it
# next turn: damage now counts NEMESIS_NOW x. Hatch at 1: hatches after its next turn: kill it now (HATCH_NOW x).
# Suck: +X Strength per attack that gets through our Block (future HP, SUCK_TURNS turns of its hits).
# Asleep (Lagavulin Matriarch) / Slumber (Slumbering Beetle): unblocked damage wakes them early (WAKE_COST HP).
NEMESIS_NOW, HATCH_NOW, SUCK_TURNS, WAKE_COST = 1.5, 2, 2, 8  # ponytail: rough, tune from recorded fights
# ponytail: plays one turn of cards; ignores draw and card generation. Of power cards' future value only lasting
# Strength/Dexterity counts (lasting() below).
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
    energy_left: int = 0
    guessed: bool = False  # totals include a card potion's stand-in card (the real cards are offered later)
    end: tuple = ()  # each enemy's slots after the enemy turn (HP [0], Vulnerable [2], Weak [3], Poison [10]): lookahead
    drawn: int = 0  # cards the plan draws this turn (they leave the top of the draw pile)
    plays: int = 0  # cards played (Pocketwatch)
    on_top: tuple = ()  # cards the plan puts on top of the draw pile (Headbutt, pick screens): the next hand's first


def amount(powers, name):
    return next((p["amount"] for p in powers if p["name"] == name), 0)


def keywords(card):
    """Live keywords when the reader has them (added Ethereal/Retain...), else the printed ones."""
    kw = card.get("keywords")
    kw = kw if kw is not None else GAMEDATA.get("cards", {}).get(card["name"], {}).get("keywords", [])
    return kw + ["Ethereal"] if card.get("affliction") == "Hexed" and "Ethereal" not in kw else kw  # Spectral Knight


@functools.lru_cache(maxsize=4096)
def timing(name, key):
    """(sign, when) for a card's own Strength/Dexterity/Energy var, from its text: "now" (and it lasts), "this turn",
    "turn" (Demon Form: at the start of each later turn), "later" (next turn), "trigger" (Rupture: whenever...),
    "if" (Forgotten Ritual v0.107.1), "ally" (another player), "choice" (Mad Science). Sign -1: "Lose X Strength"."""
    text = card_text(name)
    if "choose(" in text:
        return 1, "choice"
    s = next((x for x in re.split(r"(?<=\.)\s+|\n", text) if "{" + key in x), "")
    low = s.lower()
    sign = -1 if low.startswith("lose") else 1
    for word, when in (("another player", "ally"), ("start of your turn", "turn"), ("start of each turn", "turn"),
                       ("next turn", "later"), ("whenever", "trigger"), ("if you", "if"), ("this turn", "this turn")):
        if word in low:
            return sign, when
    return sign, "now"


def exhausts_others(card):
    """True Grit, Burning Pact, Brand, Second Wind, Fiend Fire...: the card text exhausts cards from hand."""
    return bool(re.search(r"\bExhaust (1|\{|all|your)", card_text(card["name"])))


_turn_exhaust = {}  # turn -> smallest exhaust-pile size seen that turn


def exhausted_this_turn(state):
    """Cards exhausted earlier this turn (before this plan): the reader's count from the game's combat history.
    Older readers: the exhaust pile's growth since the turn started (undercounts after an advisor restart mid-turn)."""
    if state.get("exhausted_this_turn") is not None:
        return state["exhausted_this_turn"]
    turn, pile = state.get("turn"), state.get("exhaust")
    if turn is None or pile is None:
        return 0
    if _turn_exhaust.get("turn") != turn:
        _turn_exhaust.update(turn=turn, base=len(pile))
    _turn_exhaust["base"] = min(_turn_exhaust["base"], len(pile))  # a new fight resets the pile
    return len(pile) - _turn_exhaust["base"]


def deferred(card):
    """A self-gain the solver doesn't count this turn (Rupture's trigger, next-turn or per-turn Energy, a choice):
    the play keeps the unmodeled credit instead of looking worthless."""
    if card.get("ops"):
        return False  # read from the code: everything in its vars lands now
    return any(when in ("trigger", "later", "choice") or (key == "Energy" and when == "turn")
               for key in ("StrengthPower", "DexterityPower", "Energy") if key in card["vars"]
               for _, when in [timing(card["name"], key)])


def star_cost(card):
    sc = card.get("star_cost")
    return sc if sc is not None and sc >= 0 else GAMEDATA.get("cards", {}).get(card["name"], {}).get("star_cost", 0)


def creates(card):
    """(card dict, count) this card puts into your hand when played, or None."""
    made = GAMEDATA.get("cards", {}).get(card["name"], {}).get("creates") or (
        ["Shiv", None] if card["name"] == "StormOfSteel" else None)  # a Shiv per card discarded (solve.play)
    if not made or card["type"] == "Potion":
        return None
    d = GAMEDATA["cards"].get(made[0])
    if not d:
        return None
    n = int(card["vars"].get(made[1], 1)) if made[1] else 1
    return gd_card(made[0]), n


def gd_card(name, upgraded=False, free=False):
    """A card dict built from gamedata (generated cards: Shivs, potion picks...)."""
    d = GAMEDATA["cards"][name]
    v = {k: int(x) for k, x in d["vars"].items()}
    for k, x in (GAMEDATA["upgrades"].get(name, {}).get("vars", {}) if upgraded else {}).items():
        v[k] = v.get(k, 0) + int(x)
    return {"name": name, "upgraded": upgraded, "type": d["type"], "target": d["target"],
            "cost": 0 if free else d["cost"], "x_cost": False, "vars": v, "rarity": d["rarity"], "keywords": d["keywords"]}


# Card effects read from the game code (gamedata `ops`, tools/gen_gamedata.parse_ops): what OnPlay does, in order,
# with amounts named by DynamicVars key, read from the live card (upgrades, buffs, beta changes all follow).
ENEMY_DEBUFFS = {"VulnerablePower", "WeakPower", "PoisonPower", "DoomPower"}
DEBUFF_SLOT = {"VulnerablePower": 2, "WeakPower": 3, "PoisonPower": 10, "DoomPower": 11}  # enemy row slots
# Count cards' output when it isn't hits (Repeat) or Block: Doom (No Escape), channels (Voltaic), draws (Compile Driver)
COUNT_KEY = {"NoEscape": "DoomPower", "Voltaic": "Channels", "CompileDriver": "Cards"}
# Count cards the solver plays by name (solve.counted / play): modeled even without effect vars, so no flat credit
COUNTED = {"Dualcast", "Quadcast", "Shatter", "Darkness", "TeslaCoil", "Voltaic", "StormOfSteel", "FiendFire", "Stardust",
           "Hang", "MoltenFist", "KnifeTrap", "Sacrifice"}
NEEDS_ORBS = {"Dualcast", "Quadcast", "Synchronize"}  # do nothing without an orb: never worth a play then
HAND_TOSS = {"FiendFire": "exhaust", "Stoke": "exhaust", "StormOfSteel": "discard"}  # the whole hand goes first
OSTY_ATTACKS = {n for n, d in GAMEDATA.get("cards", {}).items() if any(o.get("by") == "osty" for o in d.get("ops") or [])}
OP_VARS = {"block": "Block", "energy": "Energy", "stars": "Stars", "summon": "Summon",
           "heal": "Heal", "hp_loss": "HpLoss", "orb_slots": "SlotsGained"}
OP_PASS = {"channel", "create", "draw"}  # handled from other gamedata fields (channel list, creates, draws)


def from_ops(card):
    """The card with its vars rewritten from its ops into the solver's effect keys (Uppercut's generic Power ->
    WeakPower + VulnerablePower, hit counts -> Repeat, self vs enemy Strength) and ops=True; buffs it gives us in
    op_buffs. Any op the solver can't play yet (a pick, exhaust, unknown power...) -> the old var path."""
    d = GAMEDATA.get("cards", {}).get(card["name"], {})
    ops = d.get("ops_upgraded") if card.get("upgraded") and "ops_upgraded" in d else d.get("ops")
    if not ops or card["type"] not in ("Attack", "Skill", "Power") or card["target"] in ("AnyAlly", "AllAllies"):
        return expand_power(card)  # ally cards (Blaze): cardPlay.Target is another player
    live = card["vars"]

    def val(a):
        if isinstance(a, (int, float)):
            return a
        neg = isinstance(a, str) and a.startswith("-")
        k = (a[1:] if neg else a).removeprefix("calc:")  # Barrage's hits: solve.counted() recounts them
        if k not in live:
            raise LookupError(a)
        used.add(k)
        return -live[k] if neg else live[k]

    v, buffs, used, toss = {}, {}, set(), []

    def add(key, n):
        v[key] = v.get(key, 0) + n
    try:
        for o in ops:
            op = o["op"]
            if op == "damage":
                hits = 1 if o["hits"] == "X" else val(o["hits"])
                key = "OstyDamage" if o.get("by") == "osty" else "Damage"
                if key in v and v[key] != val(o["amt"]):
                    return expand_power(card)  # two different hits: not one Damage x Repeat
                v[key] = val(o["amt"])
                if key == "Damage":
                    add("Repeat", hits)
            elif op in OP_VARS:
                add(OP_VARS[op], val(o["amt"]))
            elif op == "power" and o["tgt"] == "self" and (o["power"] in ("StrengthPower", "DexterityPower")):
                add(o["power"], val(o["amt"]))
            elif op == "power" and o["tgt"] == "self" and o["power"] in BUFFS:
                buffs[o["power"]] = buffs.get(o["power"], 0) + val(o["amt"])
            elif op == "power" and o["tgt"] != "self" and o["power"] in ENEMY_DEBUFFS:
                add(o["power"], val(o["amt"]))
            elif op == "power" and o["tgt"] != "self" and o["power"] in ("StranglePower", "OblivionPower"):
                add(o["power"], val(o["amt"]))  # per later card this turn: HP loss / Doom (solver.play)
            elif op == "power" and o["tgt"] != "self" and o["power"] == "StrengthPower":
                n = val(o["amt"])
                add("EnemyStrength" if n > 0 else "StrengthLoss", abs(n))
            elif op == "select" and o["from"] == "Hand" and o["to"] in ("Discard", "Exhaust"):
                toss.append((o["to"].lower(), int(val(o["count"])), "pick"))  # we pick: the worst card (toss_key)
            elif op in ("exhaust", "discard") and o["which"] == "random":
                toss.append((op, 1, "random"))  # True Grit: a random hand card
            elif op not in OP_PASS:
                return expand_power(card)
    except LookupError:
        return expand_power(card)
    if v.get("Repeat") == 1:
        del v["Repeat"]
    # other vars stay (draw/create counts: Cards, Shivs...); effect vars come only from the ops
    v.update({k: x for k, x in live.items() if k not in used and k not in MODELED and k != "Power"})
    return {**card, "vars": v, "ops": True, "op_buffs": buffs, "op_toss": toss}


def toss_key(c, discard, value):
    """Order of cards to give up when a card asks for a discard/exhaust: Curse/Status first, then (discards) Sly cards,
    which get played for free, then the lowest value. Shared by the solver and the pick screens (picker.hand_pick)."""
    return (c["type"] not in ("Curse", "Status") or c["name"] == ESCAPE,  # Frantic Escape: a Sandpit escape, keep it
            not (discard and "Sly" in keywords(c)), value(c))


# Potions that put cards in hand (research/effects-audit.md). The offered/random cards aren't known when planning,
# so a stand-in from the pool is used: choose-1-of-3 potions take the 75th-percentile card (best of 3 random ~ the
# 75th percentile), random ones the median, by printed value. The real pick is made on the choose-a-card screen.
# Entries: (card types or None, pool ("own" = our character's), quantile, free this turn, upgraded, how many).
CARD_POTIONS = {
    "AttackPotion": [(("Attack",), "own", 0.75, True, False, 1)],
    "SkillPotion": [(("Skill",), "own", 0.75, True, False, 1)],
    "PowerPotion": [(("Power",), "own", 0.75, True, False, 1)],
    "ColorlessPotion": [(None, "Colorless", 0.75, True, False, 1)],
    "OrobicAcid": [((t,), "own", 0.5, True, False, 1) for t in ("Attack", "Skill", "Power")],
    "CosmicConcoction": [(None, "Colorless", 0.5, False, True, 3)],
}
FIXED_POTION_CARDS = {"CunningPotion": ("Shiv", True), "PotOfGhouls": ("Soul", False)}  # count = potion's Cards
CHARACTERS = {"Ironclad", "Silent", "Defect", "Necrobinder", "Regent"}


def printed_value(c):
    v = c["vars"]
    return (v.get("Damage", 0) * max(v.get("Repeat", 1), 1) + v.get("Block", 0)
            + 4 * (v.get("StrengthPower", 0) + v.get("DexterityPower", 0)) + 5 * len(channels(c)))


def potion_cards(name, state):
    """[(card, count)] a card-making potion puts in hand (stand-ins for offered/random cards)."""
    if name in FIXED_POTION_CARDS:
        made, up = FIXED_POTION_CARDS[name]
        return [(gd_card(made, up), int(GAMEDATA["potions"][name]["vars"].get("Cards", 1)))]
    pools = collections.Counter(GAMEDATA["cards"].get(c["name"], {}).get("pool") for c in state.get("deck") or state["hand"])
    own = next((p for p, _ in pools.most_common() if p in CHARACTERS), None)
    out = []
    for types, pool, q, free, up, n in CARD_POTIONS.get(name, ()):
        pool = own if pool == "own" else pool
        cands = sorted((c for c in (gd_card(k, up, free) for k, d in GAMEDATA["cards"].items()
                                    if d.get("pool") == pool and d["rarity"] not in ("Basic", "Ancient")
                                    and d["cost"] >= 0 and (types is None or d["type"] in types))
                        if set(c["vars"]) <= MODELED and (set(c["vars"]) & EFFECTS or channels(c))), key=printed_value)
        if cands:  # stand_in: valued in the plan but never shown or played (the game offers other cards)
            out.append(({**cands[int(q * (len(cands) - 1))], "stand_in": True}, n))
    return out


def pile_moves(card):
    """(cards drawn, fetch {from, to, top, count, filter} or None) from gamedata (research/piles.md)."""
    d = GAMEDATA.get("potions" if card["type"] == "Potion" else "cards", {}).get(card["name"], {})
    drawn = int(card["vars"].get(d["draws"], 0)) if d.get("draws") else 0
    f = d.get("fetch")
    if f and not isinstance(f["count"], int):
        f = {**f, "count": int(card["vars"].get(f["count"], 1))}
    return drawn, f


def by_name(cards):
    return sorted(cards, key=lambda c: (c["name"], c["upgraded"]))


DRAW_POWERS = {"DrawCardsNextTurnPower": 1, "MachineLearningPower": 1, "ToolsOfTheTradePower": 1, "TyrannyPower": 1,
               "DemesnePower": 1, "MindRotPower": -1}  # x Amount (ModifyHandDraw); Clarity is a flat +1


def next_draw(state, played=0):
    """Cards drawn next turn (every ModifyHandDraw in the decomp, both builds): HAND + Pael's Blood, Snecko Eye,
    Fiddle; Ring of the Drake (first N turns); Pendulum (every Nth turn) and Pollinous Core (its Nth turn) from their
    counters; Pocketwatch if at most CardThreshold cards are played this turn (played: the plan's plays);
    Predator / Machine Learning / Tools of the Trade / Tyranny / Demesne / Clarity, minus Mind Rot.
    Turn-1-only relics (Ring of the Snake, Bag of Preparation...) never touch a next hand."""
    R = Relics(state.get("relics", []), GAMEDATA["relics"])
    n = HAND + sum(R.var(r, "Cards") for r in ("PaelsBlood", "SneckoEye", "Fiddle") if r in R)
    nxt = (state.get("turn") or 0) + 1
    if "RingOfTheDrake" in R and nxt <= R.var("RingOfTheDrake", "Turns"):
        n += R.var("RingOfTheDrake", "Cards")
    if "Pendulum" in R and (R.count("Pendulum", "_turnsSeen") + 1) % int(R.var("Pendulum", "Turns") or 1) == 0:
        n += R.var("Pendulum", "Cards")
    if "PollinousCore" in R and R.count("PollinousCore", "_turnsSeen") + 1 >= R.var("PollinousCore", "Turns"):
        n += R.var("PollinousCore", "Cards")
    if "Pocketwatch" in R and R.count("Pocketwatch", "_cardsPlayedThisTurn") + played <= R.var("Pocketwatch", "CardThreshold"):
        n += R.var("Pocketwatch", "Cards")
    powers = state.get("player", {}).get("powers", [])
    n += sum(k * amount(powers, p) for p, k in DRAW_POWERS.items()) + (amount(powers, "ClarityPower") > 0)
    return max(0, int(n))


def draw_hand(state, k, rng):
    """(next hand, draw pile left, discard left) for a k-card draw. The reader's draw pile is in the game's real
    order (draw_ordered; the user chose knowing over guessing, 2026-10-25): the top k cards, exactly. Without it
    (older readers, recordings): k random ones, sorted by name first so the order never matters. Fewer than k
    left: all of them, then the reshuffled discard + this hand: exactly (reshuffle) with the reader's shuffle RNG,
    else random ones."""
    ordered = state.get("draw_ordered")
    draw, pool = (list(state["draw"]) if ordered else by_name(state["draw"])), by_name(state.get("discard", []) + state["hand"])
    if len(draw) >= k:
        if ordered:
            return draw[:k], draw[k:], pool
        got = set(rng.sample(range(len(draw)), k))  # by index: identical cards can be the same dict
        return [draw[i] for i in got], [c for i, c in enumerate(draw) if i not in got], pool
    if ordered and (new := reshuffle(state, pool)) is not None:
        n = k - len(draw)
        return draw + new[:n], new[n:], []
    got = set(rng.sample(range(len(pool)), min(k - len(draw), len(pool))))
    return draw + [pool[i] for i in got], [c for i, c in enumerate(pool) if i not in got], []


def reshuffle(state, cards):
    """The draw pile the game makes from `cards` when it reshuffles (CardPileCmd.Shuffle, both builds): StableShuffle
    sorts by (ModelId.Entry, upgrade level) then Fisher-Yates with RunState.Rng.Shuffle (the reader's live state);
    a Perfect Fit card goes on top. None without the RNG state or card ids (older readers)."""
    if not state.get("shuffle_rng") or any(c.get("id") is None for c in cards):
        return None
    from pathing import MegaRng  # lazy: pathing imports deck / runplan
    rng, out = MegaRng.at(state["shuffle_rng"]), sorted(cards, key=lambda c: (c["id"], c.get("level", 0)))
    for i in range(len(out) - 1, 0, -1):
        j = rng.next_int(i + 1)
        out[i], out[j] = out[j], out[i]
    return sorted(out, key=lambda c: (c.get("enchantment") or {}).get("name") != "PerfectFit")  # stable: top only


def keeps_hand(state):
    """The hand isn't discarded at turn end (ShouldFlush false): Runic Pyramid, Ringing Triangle on turn 1, Retain
    Hand. ponytail: Well-Laid Plans (keeps only the cards picked) isn't modelled."""
    names = {r["name"] for r in state.get("relics", [])}
    return ("RunicPyramid" in names or ("RingingTriangle" in names and (state.get("turn") or 0) <= 1)
            or amount(state.get("player", {}).get("powers", []), "RetainHandPower") > 0)


def end_discard(state, plan):
    """The discard pile at the end of this turn after `plan`: the discard now + this hand's cards except played
    Powers / Exhaust cards and unplayed Ethereal (exhausted) or Retain (stay in hand) ones, plus a card still
    resolving (play_pile).
    ponytail: cards the plan makes (Shivs) and Retain from relics (Runic Pyramid) aren't tracked."""
    played = collections.Counter(st[3]["name"] for st in plan.steps if st[5] == "card")
    keep = keeps_hand(state)
    out = list(state.get("discard", []))
    for c in state.get("play_pile") or []:  # a card still resolving (reader's play_pile) lands like a played one
        if c["type"] != "Power" and "Exhaust" not in keywords(c):
            out.append(c)
    for c in state["hand"]:
        kw = keywords(c)
        if played[c["name"]] > 0:
            played[c["name"]] -= 1
            if c["type"] != "Power" and "Exhaust" not in kw:
                out.append(c)
        elif "Ethereal" not in kw and "Retain" not in kw and not keep:
            out.append(c)
    return out


def card_id(name):
    """ModelId.Entry of a card class (StrikeIronclad -> STRIKE_IRONCLAD), for cards the reader didn't send."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).upper()


def enemy_discards(state, plan):
    """Status cards the enemies' shown moves put in our discard this enemy turn (Sticky Shot: 2 Slimed), so the
    reshuffle has them; enemies the plan kills don't act. ponytail: a stunned enemy still counts."""
    out = []
    for i, f in enumerate(state.get("enemies", [])):
        if plan.end and i < len(plan.end) and plan.end[i][0] <= 0:
            continue
        mv = GAMEDATA["monsters"].get(f["name"], {}).get("moves", {}).get(f.get("move"), {})
        for e in mv.get("effects", []):
            if (e.get("adds_card") in GAMEDATA["cards"] and e.get("pile") == "Discard"
                    and isinstance(e.get("count", 1), (int, float))):
                out += [{**gd_card(e["adds_card"]), "id": card_id(e["adds_card"]), "level": 0}] * int(e.get("count", 1))
    return out


def after_draws(state, plan):
    """The piles once the plan's own draws are done: with the real order, its top `plan.drawn` cards leave the draw
    pile (into hand, then the discard), the hand goes to the discard (end_discard), and cards the plan puts on top
    (plan.on_top) lead the draw pile.
    ponytail: unordered piles keep them; a draw past the pile's end isn't reshuffled here; several cards put on top
    keep the plan's order (the game's last-placed-first isn't modelled)."""
    d = min(plan.drawn, len(state.get("draw") or []))
    if not state.get("draw_ordered"):
        return state
    disc = end_discard(state, plan) + state["draw"][:d] + enemy_discards(state, plan)
    for c in plan.on_top:  # user: a card moved on top (Headbutt...) is drawn first next turn, not reshuffled
        i = next((i for i, x in enumerate(disc) if x["name"] == c["name"] and x.get("upgraded") == c.get("upgraded")), None)
        if i is not None:
            disc.pop(i)
    return {**state, "draw": list(plan.on_top) + state["draw"][d:], "discard": disc, "hand": []}


def next_hand(state, plan):
    """Overlay: (the next hand's known cards, how many more come from the reshuffle, how many of the known ones come
    from the draw pile; the rest are the reshuffled discard) after `plan`, or None without the real pile order."""
    if not (state.get("status") == "combat" and state.get("draw_ordered") and "draw" in state):
        return None
    k, st = next_draw(state, plan.plays), after_draws(state, plan)
    draw, n = st["draw"], min(k, len(st["draw"]))
    if len(draw) < k and (new := reshuffle(st, st["discard"])) is not None:
        draw = draw + new  # short pile: the rest from the exact reshuffle
    return draw[:k], max(0, k - len(draw)), n


def next_blocks(state, dex):
    """Block each of EXPOSURE_SAMPLES likely next hands can make with next turn's energy (greedy, Block per
    energy); see draw_hand. Seeded, so the same state gives the same answer."""
    rng, out, k = random.Random(0), [], next_draw(state)
    energy = (state.get("max_energy") or 3) + amount(state.get("player", {}).get("powers", []), "EnergyNextTurnPower")
    for _ in range(EXPOSURE_SAMPLES):
        hand = draw_hand(state, k, rng)[0]
        e = b = 0
        for c in sorted(hand, key=lambda c: -c["vars"].get("Block", 0) / max(c["cost"], 0.5)):
            if c["vars"].get("Block") and c["type"] in ("Attack", "Skill") and 0 <= c["cost"] <= energy - e:
                e += c["cost"]
                b += max(0, c["vars"]["Block"] + dex)
        out.append(b)
    return out


def next_turn_unblocked(state):
    """Overlay: expected damage next turn's hand can't block (None without pile info)."""
    if state.get("status") != "combat" or "draw" not in state:
        return None
    dmg = sum(fc[0]["dmg"] for f in state["enemies"] if (fc := predict(f, state.get("ascension", 0), 1)))
    blocks = next_blocks(state, amount(state["player"]["powers"], "DexterityPower"))
    return sum(max(0, dmg - b) for b in blocks) / len(blocks)


def channels(card):
    return GAMEDATA.get("cards", {}).get(card["name"], {}).get("channel", [])


# Enemy powers the solver/predict read. Anything else on an enemy is shown in the overlay as "not modeled".
ENEMY_POWERS = REVIVE_POWERS | NO_END_POWERS | {"BackAttackLeftPower", "BackAttackRightPower", "PlowPower", "BurrowedPower", "RavenousPower", "CrabRagePower", "ImbalancedPower",
                                 "SwipePower", "HeistPower", "EscapeArtistPower", "BattlewornDummyTimeLimitPower", "ThieveryPower", "GalvanicPower",
                                 "VitalSparkPower", "StrengthPower", "VigorPower", "TerritorialPower", "VulnerablePower", "WeakPower", "SkittishPower",
                "HardenedShellPower", "SlipperyPower", "IntangiblePower", "ArtifactPower", "SlowPower",
                "PersonalHivePower", "EnragePower", "ShriekPower", "SandpitPower", "MinionPower", "CurlUpPower",
                "HardToKillPower", "FlutterPower", "SoarPower", "ThornsPower", "RitualPower", "PoisonPower",
                "DoomPower", "DebilitatePower", "HighVoltagePower", "DemisePower", "WitheringPresencePower", "PaperCutsPower", "PainfulStabsPower"}


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
PLAYER_POWERS = COST_POWERS | {"SurroundedPower", "TaintedPower", "StrengthPower", "DexterityPower", "WeakPower", "FrailPower", "VulnerablePower",
                               "SlothPower", "DisintegrationPower", "ConstrictPower", "ShrinkPower", "TenderPower",
                               "MindRotPower", "WasteAwayPower", "RingingPower", "SmoggyPower", "ChainsOfBindingPower",
                               "HexPower", "FocusPower", "AccuracyPower", "RegenPower"}
# Player powers carried through a plan (game hooks: .planning/research/powers.md). Plating: Block at our turn end
# (unpowered: no Dex/Frail). Buffer: the next HP loss is 0. Intangible: each hit and HP loss is at most 1. Thorns /
# Flame Barrier: damage back per attack hit. Rage (per Attack), Afterimage (per card), Feel No Pain (per exhaust): Block.
# Juggernaut: damage per Block gain. Envenom: Poison per unblocked attack hit. Vigor: + damage on the next Attack.
# Barricade / Blur: Block isn't cleared, so what's left after the enemy turn counts next turn.
BUFFS = ("PlatingPower", "BufferPower", "IntangiblePower", "ThornsPower", "FlameBarrierPower", "RagePower",
         "AfterimagePower", "FeelNoPainPower", "JuggernautPower", "EnvenomPower", "VigorPower", "BarricadePower",
         "BlurPower",
         # this turn's extra plays: Duplicator (next card), One-Two Punch (next Attacks), Burst (next Skills)
         "DuplicationPower", "OneTwoPunchPower", "BurstPower",
         # this turn's multipliers: Gigantification (next Attack x3), Shadowmeld (Block x2), Colossus (half damage
         # from Vulnerable enemies), Double Damage (Attacks x2)
         "GigantificationPower", "ShadowmeldPower", "ColossusPower", "DoubleDamagePower",
         # P3 triggers (decompiled): Panache (every 5th card: damage ALL), Haunt (a Soul played: a random enemy loses
         # X HP), Sleight of Flesh (each debuff we apply: X damage to it), Speedster (each card drawn: X to ALL),
         # Corrosive Wave (each card drawn this turn: X Poison to ALL), Smokestack (each Status we create: X to ALL),
         # Black Hole (stars spent or gained: X to ALL), Necro Mastery (Osty's HP lost: ALL enemies lose that x X)
         "PanachePower", "HauntPower", "SleightOfFleshPower", "SpeedsterPower", "CorrosiveWavePower",
         "SmokestackPower", "BlackHolePower", "NecroMasteryPower",
         # next turns only (value in ENGINES): no effect inside this turn
         "GenesisPower", "MachineLearningPower", "PrepTimePower", "SpectrumShiftPower", "LoopPower",
         "WellLaidPlansPower")
# Temporary Strength (Piercing Wail, Dark Shackles, Flex Potion...): the live StrengthPower already holds it, and it
# reverts at the end of its owner's turn (TemporaryStrengthPower), so the owner's hits this turn use it as read.
TEMP_POTIONS = {"FlexPotion", "SpeedPotion"}  # FlexPotionPower / SpeedPotionPower: Temporary Strength / Dexterity
TEMP_STRENGTH = {"PiercingWailPower", "CrushUnderPower", "DarkShacklesPower", "EnfeeblingTouchPower",
                 "ShacklingPotionPower", "FlexPotionPower", "SetupStrikePower"}
ENEMY_POWERS |= TEMP_STRENGTH | {"StranglePower", "OblivionPower", "NemesisPower", "HatchPower", "SuckPower",
                                 "AsleepPower", "SlumberPower",
                                 "RampartPower"}  # Rampart: Block at OUR turn start, already in the live Block
# Noxious Fumes / Neurosurge act at our turn start (live Poison / Doom); their later turns are in ENGINES / Doom.
PLAYER_POWERS |= set(BUFFS) | TEMP_STRENGTH | {"NoxiousFumesPower", "NeurosurgePower", "SoulboundPower", "ParryPower", "NoDrawPower", "JugglingPower", "EnergyNextTurnPower", "DrawCardsNextTurnPower",
                                "NoBlockPower", "TheGambitPower", "DoomPower", "CrueltyPower", "TrackingPower", "LethalityPower", "UnmovablePower", "FastenPower",
                                "RupturePower", "InfernoPower", "ShroudPower", "BlockNextTurnPower", "CrimsonMantlePower"}
EFFECTS |= set(BUFFS)
MODELED |= set(BUFFS) | {"StrengthPerVulnerable"}  # Dominate: Strength per Vulnerable on its target


def bget(buffs, name):
    return next((a for n, a in buffs if n == name), 0)


def badd(buffs, name, n):
    if not n:
        return buffs
    d = dict(buffs)
    d[name] = d.get(name, 0) + n
    return tuple(sorted((k, a) for k, a in d.items() if a))


def card_buffs(card):
    """({power: amount} a card or potion gives us, the var keys that says it). From vars named after the power
    (Stone Armor: PlatingPower 4) or the card's own power (Rage -> RagePower, amount from its generic var)."""
    if card.get("ops"):
        return card["op_buffs"], set()
    v = card["vars"]
    if not (card["type"] in ("Power", "Potion") or card.get("target") in ("Self", "None")):
        return {}, set()
    out = {k: v[k] for k in BUFFS if v.get(k)}
    used = set(out)
    own = {"Duplicator": "DuplicationPower"}.get(card["name"], card["name"] + "Power")
    if own in BUFFS and own not in out:
        key = next((k for k in ("Power", "DamageBack", card["name"]) if v.get(k)), None)
        out[own] = v[key] if key else 1
        used |= {key} if key else set()
    return out, used


def unmodeled_player_powers(state):
    return sorted({p["name"].removesuffix("Power") for p in state.get("player", {}).get("powers", [])
                   if p["name"] not in PLAYER_POWERS})


# Potions whose effect maps onto a modelled one (decomp OnUse; user: the advisor didn't use these):
# Foul Potion in combat = 12 damage to ALL (gamedata skips Event potions); Powdered Demise = 9 unblockable each enemy
# turn end forever (DemisePower), modelled as Poison 10 (10+9+8 = 27 = 9x3 over 3 turns); Touch of Insanity = a
# hand card costs 0 for the rest of combat, counted as this turn's energy for the dearest card in hand.
# ponytail: Demise past 3 turns and Catalyst-style Poison doubling skew it; Insanity's later turns aren't counted.
POTION_EXTRA = {"FoulPotion": {"target": "AllEnemies", "vars": {"Damage": 12}}}
POTION_AS = {"PowderedDemise": lambda v, state: {"PoisonPower": v.get("Demise", 9) + 1},
             "TouchOfInsanity": lambda v, state: {"Energy": max((c["cost"] for c in state.get("hand", [])
                                                                 if c["type"] in ("Attack", "Skill", "Power")), default=0)}}


def potion_actions(state):
    """Held potions whose effects the solver can model, shaped like cards (type "Potion", cost 0)."""
    out = []
    for p in state.get("potions", []):
        d = GAMEDATA["potions"].get(p["name"]) or POTION_EXTRA.get(p["name"])
        if not d:
            continue
        v = {k: int(n) for k, n in (d.get("vars") or {}).items()}
        if p["name"] in POTION_AS:
            v = POTION_AS[p["name"]](v, state)
            if not any(v.values()):
                continue
        pm = "draw" in state and any(pile_moves({"name": p["name"], "type": "Potion", "vars": v}))
        rest = set(v) - PILE_VARS if pm else set(v)
        made = potion_cards(p["name"], state)
        if made:
            rest -= {"Cards"}
        if not (rest & EFFECTS or pm or made or p["name"] == "Duplicator") or not rest <= MODELED:  # Duplicator: no vars
            continue
        if d["target"] != "Self" and "StrengthPower" in v:
            continue  # enemy Strength loss isn't modeled
        out.append({"name": p["name"], "type": "Potion", "target": "Self" if p["name"] == "TouchOfInsanity" else d["target"], "cost": 0,
                    "x_cost": False, "upgraded": False, "vars": v, "slot": p["slot"], "makes": made})
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


def hp_weight(state):
    """HP_WEIGHT for this fight, with boss HP relief (see FINAL_BOSS_HP)."""
    kind = fight_type(state)
    if kind != "Boss":
        return HP_WEIGHT.get(kind, 1)
    acts = GAMEDATA.get("acts", {})
    act = max((acts.get(a, {}).get("index") or 0
               for a in GAMEDATA["encounters"][state["encounter"]].get("acts", [])), default=0)
    if act < max((a.get("index") or 0) for a in acts.values()):
        return A2_BOSS_HP if (state.get("ascension") or 0) >= WEARY_TRAVELER else HP_WEIGHT["Boss"]
    rooms = state.get("rooms") or {}
    if rooms.get("second_boss") and not rooms.get("bosses_visited"):
        return HP_WEIGHT["Elite"]  # A10: the second final boss is next, with this HP
    return FINAL_BOSS_HP if rooms else HP_WEIGHT["Boss"]  # old reader: can't tell which final boss this is


def potion_cost(state, hp=None):
    """hp: HP to judge by (solve passes HP after this turn's and next turn's predicted hits)."""
    cost = POTION_COST.get(fight_type(state), POTION_COST["Monster"])
    if state.get("potion_slots") and len(state.get("potions", [])) >= state["potion_slots"]:
        cost *= FULL_SLOTS_FACTOR
    me = state["player"]
    frac = (me["hp"] if hp is None else hp) / max(me["max_hp"], 1)
    cost *= min(1, max(0, (frac - POTION_FREE_HP) / POTION_FREE_HP))  # 0 at <=30% HP, full at >=60%
    return max(MIN_POTION_COST, cost)


def held_energy_reason(state, plan):
    """Overlay note when the best plan leaves energy unspent while a card in hand could still be paid for."""
    left = plan.energy_left or 0
    if left < 1 or not any(c["type"] in ("Attack", "Skill", "Power") and 0 <= c["cost"] <= left for c in state["hand"]):
        return None
    if any(r["name"] == "PaelsTears" for r in state.get("relics", [])):
        n = int(GAMEDATA["relics"].get("PaelsTears", {}).get("vars", {}).get("Energy", 2))
        return f"keeps {left} energy on purpose: Pael's Tears gives +{n} energy next turn"
    return f"{left} energy left on purpose: no card adds more than it costs this turn"


def danger(hp, max_hp):
    """Multiplier on the cost of losing HP when ending at `hp`."""
    frac = max(0, hp) / max(max_hp, 1)
    return 1 + LOW_HP_WEIGHT * max(0, LOW_HP_FRACTION - frac) / LOW_HP_FRACTION


def deck_dpt(state, key="Damage", floor=DPT_FLOOR):
    """Our deck's rough damage (or Block) per turn: amount per energy times max energy."""
    deck = [c for c in (state.get("deck") or state["hand"]) if c["type"] in ("Attack", "Skill", "Power")]
    amt = sum(c["vars"].get(key, 0) * (max(c["vars"].get("Repeat", 1), 1) if key == "Damage" else 1) for c in deck)
    energy = sum(max(c["cost"], 1) for c in deck) or 1
    return max(floor, amt / energy * state.get("max_energy", state.get("energy") or 3))


def hit(e, n):
    """Deal n damage to enemy row e = [hp, block, vuln, weak, skittish, shell_left, slippery, intangible].
    Intangible turns a hit into 1 before Block; Block absorbs; Slippery turns HP lost into 1; Hardened Shell caps
    HP lost per turn. A negative hit (Strength below 0) deals 0. Returns the HP actually lost."""
    n = max(0, n)
    if n >= 1 and e[7] > 0:
        n = 1
    if len(e) > 13 and e[13]:  # Hard to Kill (HardToKillPower.cs ModifyDamageCap: each hit, before Block)
        n = min(n, e[13])
    absorbed = min(e[1], n)
    e[1] -= absorbed
    lost = n - absorbed
    if lost >= 1 and e[6] > 0:
        lost, e[6] = 1, e[6] - 1
    if e[5] >= 0:
        lost = min(lost, e[5])
        e[5] -= lost
    lost = min(lost, e[0])
    e[0] -= lost
    return lost


# ponytail: time cap on the plan search so a huge turn (9 cards, 4 targets, potions, Regent stars: minutes uncapped)
# can't freeze the overlay or the autopilot; past it the plans found so far stand (the best-first card order finds
# the strong lines early). Results on capped turns depend on machine speed. A beam search is the upgrade if capped
# turns show worse plans.
SOLVE_SECONDS = 1.5
# S7 B9 (user): two-turn lookahead in elite / boss fights. The top LOOK_PLANS plans each play out LOOK_SAMPLES next
# turns (hand from the draw pile, each enemy's next move from its AI table, potions used are gone) solved one turn
# deep; the plans are reordered by score + LOOK_WEIGHT x the mean next-turn score. Same samples for every plan
# (common random numbers). Uses only the time the search left of `seconds`.
# ponytail: cards drawn / made this turn, enemy buff moves, our temporary powers and Block kept (Barricade) aren't
# carried over; Power cards' own powers are. LOOK_WEIGHT is below FUTURE_DISCOUNT since the foresight already
# counts part of it; tune from hp_audit.
LOOK_PLANS, LOOK_SAMPLES, LOOK_WEIGHT, LOOK_MIN_SECONDS = 3, 4, 0.3, 0.02


def next_turn_state(state, plan, rng, exact=()):
    """The state at the start of next turn after `plan` (a sampled draw and enemy moves), for the lookahead.
    exact: replay_moves' result (H2): an enemy's known next move replaces the sampled one."""
    me, asc = state["player"], state.get("ascension", 0)
    played = [st[3] for st in plan.steps if st[5] == "card"]
    used = {st[3].get("slot") for st in plan.steps if st[5] == "potion"}
    powers = {p["name"]: dict(p) for p in me["powers"]}
    for c in played:  # Power cards stay in play: Inflame's Strength, Noxious Fumes, Barricade...
        if c["type"] != "Power":
            continue
        for o in GAMEDATA.get("cards", {}).get(c["name"], {}).get("ops") or []:
            if o.get("op") == "power" and o.get("tgt") == "self":
                n = c["vars"].get(o["amt"], 0) if isinstance(o["amt"], str) else o["amt"]
                powers.setdefault(o["power"], {"name": o["power"], "amount": 0})["amount"] += n
    k = next_draw(state, plan.plays)
    hand, draw, discard = draw_hand(after_draws(state, plan), k, rng)
    enemies = []
    for f, e in zip(state["enemies"], plan.end or [()] * len(state["enemies"])):
        cr = dict(f["creature"])
        if e:
            ticked = {"VulnerablePower": max(0, e[2] - 1), "WeakPower": max(0, e[3] - 1), "PoisonPower": max(0, e[10] - 1)}
            cr["hp"], cr["block"] = e[0], 0
            cr["powers"] = [{**p, "amount": ticked[p["name"]]} if p["name"] in ticked else p
                            for p in cr.get("powers", []) if ticked.get(p["name"], 1)]
            cr["powers"] += [{"name": n, "amount": v} for n, v in ticked.items()
                             if v and all(p["name"] != n for p in cr["powers"])]
        g = {**f, "creature": cr}
        ai = MONSTERS.get(f["name"])
        if ai and f.get("move") in ai["moves"]:
            nxt = follow(ai, f["move"], f["move"], set(f.get("history", [])) | {f["move"]}, f.get("slot"))
            known = exact[len(enemies)] if len(enemies) < len(exact) and exact[len(enemies)] else None
            if known or nxt:
                mv = known[0] if known else rng.choices(list(nxt), weights=list(nxt.values()))[0]
                g.update(move=mv, history=list(f.get("history", [])) + [f["move"]],
                         intents=[{"kind": "SingleAttackIntent", "damage": d, "hits": h}
                                  for d, h in attacks(ai["moves"][mv], asc)])
        enemies.append(g)
    return {**state, "hand": hand, "draw": draw, "discard": discard, "enemies": enemies,
            "potions": [p for p in state.get("potions", []) if p.get("slot") not in used],
            "energy": (state.get("max_energy") or 3) + amount(me["powers"], "EnergyNextTurnPower"),
            "turn": (state.get("turn") or 0) + 1, "my_cards_played": 0, "cards_played": 0, "skills_played": 0,
            "bound_played": 0, "drawn_turn": k, "discarded_turn": 0, "exhausted_this_turn": 0,
            "player": {**me, "hp": min(me["max_hp"], me["hp"] - plan.hp_loss), "block": 0,
                       "powers": [p for p in powers.values()
                                  if p["name"] not in ("EnergyNextTurnPower", "DrawCardsNextTurnPower")]}}


def look_ahead(state, plans, deadline):
    """Reorder the top LOOK_PLANS plans by score + LOOK_WEIGHT x mean next-turn best score (see LOOK_PLANS)."""
    top = plans[:LOOK_PLANS]
    if len(top) < 2 or any(p.win for p in top):
        return plans
    each = (deadline - time.monotonic()) / (len(top) * LOOK_SAMPLES)
    if each < LOOK_MIN_SECONDS:
        return plans
    means, exact = [], replay_moves(state["enemies"], state.get("monster_ai_rng"), 1)
    for p in top:
        if state["player"]["hp"] - p.hp_loss <= 0:
            means.append(None)
            continue
        vals = []
        for k in range(LOOK_SAMPLES):
            nxt = solve(next_turn_state(state, p, random.Random(k), exact), top=1, seconds=each, ahead=False)
            vals.append(nxt[0].score if nxt else 0)
        means.append(sum(vals) / len(vals))
    floor = min((m for m in means if m is not None), default=0)
    adj = {id(p): p.score + LOOK_WEIGHT * (floor if m is None else m) for p, m in zip(top, means)}
    return sorted(top, key=lambda p: -adj[id(p)]) + plans[LOOK_PLANS:]


def solve(state, top=3, kept=(), seconds=SOLVE_SECONDS, ahead=True):
    """Best plans for this turn. kept: cards already chosen to go on top of the draw pile (pick screens).
    seconds: search time cap (pick screens split theirs over the candidates). ahead: the two-turn lookahead (elite /
    boss fights, full-time solves only)."""
    if state.get("status") != "combat":
        return []
    me = state["player"]
    foes = state["enemies"]
    R = Relics(state.get("relics", []), GAMEDATA["relics"])
    # Same-name enemies are numbered among themselves (Louse #1, Louse #2), in the game's list order.
    # ponytail: that order isn't always left to right on screen; the autopilot aims by combat_id, not the label.
    names = [f["name"] + (f" #{sum(g['name'] == f['name'] for g in foes[:i]) + 1}"
                          if sum(g["name"] == f["name"] for g in foes) > 1 else "") for i, f in enumerate(foes)]

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
    # Unplayable cards in hand (Dazed, Burn, curses): what a discard/exhaust pick takes first, the most harmful
    # (turn-end damage, Regret) first. Plays.tossed = how many of them are gone.
    # Doubt / Shame: 1 Weak / Frail on us at turn end (no damage, but worse than a Dazed)
    harm = lambda c: end_dmg(c) + end_loss(c) + HAND * (c["name"] == "Regret") + (c["name"] in DEBUFF_CURSES)
    dead = sorted((c for c in state["hand"] if c not in playable), key=lambda c: -harm(c))
    enthralled = sum(c["name"] == "Enthralled" for c in state["hand"])
    smoggy = amount(me["powers"], "SmoggyPower")  # Skills played before this replan: their Smog shows on the cards
    skills_before = state.get("skills_played") or 0  # ...but made Skills have none: the reader's count catches those
    def add(c, n=1):
        k = (c["type"] == "Potion", c["name"], c["upgraded"], c["cost"], c.get("affliction"), tuple(sorted(c["vars"].items())))
        if k not in keys:
            keys[k] = len(groups)
            groups.append([c, 0])
        groups[keys[k]][1] += n
        return keys[k]

    for c in playable + potion_actions(state):
        add(from_ops(c))
    # Cards that put cards in hand (Blade Dance -> Shivs, Compact -> Fuel...): the made cards get a group (count 0)
    # so a plan can play them the same turn. makes[group] = (made group, how many).
    makes = {}
    # A card entering combat gets the affliction an active power gives new cards (AfterCardEnteredCombat):
    # Ringing / Hex on us (any card), Tangled on us (Attacks), Globe Head's Galvanic (Powers), Infested Prism's
    # Vital Spark (Skills). Smog goes by card type in allowed(). ponytail: one rule per card, first match.
    foe_pw = lambda name: any(amount(f["creature"]["powers"], name) for f in state["enemies"])
    new_aff = [(lambda c: True, "Ringing", amount(me["powers"], "RingingPower")),
               (lambda c: True, "Hexed", amount(me["powers"], "HexPower")),
               (lambda c: c["type"] == "Attack", "Entangled", amount(me["powers"], "TangledPower")),
               (lambda c: c["type"] == "Power", "Galvanized", foe_pw("GalvanicPower")),
               (lambda c: c["type"] == "Skill", "Tainted", foe_pw("VitalSparkPower"))]

    def entering(c):
        aff = next((a for fits, a, on in new_aff if on and fits(c)), None)
        return {**c, "affliction": aff} if aff and not c.get("affliction") else c
    for g, (c, _) in enumerate(list(groups)):
        made = creates(c)
        if made:
            makes[g] = [(add(entering(made[0]), 0), made[1])]
        for x, n in c.get("makes") or ():  # card-making potions (stand-in cards, see CARD_POTIONS)
            makes[g] = makes.get(g, []) + [(add(entering(x), 0), n)]
    # Fetch to hand (Hologram, Dredge, Secret Weapon, Liquid Memories...): the best eligible cards of the source
    # pile get a count-0 group too. ponytail: ranked by printed Damage+Block; cards played earlier this turn
    # (already in the discard by then) aren't candidates.
    pile_known = "draw" in state
    piles = {"Draw": state.get("draw", []), "Discard": state.get("discard", []), "Exhaust": state.get("exhaust", [])}
    for g, (c, _) in enumerate(list(groups)):
        f = pile_moves(c)[1] if pile_known else None
        if f and f["to"] == "Hand" and f["from"] in piles:
            ok = [x for x in by_name(piles[f["from"]]) if x["type"] in ("Attack", "Skill", "Power")
                  and f["filter"] in (None, x["type"])]
            ok.sort(key=lambda x: -(x["vars"].get("Damage", 0) * max(x["vars"].get("Repeat", 1), 1)
                                    + x["vars"].get("Block", 0)))
            makes[g] = makes.get(g, []) + [(add(x, 0), 1) for x in ok[:f["count"]]]
    no_draw = amount(me["powers"], "NoDrawPower") > 0  # Battle Trance / Bullet Time: no more draws this turn
    moves = [(0 if no_draw else m[0], m[1]) for m in (pile_moves(c) if pile_known else (0, None) for c, _ in groups)]
    # A draw that takes the whole draw pile is certain (Drum of Battle with 2 cards left): those cards join the hand
    # this turn as count-0 groups, so the plan can play them; they no longer count as expected draws. Not when a
    # drawn card draws too (Soul drawing Soul would loop). ponytail: two such draw cards both get the pile.
    for g, (c, _) in enumerate(list(groups)):
        if pile_known and 0 < len(piles["Draw"]) <= moves[g][0] and not any(x["vars"].get("Cards") for x in piles["Draw"]):
            makes[g] = makes.get(g, []) + [(add(x, 0), 1) for x in piles["Draw"]]
            moves[g] = (0, moves[g][1])
    moves += [(0, None)] * (len(groups) - len(moves))
    deck_names = {c["name"] for c in state.get("deck") or state["hand"]}  # Exhaust only costs cards that come back

    # state: energy, group counts, enemies (hp, block, vuln, weak, skittish, shell left (-1 = none),
    # slippery, intangible, Dazed added to our deck, artifact, poison, doom, Strength change this turn), block,
    # strength, dex, Plays, potions used, our HP (after self-damage and heals), Osty HP, orbs ((name, value), ...), stars,
    # buffs (our BUFFS powers: ((name, amount), ...))
    def pw(f, name):
        return amount(f["creature"]["powers"], name)

    def pdata(f, name, key, default=0):
        """A power's private per-turn state from the reader (PowerModel._internalData), e.g. Hardened Shell's damage
        taken this turn; default for older readers."""
        p = next((p for p in f["creature"]["powers"] if p["name"] == name), None)
        return ((p or {}).get("data") or {}).get(key, default)

    start = (
        state["energy"],
        tuple(n for _, n in groups),
        tuple((f["creature"]["hp"], f["creature"]["block"], pw(f, "VulnerablePower"), pw(f, "WeakPower"),
               # Skittish: once per turn; Hardened Shell: what's left of this turn's cap (both from the reader)
               pw(f, "SkittishPower") * (not pdata(f, "SkittishPower", "hasGainedBlockThisTurn", False)) + pw(f, "CurlUpPower"),
               max(0, pw(f, "HardenedShellPower") - pdata(f, "HardenedShellPower", "damageReceivedThisTurn"))
               if pw(f, "HardenedShellPower") else -1, pw(f, "SlipperyPower"),
               pw(f, "IntangiblePower"), 0, pw(f, "ArtifactPower"), pw(f, "PoisonPower"), pw(f, "DoomPower"), 0,
               pw(f, "HardToKillPower"),  # [13]: Hard to Kill caps every hit (cards, orbs, relics, Thorns)
               pw(f, "FlutterPower"))  # [14]: Flutter hits left; 0 after it had some = stunned (skips its move)
              for f in foes),
        me["block"],
        amount(me["powers"], "StrengthPower"),
        amount(me["powers"], "DexterityPower"),
        NO_PLAYS,
        0,
        me["hp"],
        next((p["creature"]["hp"] for p in (state.get("pets") or []) if p["name"] == "Osty"), 0),
        tuple((o["name"].removesuffix("Orb"), o.get("value")) for o in state.get("orbs", [])),
        state.get("stars", 0),
        tuple(sorted((p["name"], p["amount"]) for p in me["powers"] if p["name"] in BUFFS and p["amount"])),
    )
    focus = amount(me["powers"], "FocusPower")
    echo = amount(me["powers"], "EchoFormPower")  # first N cards each turn are played twice (already in play only)
    slots = state.get("orb_slots", 0)
    slowed = [pw(f, "SlowPower") > 0 for f in foes]
    hive = [pw(f, "PersonalHivePower") for f in foes]
    # cards you already played this turn (autopilot replans after each); cards_played also counts teammates' (Slow)
    before = (state["my_cards_played"] if state.get("my_cards_played") is not None else state.get("cards_played")) or 0
    # Knowledge Demon curses on us: Sloth caps cards per turn, Disintegration hits us at our turn end.
    sloth = next((p for p in me["powers"] if p["name"] == "SlothPower"), None)
    cap = sloth["amount"] - (sloth.get("played") or before) if sloth else math.inf
    if "VelvetChoker" in R:  # no more than 6 cards a turn
        cap = min(cap, R.var("VelvetChoker", "Cards") - before)
    mine = {p["name"]: p["amount"] for p in me["powers"]}

    scarf = int(R.var("BrilliantScarf", "Cards")) if "BrilliantScarf" in R else 0

    def cost_of(card, plays):  # Brilliant Scarf: the 5th card played from hand each turn is free
        if scarf and card["type"] != "Potion" and before + plays.cards == scarf - 1:
            return 0
        return card_cost(card, mine, before + plays.cards, plays)

    burn = amount(me["powers"], "DisintegrationPower") + amount(me["powers"], "ConstrictPower")  # at our turn end
    # Enemy powers on our cards / hits (wiki): Aeonglass' Withering Presence adds a Wither (3 damage at our turn end
    # if held) every 6 cards played; Paper Cuts (Scroll of Biting): -1 Max HP per unblocked attack hit (counted as
    # HP); Painful Stabs (Test Subject): a Wound into our discard per unblocked attack hit.
    withering = any(pw(f, "WitheringPresencePower") for f in foes)
    wither_left = next((pdata(f, "WitheringPresencePower", "CardsLeft", None) for f in foes
                        if pw(f, "WitheringPresencePower")), None) or 6 - before % 6  # older reader: counter from 0
    # new Withers match the upgraded ones (Aeonglass upgrades every Wither: MatchWitherToUpgradeCount)
    wither_dmg = max([c["vars"].get("Damage", 0) for pile in ("hand", "draw", "discard", "exhaust")
                      for c in state.get(pile) or [] if c["name"] == "Wither"]
                     or [GAMEDATA["cards"].get("Wither", {}).get("vars", {}).get("Damage", 3)])
    # Paper Cuts / Painful Stabs: only the owner's unblocked hits (PaperCutsPower.cs / PainfulStabsPower.cs: dealer == Owner)
    paper_cuts = [pw(f, "PaperCutsPower") for f in foes]
    stabs = [pw(f, "PainfulStabsPower") for f in foes]
    # Hallway powers: Hard to Kill caps each hit; Flutter/Soar halve our attacks on them; Thorns hit back per
    # attack hit; Shrink (on us) cuts our attacks to 70%; Tender (on us) costs 1 Str and 1 Dex per card played.
    # Flutter (Thieving Hopper): each Attack hit that gets past its Block counts it down; at 0 it's stunned (skips
    # its move, Escape included) and the halving ends (FlutterPower.cs).
    hard_cap = [pw(f, "HardToKillPower") or math.inf for f in foes]
    demise = [pw(f, "DemisePower") for f in foes]  # loses X HP at the end of its turn
    soar = [pw(f, "SoarPower") > 0 for f in foes]
    thorns = [pw(f, "ThornsPower") for f in foes]
    # Minions (Gas Bomb, Illusions...) die when the last non-minion enemy dies (CreatureCmd.Kill).
    minion = [pw(f, "MinionPower") > 0 for f in foes]
    leaders = not all(minion)
    # A living summoner (a move that summons: Ovicopter's eggs, The Obscura's Parafright, Living Fog's Gas Bomb)
    # replaces killed minions, so a minion's future damage counts RESUMMON_TURNS forecast turns at most.
    summons = lambda f: any(e.get("summon") for mv in GAMEDATA["monsters"].get(f["name"], {}).get("moves", {}).values()
                            for e in mv.get("effects", []))
    respawn = [m and any(not mm and summons(g) for g, mm in zip(foes, minion)) for m in minion]
    shrink = 0.7 if amount(me["powers"], "ShrinkPower") else 1
    # Debuffs on us that leave with the enemy that applied them (their AfterDeath removes them): Constrict
    # (Slithering Strangler), Shrink (Shrinker Beetle), Hex's Ethereal (Spectral Knight). A kill this turn ends them.
    src = lambda name: [i for i, f in enumerate(foes) if f["name"] == name]
    gone_src = lambda idx, en: bool(idx) and all(en[i][0] == 0 for i in idx)
    strangler, shrinker, knight = src("SlitheringStrangler"), src("ShrinkerBeetle"), src("SpectralKnight")
    constrict = amount(me["powers"], "ConstrictPower")
    # The Lost / The Forgotten give back the Strength / Dexterity they stole from us when they die (reader data)
    possess = [(i, -pdata(f, "PossessStrengthPower", "stolenStrength"), -pdata(f, "PossessSpeedPower", "stolenDexterity"))
               for i, f in enumerate(foes) if pw(f, "PossessStrengthPower") or pw(f, "PossessSpeedPower")]
    tender = amount(me["powers"], "TenderPower") > 0
    weak = amount(me["powers"], "WeakPower") > 0
    frail = amount(me["powers"], "FrailPower") > 0
    # Vulnerable / Weak strength (wiki: Buffs/Debuffs): Paper Phrog makes Vulnerable +75%, Cruelty adds X%, Paper
    # Krane makes enemy Weak -40%; Debilitate on an enemy doubles both. Tracking: Weak enemies take X times more
    # from Attacks. Lethality: the first Attack each turn +X%. Once active (playing them isn't scored yet).
    debil = [pw(f, "DebilitatePower") > 0 for f in foes]
    vuln_bonus = 0.5 + 0.25 * ("PaperPhrog" in R) + amount(me["powers"], "CrueltyPower") / 100
    weak_cut = 0.4 if "PaperKrane" in R else 0.25
    tracking = amount(me["powers"], "TrackingPower")
    lethality = amount(me["powers"], "LethalityPower") / 100
    unmovable = amount(me["powers"], "UnmovablePower")  # the first X card Blocks each turn are doubled
    fasten = amount(me["powers"], "FastenPower")  # + Block on Defend cards
    rupture = amount(me["powers"], "RupturePower")  # HP lost on our turn: + Strength
    inferno = amount(me["powers"], "InfernoPower")  # HP lost on our turn: X damage to ALL enemies
    shroud = amount(me["powers"], "ShroudPower")  # applying Doom: + Block
    juggling = amount(me["powers"], "JugglingPower")
    # Attacks already played this turn (reader, from combat history); older readers only know none were played.
    atk_before = state.get("attacks_played", None if before else 0)
    soulbound = amount(me["powers"], "SoulboundPower")  # each Soul we create adds X Souls to the draw pile
    hang = [pw(f, "HangPower") for f in foes]  # Hang: x its damage on this enemy
    strangle = [pw(f, "StranglePower") for f in foes]  # this turn: HP loss / Doom per card we play
    oblivion = [pw(f, "OblivionPower") for f in foes]
    music = "MusicBox" in R and not R.count("MusicBox", "_wasUsedThisTurn")
    # Pael's Eye: the first turn each combat we end without playing a card, the hand is Exhausted and we take an
    # extra turn (the enemy turn waits). ponytail: the delayed enemy turn is valued like next turn's forecast.
    eye = "PaelsEye" in R and not R.count("PaelsEye", "_usedThisCombat") and not before
    lamp = "UnsettlingLamp" in R and not R.count("UnsettlingLamp", "_isFinishedTriggering")
    # Relics on the enemy turn: Diamond Diadem halves its damage when we played <= 2 cards; Undying Sigil halves
    # hits of enemies whose Doom >= HP. Art of War: no Attacks this turn -> +1 energy next turn.
    diadem = R.var("DiamondDiadem", "CardThreshold") if "DiamondDiadem" in R else -1
    art = R.var("ArtOfWar", "Energy") if "ArtOfWar" in R and not R.count("ArtOfWar", "_anyAttacksPlayedThisTurn") else 0
    # Hazards: No Block (Panic Button) = no Block from cards; The Gambit = unblocked attack damage kills you;
    # Doom on us (Neurosurge) = we die at the end of the enemy turn if HP <= Doom.
    no_block = amount(me["powers"], "NoBlockPower") > 0
    gambit = amount(me["powers"], "TheGambitPower") > 0
    my_doom = amount(me["powers"], "DoomPower")

    def enemy_hits(en, skills=0, taint=0, facing=None, colossus=False):
        """Per enemy, the damage of each hit it deals next enemy turn (Strength, Vigor, Weak, our Vulnerable;
        Enrage: +Strength per Skill we played; taint: our Tainted, + to every attack hit)."""
        vuln = amount(me["powers"], "VulnerablePower") > 0
        out = []
        for i, (f, e, e0) in enumerate(zip(foes, en, start[2])):
            total = []
            ally_died = any(a[0] > 0 and b[0] == 0 for j, (a, b) in enumerate(zip(start[2], en)) if j != i)
            s = (amount(f["creature"]["powers"], "StrengthPower") + skills * pw(f, "EnragePower")
                 + CRAB_RAGE_STR * (pw(f, "CrabRagePower") > 0 and ally_died) + e[12])
            vigor = amount(f["creature"]["powers"], "VigorPower")  # adds to every hit of the next attack
            # Stunned now (no attack): Terror Eel's Shriek / Ceremonial Beast's Plow when HP drops to the
            # threshold; Tunneler's Burrowed when its Block is broken; Corpse Slug's Ravenous when an ally dies.
            stun_at = pw(f, "ShriekPower") or pw(f, "PlowPower")
            stunned = ((stun_at and e[0] <= stun_at < e0[0]) or (pw(f, "BurrowedPower") and e0[1] > 0 and e[1] == 0)
                       or (pw(f, "RavenousPower") and ally_died) or (e0[14] > 0 and e[14] == 0))
            for it in f["intents"] if e[0] > 0 and not stunned else []:
                base = intent_damage(f, it, state.get("ascension", 0))
                if base is not None:
                    per = math.floor((base + s + vigor + taint) * (1 - weak_cut * (2 if debil[i] else 1) if e[3] > 0 else 1)
                                     * (1.5 if vuln else 1) * (BACK_ATTACK if behind(f, facing) else 1)
                                     * (0.5 if colossus and e[2] > 0 else 1))
                    total += [per] * max(it["hits"], 1)
                    vigor = 0
            out.append(total)
            # Its move puts Vulnerable on us (Queen, Soul Nexus, Ovicopter...): enemies acting after it hit x1.5
            if e[0] > 0 and not stunned and gives_vuln(f):
                vuln = True
        return out

    def gives_vuln(f):
        mv = GAMEDATA["monsters"].get(f["name"], {}).get("moves", {}).get(f.get("move"), {})
        return any(x.get("power") == "VulnerablePower" and x.get("on") != "self" for x in mv.get("effects", []))

    def incoming_each(en, skills=0, taint=0, facing=None):
        return [sum(h) for h in enemy_hits(en, skills, taint, facing)]

    wounds = 0  # Painful Stabs: Wounds from the last take_hits call

    def take_hits(hits, block, osty, buffs, pre_dmg, pre_loss):
        """(HP lost, Osty soaked, Block left) in the game's order: our turn-end damage (blockable) and HP loss, then
        each enemy hit. Block soaks each hit; Osty takes unblocked attack damage; Intangible caps each at 1; Buffer
        cancels the next HP loss."""
        buffer, intangible = bget(buffs, "BufferPower"), bget(buffs, "IntangiblePower") > 0
        nonlocal wounds
        lost = soaked = 0
        for dmg, attack, blockable, who in [(pre_dmg, False, True, None), (pre_loss, False, False, None)] + [
                (h, True, True, i) for i, hs in enumerate(hits) for h in hs]:
            if dmg <= 0:
                continue
            if intangible:
                dmg = min(dmg, 1)
            through = dmg
            if blockable:
                through, block = max(0, dmg - block), max(0, block - dmg)
            if attack and osty:
                s = min(osty, through)
                osty, soaked, through = osty - s, soaked + s, through - s
            if through and buffer:
                buffer, through = buffer - 1, 0
            if through and attack and gambit:
                through = DEATH_HP  # The Gambit
            if through and attack:
                through += paper_cuts[who]
                wounds += stabs[who]
            lost += through
        return lost, soaked, block

    def incoming(en, skills=0, taint=0, facing=None):
        return sum(incoming_each(en, skills, taint, facing))

    # Infested Prism's Vital Spark N: our Skills are Tainted; each one played gives us N Tainted (+N damage per
    # enemy attack hit this turn). ponytail: still counted if the plan kills the Prism before the Skill.
    # Surrounded (Crusher + Rocket): we face one side; enemies on the other side (Back Attack Left/Right) hit 1.5x.
    # A targeted card or potion aimed at an enemy behind us turns us to face it.
    surround = next((x for x in me["powers"] if x["name"] == "SurroundedPower"), None)
    face0 = ("Left" if surround.get("facing") == 1 else "Right") if surround else None
    side = {f.get("combat_id"): "Left" if pw(f, "BackAttackLeftPower") else "Right" if pw(f, "BackAttackRightPower")
            else None for f in foes}

    def facing_after(steps):
        return next((side[s[4]] for s in reversed(steps) if s[4] is not None and side.get(s[4])), face0)

    spark = max((pw(f, "VitalSparkPower") for f in foes), default=0)
    tainted_now = amount(me["powers"], "TaintedPower")
    galvanic = max((pw(f, "GalvanicPower") for f in foes), default=0)  # Globe Head: Galvanized Powers hurt us on play
    galv_src = [i for i, f in enumerate(foes) if pw(f, "GalvanicPower")]  # ...while it lives (the power goes with it)
    crabs = [i for i, f in enumerate(foes) if pw(f, "CrabRagePower")]
    revives = [any(pw(f, p) > 0 for p in REVIVE_POWERS) for f in foes]
    reattach = [pw(f, "ReattachPower") > 0 for f in foes]
    illusion = [pw(f, "IllusionPower") > 0 for f in foes]
    erupt = [pw(f, "SteamEruptionPower") for f in foes]
    loot = [card_loot_hp(f, state) + GOLD_LOOT_HP * pw(f, "HeistPower") for f in foes]
    # Bowlbug Rock (Imbalanced): fully blocking its attack knocks it off balance, so it skips its next attack.
    off_balance = [pw(f, "ImbalancedPower") > 0 and any(intent_damage(f, it, state.get("ascension", 0))
                                                        for it in f["intents"]) for f in foes]
    tears = R.var("PaelsTears", "Energy") if "PaelsTears" in R else 0  # unspent energy -> +2 energy next turn

    # The Insatiable's Sandpit: a countdown that kills you at 0; each Frantic Escape played adds 1.
    sandpit = max((pw(f, "SandpitPower") for f in foes), default=0)
    pit_owners = [i for i, f in enumerate(foes) if pw(f, "SandpitPower")]
    escape_groups = [g for g, (c, _) in enumerate(groups) if c["name"] == ESCAPE]

    # Ironclad's Burning Blood / Black Blood heal after a won fight; only the part that would overflow max HP
    # makes damage free (at 80/80 a 3 hit costs nothing; at 70/80 it does), and only what the rest of the fight
    # won't take anyway (set below, once the expected hits are known). Everyone else pays full price.
    after_heal = sum(GAMEDATA["relics"].get(r["name"], {}).get("vars", {}).get("Heal", 0)
                     for r in state.get("relics", []) if r["name"] in POST_FIGHT_HEAL)
    free_hp = max(0, min(after_heal, me["hp"] + after_heal - me["max_hp"]))
    hp_w = W_HP * hp_weight(state)  # HP lost now / to the enemies left; future-turn credits
    # (draws, retained cards, Dexterity, Plating, kept exhaust cards) use fut_w: expected, not certain, so never
    # above W_HP, but no dearer than this fight's HP (a boss's Impervious saved for later saves cheap HP)
    fut_w = min(hp_w, W_HP)
    # co-op: enemy HP is scaled for the whole party (players x 1.1-1.3), and every living player deals damage
    turns_left = sum(f["creature"]["hp"] for f in foes) / (deck_dpt(state) * max(1, state.get("party") or 1))
    # A played card comes back only after the draw pile runs out (reshuffle).
    left = turns_left - 1 - (len(state["draw"]) / HAND if pile_known else 0)
    redraws = min(REDRAW_CAP, max(0.0, left) * HAND / max(len(state.get("deck") or state["hand"]), 1))
    pack = state.get("deck") or state["hand"]
    atk_hits = HAND * sum(max(c["vars"].get("Repeat", 1), 1) for c in pack
                          if c["type"] == "Attack" and "Damage" in c["vars"]) / max(len(pack), 1)
    blk_cards = HAND * sum("Block" in c["vars"] for c in pack) / max(len(pack), 1)
    buff_turns = min(max(turns_left - 1, 0.0), BUFF_TURNS)
    eng_turns = min(max(turns_left - 1, 0.0), ENGINE_TURNS)
    n_alive = sum(f["creature"]["hp"] > 0 for f in foes)

    def lasting(steps):
        """Score of the Strength/Dexterity this plan leaves for later turns."""
        out = 0
        for s in steps:
            c = s[3]
            if c["type"] == "Potion" and c["name"] in TEMP_POTIONS:
                continue  # Flex / Speed: this turn only (Strength Potion, Dexterity Potion last)
            for key, per in (("StrengthPower", atk_hits * value_per_damage), ("DexterityPower", blk_cards * fut_w * 0.5)):
                sign, when = (1, "now") if c.get("ops") else timing(c["name"], key)
                n = sign * c["vars"].get(key, 0)
                if when == "now" and (c["type"] == "Power" or c["target"] not in ("AnyAlly", "AllAllies")):
                    out += n * per * buff_turns
                elif when == "turn":  # +n at the start of each later turn: n, 2n, 3n...
                    out += n * per * buff_turns * (buff_turns + 1) / 2
            for power, n in engine_powers(c):
                kind, per, ramp = ENGINES[power]
                amt = n * per * (n_alive if kind == "all" else 1) * (eng_turns * (eng_turns + 1) / 2 if ramp else eng_turns)
                out += fut_w * 0.5 * amt if kind == "block" else value_per_damage * amt
                out -= fut_w * eng_turns if power == "InfernoPower" else 0
            # Capacitor / Modded add orb slots, Bulk Up removes one: one more (or fewer) orb passive each later turn.
            # ponytail: half a Lightning passive per slot per turn (the slot isn't always filled); no orb types read.
            out += c["vars"].get("SlotsGained", 0) * value_per_damage * (ORB_PASSIVE["Lightning"] + focus) * 0.5 * eng_turns
            plating = card_buffs(c)[0].get("PlatingPower", 0)  # Plating keeps giving Block: n-1, n-2... later turns
            out += fut_w * 0.5 * sum(max(0, plating - k) for k in range(1, round(buff_turns) + 1))
        return FUTURE_DISCOUNT * out

    def worth(c):  # rough value of one play of a card: its damage + half its Block, in score
        v = c["vars"]
        return value_per_damage * v.get("Damage", 0) * max(v.get("Repeat", 1), 1) + fut_w * 0.5 * v.get("Block", 0)

    def exhausts(c):
        return "Exhaust" in keywords(c) or (c["type"] == "Skill" and "CorruptionPower" in mine)
    exhausted_before = exhausted_this_turn(state)
    exact = replay_moves(foes, state.get("monster_ai_rng"), FUTURE_TURNS)  # H2: the real next moves when known
    forecast = [predict(f, state.get("ascension", 0), FUTURE_TURNS, exact[i] if i < len(exact) else None)
                for i, f in enumerate(foes)]
    for j, fc in enumerate(forecast):  # a summon adds its hits to every later turn: killing the summoner first
        for k, t in enumerate(fc):     # stops them (summons are its minions or come from it)
            extra = summon_dmg(foes[j]["name"], t["moves"], state.get("ascension", 0))
            for t2 in fc[k + 1:] if extra else ():
                t2["dmg"] = round(t2["dmg"] + extra, 1)
    for j, fc in enumerate(forecast):  # Strength one enemy gives its teammates lifts their later hits
        for k, t in enumerate(fc):
            gain = ally_strength(foes[j]["name"], t["moves"], state.get("ascension", 0))
            for i, other in enumerate(forecast):
                for t2 in other[k + 1:] if i != j and gain else ():
                    t2["dmg"] = round(t2["dmg"] + gain * (t2["dmg"] / t2["hit"] if t2.get("hit") else 0), 1)
    future = [sum(t["dmg"] * FUTURE_DISCOUNT ** k for k, t in enumerate(fc)) for fc in forecast]
    next_turn = [fc[0]["dmg"] if fc else 0 for fc in forecast]
    # Threat looks further ahead than the HP math (up to the fight's expected length): an enemy that scales
    # (Ritual, Enlarge, Territorial...) keeps hitting harder, so its damage now is worth more.
    t_turns = min(THREAT_TURNS_MAX, max(FUTURE_TURNS, math.ceil(turns_left)))
    scaled = [sum(t["dmg"] * THREAT_DISCOUNT ** k for k, t in enumerate(fc[:RESUMMON_TURNS] if r else fc))
              for fc, r in zip((predict(f, state.get("ascension", 0), t_turns) for f in foes), respawn)]
    # Minions leave with the last leader: the leaders carry their minions' future threat (Smith's rule then favours
    # the kill that ends them all; a summoner's minions are a treadmill, see respawn).
    carried = sum(sc for sc, m in zip(scaled, minion) if m) / max(1, sum(not m for m in minion))
    threat = [n + fu + lo + GOLD_LOOT_HP * pw(f, "ThieveryPower") + (0 if m else carried)
              for n, fu, lo, f, m in zip(incoming_each(start[2]), scaled, loot, foes, minion)]
    # Focus fire (Smith's rule): damage counts by threat per HP left, so the scaler / nearly dead enemy is
    # finished first instead of chipping everyone. Damage into Block (Skittish) still counts 0.
    ratio = [t / max(f["creature"]["hp"] + f["creature"]["block"], 1) for t, f in zip(threat, foes)]
    mean_threat = sum(ratio) / max(len(ratio), 1)
    weight = [0 if f["creature"]["hp"] >= UNKILLABLE else min(THREAT_MAX, max(THREAT_MIN, t / mean_threat))
              if mean_threat else 1 for t, f in zip(ratio, foes)]
    weight = [w * (NEMESIS_NOW if pw(f, "NemesisPower") and not pw(f, "IntangiblePower") else 1)
              # Hatch at 1, or about to leave: Battleworn Dummy's timer at 1 escapes at its turn end, Thieving Hopper's
              # Escape Artist at 1 = its escape move comes: damage only counts if it dies now
              * (HATCH_NOW if any(0 < pw(f, p) <= 1 for p in ("HatchPower", "BattlewornDummyTimeLimitPower",
                                                                "EscapeArtistPower")) else 1) for w, f in zip(weight, foes)]
    # A summoner's minion is replaced when killed: chip damage on it is mostly wasted, the summoner's HP is what
    # ends the fight. A kill before it acts still pays through W_KILL and the hits it no longer lands.
    weight = [w * (MINION_CHIP if r else 1) for w, r in zip(weight, respawn)]
    # Potions get cheaper when THIS turn's hits would leave us low. A big hit predicted for next turn
    # is a reason to keep the potion for that turn, not to spend it now.
    pot_cost = potion_cost(state, me["hp"] + me["block"] - incoming(start[2])) * hp_weight(state)
    # The forecast's biggest enemy turn after this one: (all enemies' damage, its biggest single enemy, turn k).
    spike = max(((sum(fc[k]["dmg"] for fc in forecast if len(fc) > k), max(fc[k]["dmg"] for fc in forecast if len(fc) > k), k,
                  max(fc[k].get("hit", fc[k]["dmg"]) for fc in forecast if len(fc) > k))
                 for k in range(max(map(len, forecast), default=0))), default=(0, 0, 0, 0))

    our_dpt = deck_dpt(state)
    # B7 co-op: an enemy's Vulnerable boosts every living player's attacks, not just ours.
    # ponytail: teammates assumed to hit as hard as we do; Weak / Strength-down shielding teammates isn't counted
    # (whom each intent targets isn't read).
    team_dpt = our_dpt * max(1, state.get("party") or 1)

    def vuln_later(en, alive):
        """Our later turns' extra damage from enemy Vulnerable left after this plan. It ticks down at the end of each
        enemy turn: Vulnerable v covers our next v - 1 turns. ponytail: our damage split evenly over living enemies."""
        n = sum(alive) or 1
        return sum(value_per_damage * vuln_bonus * (2 if debil[i] else 1) * team_dpt / n
                   * sum(FUTURE_DISCOUNT ** (j - 1) for j in range(1, min(int(e[2]), BUFF_TURNS + 1)))
                   for i, (e, a) in enumerate(zip(en, alive)) if a)

    def potion_price(c):
        """Cost of drinking a potion now: pot_cost, or more when a later turn needs it more. Block / Buffer / Intangible
        / Weak: what they'd save on the forecast's biggest enemy turn (if it beats this one; a Weak drunk now that
        lasts until then is credited in future_left instead). Flex / Speed: an average hand's worth (held for a turn
        with more Attacks / Block to boost)."""
        v, (dmg, one, k, big), later = c["vars"], spike, 0
        if dmg > incoming(start[2]):
            gap_then = exposure(dmg)
            shield = (min(v.get("Block", 0), gap_then) + gap_then * bool(v.get("IntangiblePower"))
                      + min(gap_then, big) * bool(v.get("BufferPower")))  # Buffer: stops one hit (its biggest)
            weak = weak_cut * one if 0 < v.get("WeakPower", 0) <= k + 1 else 0
            later = hp_w * FUTURE_DISCOUNT ** k * (shield + weak)
        if c["name"] in TEMP_POTIONS:
            later = max(later, FUTURE_DISCOUNT * (v.get("StrengthPower", 0) * atk_hits * value_per_damage
                                                  + v.get("DexterityPower", 0) * blk_cards * fut_w * 0.5))
        return max(pot_cost, later)
    # next hand's Block: sampled from the draw pile, else (pile unknown) what the deck blocks per turn on average
    blocks = next_blocks(state, start[5]) if pile_known else [deck_dpt(state, "Block", 0)]
    soon = amount(me["powers"], "BlockNextTurnPower") + amount(me["powers"], "CrimsonMantlePower")  # next turn's start
    blocks = [b + soon for b in blocks]

    def exposure(dmg, extra=0):
        """Expected unblocked damage next turn: dmg minus what a likely next hand blocks (+ cards we put on top)."""
        return sum(max(0, dmg - b - extra) for b in blocks) / len(blocks)
    gap = exposure(sum(next_turn))
    # per enemy and turn: its forecast hit, or (no AI table) what it intends now, every turn (nothing known: DMG_FLOOR)
    long = [[t["dmg"] for t in fc] or [now or DMG_FLOOR] * t_turns
            for fc, now in zip((predict(f, state.get("ascension", 0), t_turns) for f in foes), incoming_each(start[2]))]
    unblocked = sum(exposure(sum(fc[k] for fc in long if len(fc) > k)) for k in range(t_turns)) / t_turns
    value_per_damage = hp_w * min(VPD_CAP.get(fight_type(state), 0.8), max(unblocked, DMG_MIN) / deck_dpt(state))
    free_hp = max(0, free_hp - unblocked * max(0.0, turns_left - 1))  # the heal's spare room is shared by all turns

    def worth_next(c, vuln=False):
        """A card's value next turn: Block only up to the damage next hand can't block (~0 before a quiet
        turn), damage x1.5 while the enemy stays Vulnerable."""
        v = c["vars"]
        block = max(0, v["Block"] + start[5]) if "Block" in v else 0
        return (value_per_damage * v.get("Damage", 0) * max(v.get("Repeat", 1), 1) * (1.5 if vuln else 1)
                + fut_w * min(block, gap))
    # Draw cards: each draw is worth the mean draw-pile card we can still afford (junk = 0); empty -> reshuffle.
    pool = (state.get("draw") or state.get("discard") or []) if pile_known else []
    pool = [(c["cost"], 0 if junk(c) else worth(c), c["name"]) for c in pool]
    # Void: drawing it loses 1 energy (Void.cs AfterCardDrawn): about one average playable card's worth
    avg_card = sum(w for c, w, _ in pool if c >= 0) / max(sum(1 for c, _, _ in pool if c >= 0), 1)
    pool = [(0, -avg_card) if n == "Void" else (c, w) for c, w, n in pool]

    def lose(e, n):
        """HP loss (Unblockable: Strangle, Haunt, Necro Mastery): Block doesn't stop it; Intangible etc. still do."""
        b, e[1] = e[1], 0
        hit(e, n)
        e[1] = b

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

    # Count cards (decomp CalculatedVar / OnPlay): value = CalculationBase + extra x a count in combat. The card's own
    # var holds only the base (Barrage: 0 hits), so count it here: the reader's combat counts plus what this plan
    # changed (orbs channeled, Attacks played, Block gained, cards discarded...). A count of 0 hits deals no damage.
    # ponytail: draws inside the plan are unknown (Death March / Murder use the reader's count); Gang Up counts only
    # teammates' hits (0 solo); Rattle / Tear Asunder don't add this plan's own Osty hits / damage taken.
    all_cards = [c for k in ("hand", "draw", "discard", "exhaust") for c in state.get(k) or []]
    hand_skills = sum(c["type"] == "Skill" for c in state["hand"])
    n_statuses = sum(c["type"] == "Status" for k in ("hand", "draw", "discard") for c in state.get(k) or [])
    osty_max = next((p["creature"].get("max_hp", 0) for p in (state.get("pets") or []) if p["name"] == "Osty"), 0)
    debuffs0 = [{p["name"] for p in f.get("powers", []) if GAMEDATA.get("powers", {}).get(p["name"], {}).get("type") == "Debuff"}
                for f in foes]
    shiv = gd_card("Shiv")["vars"].get("Damage", 4)

    def in_hand(st):  # cards in hand now (the played card already left)
        return sum(k for (c, _), k in zip(groups, st[1]) if c["type"] != "Potion") + len(dead) - st[6].tossed

    def counted(card, v, st, tgt=None, hand_n=0):
        energy, counts, en, block, strength, dex, plays, pots, hp, osty, orbs, stars, buffs = st
        t = en[tgt] if tgt is not None else None
        n = {"Barrage": lambda: len(orbs),
             "Finisher": lambda: (atk_before or 0) + plays.attacks,
             "LunarBlast": lambda: skills_before + plays.skills,
             "PullFromBelow": lambda: (state.get("ethereal_played") or 0) + bget(buffs, "EtherealPlayed"),
             "Radiate": lambda: (state.get("stars_gained") or 0) + max(0, stars - state.get("stars", 0)),
             "Flechettes": lambda: max(0, hand_skills - plays.skills),
             "FlakCannon": lambda: n_statuses,
             "TearAsunder": lambda: 1 + (state.get("times_damaged") or 0),
             "Rattle": lambda: 1 + (state.get("osty_attacks") or 0),
             "HelixDrill": lambda: (state.get("energy_spent") or 0) + max(0, state["energy"] - energy),
             "BodySlam": lambda: block, "DemonicShield": lambda: block,
             "AshenStrike": lambda: len(state.get("exhaust") or []) + plays.exhausted,
             "MindBlast": lambda: len(state.get("draw") or []),
             "PerfectedStrike": lambda: sum("Strike" in c["name"] for c in all_cards),
             "CrescentSpear": lambda: sum(star_cost(c) > 0 for c in all_cards),
             "SoulStorm": lambda: sum(c["name"] == "Soul" for c in state.get("exhaust") or []),
             "Stack": lambda: len(state.get("discard") or []) + bget(buffs, "Discarded"),
             "ExpectAFight": lambda: max(0, strength),
             "Mirage": lambda: sum(e[10] for e in en if e[0] > 0),
             "Bully": lambda: t[2] if t else 0, "TimesUp": lambda: t[11] if t else 0,
             "Dismantle": lambda: 2 if t and t[2] > 0 else 1,
             "NoEscape": lambda: t[11] // max(v.get("DoomThreshold", 10), 1) if t else 0,
             "Rend": lambda: len(debuffs0[tgt] | {k for k, i in DEBUFF_SLOT.items() if t[i] > 0}) if t else 0,
             "DeathMarch": lambda: state.get("drawn_turn") or 0,
             "Murder": lambda: state.get("drawn_combat") or 0,
             "MementoMori": lambda: (state.get("discarded_turn") or 0) + bget(buffs, "Discarded"),
             "GoldAxe": lambda: (state.get("plays_combat") or 0) + plays.cards,
             "Supermassive": lambda: (state.get("generated_combat") or 0) + bget(buffs, "Generated"),
             "PreciseCut": lambda: -in_hand(st),
             "Protector": lambda: osty_max if osty > 0 else 0, "Unleash": lambda: osty,
             "Squeeze": lambda: sum(c["name"] in OSTY_ATTACKS for c in all_cards) - 1,
             "Sacrifice": lambda: 3 * osty_max if osty > 0 else 0,
             "Voltaic": lambda: (state.get("lightning_channeled") or 0) + bget(buffs, "Lightning"),
             "KnifeTrap": lambda: sum(c["name"] == "Shiv" for c in state.get("exhaust") or []) + bget(buffs, "Shiv"),
             "CompileDriver": lambda: len({o for o, _ in orbs}),
             "Stardust": lambda: stars, "FiendFire": lambda: hand_n}.get(card["name"])
        if n is None:
            return v
        base, n = v.get("CalculationBase", 0), n()
        if "ExtraDamage" in v:  # CalculatedDamage (Osty's own for Unleash / Protector / Squeeze)
            return {**v, ("OstyDamage" if "OstyDamage" in v else "Damage"): base + v["ExtraDamage"] * n}
        x = max(0, base + v.get("CalculationExtra", 1) * n)
        key = COUNT_KEY.get(card["name"], "Repeat" if "Damage" in v or "OstyDamage" in v else "Block")
        if card["name"] == "KnifeTrap":  # plays every exhausted Shiv. ponytail: as one Skill (no Strength)
            v, key = {**v, "Damage": shiv}, "Repeat"
        if key == "Repeat":
            v = {k: y for k, y in v.items() if k != "Repeat"} | ({"Repeat": x} if x else {})
            return v if x else {k: y for k, y in v.items() if k not in ("Damage", "OstyDamage")}  # 0 hits: no damage
        return {**v, key: x}

    def play(card, tgt, st, g=None):
        hand_n = 0
        if card["name"] in HAND_TOSS and card["type"] != "Potion":  # Fiend Fire / Stoke / Storm of Steel: hand first
            hand_n = in_hand(st)
            st = toss(st, HAND_TOSS[card["name"]], HAND_MAX, "pick")
        energy, counts, en, block, strength, dex, plays, pots, hp, osty, orbs, stars, buffs = st
        potion = card["type"] == "Potion"
        attack = card["type"] == "Attack"
        cost = energy if card["x_cost"] else cost_of(card, plays)
        times = energy + x_bonus(R) if card["x_cost"] else 1
        v = {} if junk(card) else counted(card, card["vars"], st, tgt, hand_n)
        if card["name"] == "SovereignBlade" and mine.get("ParryPower"):  # Parry: the Blade gains Block = Parry amount
            v = {**v, "Block": mine["ParryPower"]}
        # Strangle / Oblivion already on an enemy act on this card (not on the card that applies them)
        tied = [(strangle[i] + bget(buffs, f"Strangle{i}"), oblivion[i] + bget(buffs, f"Oblivion{i}")) for i in range(len(en))]
        if lamp and not bget(buffs, "UnsettlingLamp") and set(v) & ENEMY_DEBUFFS:  # first debuff card: doubled
            v = {k: x * 2 if k in ENEMY_DEBUFFS else x for k, x in v.items()}
            buffs = badd(buffs, "UnsettlingLamp", 1)  # used (a marker in our buffs, not a power)
        en = [list(e) for e in en]

        def gain(n):  # Block we gain; Juggernaut hits a random enemy each time (ponytail: the first living one)
            nonlocal block
            if n > 0:
                block += n
                jug = bget(buffs, "JuggernautPower")
                alive_now = [i for i, e in enumerate(en) if e[0] > 0]
                if jug and alive_now:
                    hit(en[alive_now[0]], jug)
        alive = [i for i, e in enumerate(en) if e[0] > 0]
        targets = alive if card["target"] in ("AllEnemies", "RandomEnemy") else [tgt] if tgt is not None else []
        # RandomEnemy: each hit lands on a random enemy, so each takes the expected share of the hits.
        share = 1 / len(targets) if card["target"] == "RandomEnemy" and targets else 1
        # Galvanized: Unpowered damage on play, so our Block takes it first (GalvanicPower.cs, CreatureCmd.Damage)
        galv = galvanic if card.get("affliction") == "Galvanized" and any(en[i][0] > 0 for i in galv_src) else 0
        lost_now = v.get("HpLoss", 0) + max(0, galv - block)
        block = max(0, block - galv)
        hp -= lost_now
        if v.get("HpLoss", 0) > 0:  # wiki Order + gamedata ops: Hemokinesis / Breakthrough lose HP before they hit,
            strength += rupture     # so Rupture's Strength already adds to their own damage
        heal = v.get("Heal", 0) + me["max_hp"] * v.get("HealPercent", 0) // 100 + v.get("RegenPower", 0)
        hp = min(me["max_hp"], hp + heal) if hp > 0 else hp  # ponytail: Regen counted as one tick now
        if "Damage" in v:
            if potion:  # potion damage ignores Strength/Weak
                base = v["Damage"]
            else:
                acc = mine.get("AccuracyPower", 0) if card["name"] == "Shiv" else 0  # Accuracy: Shivs hit harder
                vigor = bget(buffs, "VigorPower") if attack else 0  # Vigor: added to the next Attack's hits, then gone
                buffs = badd(buffs, "VigorPower", -vigor)
                base = ((v["Damage"] + acc + (strength if attack else 0) + vigor + dmg_add(card, R))
                        * (0.75 if weak and attack else 1) * dmg_mult(card, R, plays))  # Str/Weak: Attacks only
            for i in targets:
                slow = 1 + 0.1 * ((state.get("cards_played") or 0) + plays.cards) if slowed[i] and not potion else 1  # Bygone Effigy: every player's cards
                mult = 1 if potion else (0.5 if en[i][14] > 0 or soar[i] else 1) * (1 if gone_src(shrinker, en) else shrink)
                vm = 1 + vuln_bonus * (2 if debil[i] else 1) if en[i][2] > 0 and attack else 1
                am = ((tracking if tracking and en[i][3] > 0 else 1) * (2 if bget(buffs, "DoubleDamagePower") else 1)
                      * (3 if bget(buffs, "GigantificationPower") else 1)
                      * (max(1, hang[i] + bget(buffs, f"Hang{i}")) if card["name"] == "Hang" else 1)
                      * (1 + lethality if not plays.attacks and not before else 1)) if attack else 1
                per = min(math.floor(base * vm * am * slow * mult), hard_cap[i])
                n_hits = max(v.get("Repeat", 1) if card["name"] != "Spite" or hp < me["hp"] else 1, 1) * times
                hits = [hit(en[i], per) for _ in range(n_hits)] if share == 1 else [hit(en[i], per * n_hits * share)]
                if thorns[i] and not potion:  # Thorns: our block soaks it first
                    back = thorns[i] * n_hits * share
                    soaked = min(block, back)
                    block -= soaked
                    hp -= back - soaked
                if attack and en[i][14] > 0:  # Flutter: counts unblocked Attack hits
                    en[i][14] = max(0, en[i][14] - sum(1 for h in hits if h))
                if sum(hits) and en[i][4] and attack:  # Skittish: Block after the first damaging Attack (not Skills)
                    en[i][1] += en[i][4]
                    en[i][4] = 0
                if attack:  # Personal Hive (Entomancer): each powered attack hit adds Dazed, blocked or not
                    en[i][8] += hive[i] * len(hits)
                if attack and bget(buffs, "EnvenomPower"):  # Envenom: Poison per unblocked hit; Artifact blocks each
                    for _ in range(sum(1 for h in hits if h)):
                        if en[i][9] > 0:
                            en[i][9] -= 1
                        else:
                            en[i][10] += bget(buffs, "EnvenomPower")
        if card["name"] == "Hang" and tgt is not None:  # Hang: 2 Hang, then doubles it (x damage of later Hangs)
            buffs = badd(buffs, f"Hang{tgt}", max(2, hang[tgt] + bget(buffs, f"Hang{tgt}")))
        if card["name"] == "MoltenFist" and tgt is not None and en[tgt][0] > 0:  # doubles the target's Vulnerable
            en[tgt][2] *= 2
        if attack and "Damage" in v and bget(buffs, "GigantificationPower"):  # used up by this Attack
            buffs = badd(buffs, "GigantificationPower", -1)
        if "OstyDamage" in v and osty > 0:  # Osty attacks: no player Strength/Weak
            for i in targets:
                hit(en[i], math.floor(v["OstyDamage"] * (1.5 if en[i][2] > 0 else 1)))
                en[i][8] += hive[i]  # Osty's hits count for Personal Hive too (dealer = its owner)
        if "Summon" in v:  # raises Osty's HP, or revives it
            osty += v["Summon"]
        stars += v.get("Stars", 0) - (0 if potion else star_cost(card))
        if card["name"] == "Stardust":  # X stars: spends them all (one hit each, counted above)
            stars = 0
        for name in [] if potion else channels(card) * int(v.get("Channels", 1)):
            buffs = badd(buffs, "Lightning", name == "Lightning")  # Voltaic counts them
            if slots and len(orbs) >= slots:
                block, energy = evoke(orbs[0], en, block, energy)
                orbs = orbs[1:]
            if slots:
                orbs = orbs + ((name, {"Dark": 6, "Glass": 4}.get(name)),)
        # Orb cards (decomp OnPlay): Dualcast / Quadcast evoke the first orb 2 / 4 times, Shatter evokes each orb twice
        # (nothing without orbs); Darkness: each Dark orb's passive (+6+Focus to its evoke, twice upgraded); Tesla Coil:
        # each Lightning's passive at the target (twice upgraded).
        if card["name"] in ("Dualcast", "Quadcast") and orbs and not potion:
            for _ in range(int(v.get("Repeat", 2))):
                block, energy = evoke(orbs[0], en, block, energy)
            orbs = orbs[1:]
        elif card["name"] == "Shatter":
            for o in orbs:
                for _ in range(2):
                    block, energy = evoke(o, en, block, energy)
            orbs = ()
        elif card["name"] == "Darkness":
            grow = (ORB_PASSIVE["Dark"] + focus) * (2 if card["upgraded"] else 1)
            orbs = tuple((o, (x or 6) + grow if o == "Dark" else x) for o, x in orbs)
        elif card["name"] == "TeslaCoil" and tgt is not None:
            for o, _ in orbs:
                for _ in range((2 if card["upgraded"] else 1) if o == "Lightning" else 0):
                    hit(en[tgt], ORB_PASSIVE["Lightning"] + focus)
        if "Block" in v:
            extra = fasten if card["name"].startswith("Defend") else 0
            gained = v["Block"] if potion else max(0, math.floor((v["Block"] + extra + dex) * (0.75 if frail else 1))) * times
            if not potion:
                gained *= (0 if no_block else 2 if plays.block < unmovable else 1) * 2 ** bget(buffs, "ShadowmeldPower")
            gain(gained * (1 if potion else block_mult(R, plays, card)))
        def now(key):  # our own gain that lands now (not Demon Form's later turns, Rupture's trigger, ...)
            if potion or card.get("ops"):
                return v.get(key, 0)
            sign, when = timing(card["name"], key)
            if when == "if":  # Forgotten Ritual (v0.107.1): "If you Exhausted a card this turn" (before its own)
                return sign * v.get(key, 0) if exhausted_before + plays.exhausted else 0
            return sign * v.get(key, 0) if when in ("now", "this turn") else 0
        if card["type"] == "Power" or card["target"] in ("Self", "None"):
            strength += now("StrengthPower")
            dex += now("DexterityPower")
            energy += now("Energy")
        else:
            if card["target"] in ("AnyEnemy", "AllEnemies", "RandomEnemy"):
                # "Deal X. Gain Y Strength" (Setup Strike, Fight Me!, Resonance): ours, after the damage above
                strength += now("StrengthPower")
                dex += now("DexterityPower")
            for i in targets:  # enemy Strength this turn: Fight Me! gives it, Piercing Wail / Dark Shackles take it
                if v.get("StrengthLoss") and en[i][9] > 0:  # Artifact blocks the Strength loss (a debuff)
                    en[i][9] -= 1
                    en[i][12] += v.get("EnemyStrength", 0)
                else:
                    en[i][12] += v.get("EnemyStrength", 0) - v.get("StrengthLoss", 0)
            for i in targets:
                # in the card's own order (ops order): Know Thy Place's Weak before its Vulnerable vs 1 Artifact
                for key in [k for k in v if k in DEBUFF_SLOT]:
                    slot = DEBUFF_SLOT[key]
                    if v.get(key):
                        if en[i][9] > 0:  # Artifact blocks one debuff per stack
                            en[i][9] -= 1
                        elif slot == 10:  # Bouncing Flask: Repeat bounces, split over the enemies it can hit
                            en[i][slot] += v[key] * max(v.get("Repeat", 1), 1) * times // max(len(targets), 1)                                 if card["target"] == "RandomEnemy" else v[key] * times
                        else:
                            en[i][slot] += v[key]
                            if slot == 11:
                                gain(shroud)  # Shroud: Block whenever we apply Doom
                        if en[i][slot] and bget(buffs, "SleightOfFleshPower"):  # a debuff landed (not Artifact)
                            hit(en[i], bget(buffs, "SleightOfFleshPower"))
                for key in ("Strangle", "Oblivion"):
                    if v.get(key + "Power"):
                        buffs = badd(buffs, f"{key}{i}", v[key + "Power"])
            if v.get("StrengthPerVulnerable") and targets:  # Dominate: after its own Vulnerable lands
                strength += v["StrengthPerVulnerable"] * en[targets[0]][2]
        energy -= cost
        if attack and juggling and atk_before is not None and atk_before + plays.attacks == 2 and g is not None:  # third Attack: copies
            counts = counts[:g] + (counts[g] + juggling,) + counts[g + 1:]
        if attack and music and plays.attacks == 0 and g is not None:  # Music Box: an Ethereal copy of the first Attack
            counts = counts[:g] + (counts[g] + 1,) + counts[g + 1:]
        for mg, n in makes.get(g, ()):  # made/fetched cards land in hand, playable later this turn (max 10 in hand)
            room = HAND_MAX - len(dead) + plays.tossed - sum(k for (c, _), k in zip(groups, counts) if c["type"] != "Potion")
            n = hand_n if card["name"] == "StormOfSteel" else n  # a Shiv per card discarded
            made_n = max(0, min(n * times, room))
            counts = counts[:mg] + (counts[mg] + made_n,) + counts[mg + 1:]
            buffs = badd(buffs, "Generated", made_n)  # Supermassive counts them
        if potion:
            pots += 1
        else:
            t = card["type"]
            # ops cards count their exhausts in toss() below; others by text (True Grit, Burning Pact...)
            exhausting = exhausts(card) or (not card.get("ops") and exhausts_others(card))
            plays = plays._replace(attacks=plays.attacks + (t == "Attack"), skills=plays.skills + (t == "Skill"),
                                   powers=plays.powers + (t == "Power"), cards=plays.cards + 1,
                                   block=plays.block + ("Block" in v), exhausted=plays.exhausted + exhausting)
            if "Ethereal" in keywords(card):  # Pull From Below: hits once per Ethereal card played this combat
                buffs = badd(buffs, "EtherealPlayed", 1)
            if card["name"] == "Shiv":  # Shivs exhaust: Knife Trap plays them again
                buffs = badd(buffs, "Shiv", 1)
            # Powers we already had when this card was played: Rage (Attack), Afterimage (any card), Feel No Pain
            gain(bget(buffs, "RagePower") if attack else 0)
            gain(bget(buffs, "AfterimagePower"))
            gain(bget(buffs, "FeelNoPainPower") if exhausting else 0)
            fx = after_play(card, cost, R, plays)
            strength += fx.get("str", 0) - tender
            dex += fx.get("dex", 0) - tender
            gain(fx.get("block", 0))
            energy += fx.get("energy", 0)
            for i in [i for i, e in enumerate(en) if e[0] > 0]:
                hit(en[i], fx.get("dmg_all", 0))
            alive = [i for i, e in enumerate(en) if e[0] > 0]
            if fx.get("dmg_one") and alive:  # ponytail: game picks a random enemy; we assume the first
                hit(en[alive[0]], fx["dmg_one"])
            for e, (sx, ox) in zip(en, tied):  # Strangle: HP loss, Oblivion: Doom, per card played after them
                if e[0] > 0:
                    lose(e, sx)
                    e[11] += ox
            drew = moves[g][0] if g is not None else 0
            statuses = sum(n for mg, n in makes.get(g, ()) if groups[mg][0]["type"] == "Status")
            stars_moved = (star_cost(card) > 0) + (v.get("Stars", 0) > 0)
            aoe = (bget(buffs, "PanachePower") * ((before + plays.cards) % 5 == 0) + bget(buffs, "SpeedsterPower") * drew
                   + bget(buffs, "SmokestackPower") * statuses + bget(buffs, "BlackHolePower") * stars_moved)
            alive = [i for i, e in enumerate(en) if e[0] > 0]
            for i in alive:
                hit(en[i], aoe)
                if card["name"] == "Soul":  # Haunt: a random enemy = expected share each
                    lose(en[i], bget(buffs, "HauntPower") / len(alive))
                if bget(buffs, "CorrosiveWavePower") * drew and not en[i][9]:
                    en[i][10] += bget(buffs, "CorrosiveWavePower") * drew
        for name, n in card_buffs(card)[0].items():  # powers this card gives us (Stone Armor -> Plating...)
            buffs = badd(buffs, name, n)
        for i, s_str, s_dex in possess:  # its death gives our stolen Strength / Dexterity back at once
            if en[i][0] == 0 and start[2][i][0] > 0 and not bget(buffs, f"Possessed{i}"):
                strength, dex = strength + s_str, dex + s_dex
                buffs = badd(buffs, f"Possessed{i}", 1)
        for i in crabs:  # Crab Rage: its partner just died -> 99 Block right away (Strength: enemy_hits)
            if en[i][0] > 0 and not bget(buffs, f"CrabRaged{i}") and any(
                    start[2][j][0] > 0 and en[j][0] == 0 for j in range(len(en)) if j != i):
                en[i][1] += CRAB_RAGE_BLOCK
                buffs = badd(buffs, f"CrabRaged{i}", 1)
        if lost_now > 0:  # Galvanized's HP loss lands after the card resolves (Rupture: + Strength); Inferno: ALL enemies
            strength += rupture if lost_now > v.get("HpLoss", 0) else 0
            for e in en:
                if e[0] > 0 and inferno:
                    hit(e, inferno)
        st = (energy, counts, tuple(map(tuple, en)), block, strength, dex, plays, pots, hp, osty, orbs, stars, buffs)
        for kind, n, how in card.get("op_toss", ()):
            st = toss(st, kind, n, how)
        return st

    def toss(st, kind, n, how):
        """A card's discard/exhaust from hand: we pick the worst card (toss_key: junk, Sly, then lowest worth_next);
        a random one (True Grit) costs the mean exhaust loss. Exhausts give Feel No Pain Block; a discarded Sly
        card is played for free. ponytail: cards it draws first aren't candidates (unknown)."""
        energy, counts, en, block, strength, dex, plays, pots, hp, osty, orbs, stars, buffs = st
        gone, tossed, lost, out, sly = list(plays.gone or (0,) * len(groups)), plays.tossed, plays.lost, 0, []
        exhaust = kind == "exhaust"

        def loss(c):  # score lost by exhausting c for the fight (junk: a small gain)
            return -EXHAUST_JUNK if junk(c) else EXHAUST_WEIGHT * redraws * worth(c) if c["name"] in deck_names else 0
        for _ in range(n):
            hand = [(c, None) for c in dead[tossed:]] + [(c, g) for g, (c, _) in enumerate(groups)
                                                         if counts[g] and c["type"] != "Potion" for _ in range(counts[g])]
            if not hand:
                break
            if how == "random":
                if exhaust:
                    lost += sum(loss(c) for c, _ in hand) / len(hand)
                    out += 1
                continue
            c, g = min(hand, key=lambda x: toss_key(x[0], not exhaust, lambda c: SANDPIT_VALUE if c["name"] == ESCAPE
                                                    else worth_next(c) if x[1] is not None and not junk(c) else -harm(c)))
            if g is None:
                tossed += 1
            else:
                counts = counts[:g] + (counts[g] - 1,) + counts[g + 1:]
                gone[g] += 1
                if not exhaust and "Sly" in keywords(c):
                    sly.append(c)
            if exhaust:
                lost += loss(c)
                out += 1
            else:  # Memento Mori / Stack count discards
                buffs = badd(buffs, "Discarded", 1)
        block += bget(buffs, "FeelNoPainPower") * out  # ponytail: Juggernaut isn't triggered by this Block
        plays = plays._replace(tossed=tossed, gone=tuple(gone), lost=lost, exhausted=plays.exhausted + out)
        st = (energy, counts, en, block, strength, dex, plays, pots, hp, osty, orbs, stars, buffs)
        for c in sly:  # auto-plays face the same limits (Normality, Sloth, Ringing, Smog, Bound): else just discarded
            if not allowed(c, st[6], (), st[12], auto=True):
                continue
            if c.get("affliction") == "Bound":
                st = st[:12] + (badd(st[12], "BoundPlayed", 1),)
            alive = [i for i, e in enumerate(en) if e[0] > 0]
            st = play({**c, "cost": 0, "x_cost": False}, alive[0] if alive and c["target"] == "AnyEnemy" else None, st)
        return st

    def evaluate(st, steps):
        energy, counts, en, block, _, _, plays, pots, hp, osty, orbs, _, buffs = st
        gone = plays.gone or (0,) * len(groups)  # cards a pick took from hand: not played
        rest = dead[plays.tossed:]  # unplayable cards still in hand at turn end
        # cards in hand at turn end (Regret, Runic Pyramid...): unplayables left + every unplayed card group, so
        # made cards played (Shivs, copies) don't count against the starting hand
        left = len(dead) - plays.tossed + sum(k for (c, _), k in zip(groups, counts) if c["type"] != "Potion")
        extra, relic_dmg = end_turn(R, block, left, state.get("turn", 0), plays)
        # Turn end: Orichalcum (very early, saw the Block before Plating), then Plating's Block and Regen (early).
        block += extra + bget(buffs, "PlatingPower")
        hp = min(me["max_hp"], hp + mine.get("RegenPower", 0)) if hp > 0 else hp
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
            if e[0] > 0 and e[10] > 0:  # HP loss: Block doesn't stop it; Intangible / Slippery / Hardened Shell do
                tick = 1 if e[7] > 0 else e[10]
                if e[6] > 0:
                    tick, e[6] = 1, e[6] - 1
                if e[5] >= 0:
                    tick = min(tick, e[5])
                    e[5] -= tick
                e[0] -= min(e[0], tick)
                poison_future += min(e[0], e[10] * (e[10] - 1) // 2)
        if leaders and all(e[0] == 0 or m for e, m in zip(en, minion)):
            for e in en:
                e[0] = 0
        start_en = start[2]
        kills = sum(1 for a, b in zip(start_en, en) if a[0] > 0 and b[0] == 0)
        win = all(e[0] == 0 for e in en) and hp > 0 and not any(revives)
        held = sum(map(end_dmg, rest)) + sum(n * end_dmg(c) for (c, _), n in zip(groups, counts))
        taints = sum(start[1][g] - counts[g] - gone[g] for g, (c, _) in enumerate(groups) if c.get("affliction") == "Tainted")
        facing = facing_after(steps) if surround else None
        if surround:  # a kill that leaves every enemy on one side turns us to face them (SurroundedPower.AfterDeath)
            living = {side.get(f.get("combat_id")) for f, e in zip(foes, en) if e[0] > 0}
            if len(living) == 1 and None not in living and any(a[0] > 0 and b[0] == 0 for a, b in zip(start_en, en)):
                facing = living.pop()
        hits = [] if win else enemy_hits(en, plays.skills, tainted_now + spark * taints, facing,
                                         bget(buffs, "ColossusPower") or amount(me["powers"], "ColossusPower"))
        if before + plays.cards <= diadem:
            hits = [[h // 2 for h in hs] for hs in hits]
        if "UndyingSigil" in R:
            hits = [[h // 2 for h in hs] if e[0] > 0 and e[11] >= e[0] else hs for hs, e in zip(hits, en)]
        skip_turn = eye and plays.cards == 0
        if skip_turn:
            hits = []
        attacks = sum(map(sum, hits))
        unblockable = (sum(c["name"] == "Regret" for c in rest) * left + sum(map(end_loss, rest))
                       + sum(n * end_loss(c) for (c, _), n in zip(groups, counts)))
        # Wither cards added this turn: Withering Presence's CardsLeft (reader; it carries over between turns)
        withers = (0 if plays.cards < wither_left else 1 + (plays.cards - wither_left) // 6) if withering else 0
        lost, soaked, block_left = (0, 0, block) if win else take_hits(hits, block, osty, buffs,
                                                                       burn - constrict * gone_src(strangler, en)
                                                                       + held + wither_dmg * withers, unblockable)
        stabbed = 0 if win else wounds
        sucked = (sum(pw(f, "SuckPower") * len(hs) for f, hs in zip(foes, hits) if hs) * SUCK_TURNS
                  if attacks > block else 0)  # ponytail: any unblocked hit counts for every Suck enemy
        # Slumber (SlumberPower.cs): -1 per hit that gets through Block, wakes at 0 (its turn end takes 1 anyway).
        # ponytail: hits counted from the plan's Attacks on it (all its hits assumed past Block once HP dropped).
        def hits_on(i):
            return sum(max(st_c["vars"].get("Repeat", 1), 1) for _, _, _, st_c, cid, _ in steps if st_c["type"] == "Attack"
                       and (st_c["target"] in ("AllEnemies", "RandomEnemy") or cid == foes[i].get("combat_id")))
        woken = sum(1 for i, (f, a, b) in enumerate(zip(foes, start_en, en)) if 0 < b[0] < a[0]
                    and (pw(f, "AsleepPower") or 1 < pw(f, "SlumberPower") <= hits_on(i)))
        hp_after = hp - lost
        if my_doom and hp_after <= my_doom and not win:
            hp_after = min(hp_after, 0)  # our Doom kills us at the end of the enemy turn
        back = bget(buffs, "ThornsPower") + bget(buffs, "FlameBarrierPower")  # hit back per enemy attack hit
        if back and hits:
            for e, hs in zip(en, hits):
                if e[0] > 0 and hs:
                    for _ in hs:  # per hit (Hard to Kill caps each)
                        hit(e, back)  # ponytail: a kill here doesn't cancel that enemy's later hits
            kills = sum(1 for a, b in zip(start_en, en) if a[0] > 0 and b[0] == 0)
        if soaked and bget(buffs, "NecroMasteryPower"):  # Osty's HP lost -> every enemy loses that much
            for e in en:
                if e[0] > 0:
                    lose(e, soaked * bget(buffs, "NecroMasteryPower"))
            kills = sum(1 for a, b in zip(start_en, en) if a[0] > 0 and b[0] == 0)
        carried = block_left if bget(buffs, "BarricadePower") or bget(buffs, "BlurPower") else 0  # kept next turn
        escapes = sum(start[1][g] - counts[g] - gone[g] for g in escape_groups)
        sand_left = sandpit + escapes
        if sandpit and sand_left <= 1 and not win:  # ticks down at enemy turn start; at 0 it kills you
            hp_after = min(hp_after, 0)  # the Sandpit swallows you
        net_loss = me["hp"] - hp_after
        # dies at the end of the enemy turn: Doom >= HP, or Demise (Powdered Demise) takes its last HP
        doomed = [e[0] > 0 and (e[11] >= e[0] or 0 < demise[i] >= e[0]) for i, e in enumerate(en)]
        kills += sum(doomed)
        damage = sum(a[0] - b[0] for a, b in zip(start_en, en))
        weighted = sum(w * (a[0] - b[0] + (b[0] if d else 0)) for w, a, b, d in zip(weight, start_en, en, doomed))
        # A segment at 0 while another Decimillipede segment lives reattaches; an Illusion at 0 while a leader
        # (non-minion) lives revives at full HP: not kills, still threats later (they skip one turn to come back)
        seg_left = any(x[0] > 0 for x, r in zip(en, reattach) if r)
        lead_left = any(x[0] > 0 for x, m in zip(en, minion) if not m)
        back_up = [e[0] == 0 and (r and seg_left or il and lead_left) for e, r, il in zip(en, reattach, illusion)]
        kills -= sum(b and a[0] > 0 for b, a in zip(back_up, start_en))
        alive = [(e[0] > 0 or b) and not d for e, d, b in zip(en, doomed, back_up)]
        unhexed = gone_src(knight, en)  # Spectral Knight dead: Hexed cards lose the Ethereal Hex gave them
        ethereal = sum(n for (c, _), n in zip(groups, counts) if not junk(c) and "Ethereal" in keywords(
            {**c, "affliction": None} if unhexed and c.get("affliction") == "Hexed" else c))
        played = [(c, start[1][g] - counts[g] - gone[g]) for g, (c, _) in enumerate(groups) if c["type"] != "Potion"]
        exhaust_cost = sum(n * (-EXHAUST_JUNK if junk(c) else EXHAUST_WEIGHT * redraws * worth(c))
                           for c, n in played if n > 0 and exhausts(c) and (junk(c) or c["name"] in deck_names))
        if skip_turn:  # Pael's Eye exhausts the hand we kept
            exhaust_cost += sum(n * EXHAUST_WEIGHT * redraws * worth(c) for (c, _), n in zip(groups, counts)
                                if n and not junk(c) and c["type"] != "Potion" and c["name"] in deck_names)
        vuln = any(e[2] > 1 for e, a in zip(en, alive) if a)  # still Vulnerable next turn
        retained = sum(n * RETAIN_KEEP * worth_next(c, vuln) for (c, _), n in zip(groups, counts)
                       if n and not junk(c) and "Retain" in keywords(c) and "Ethereal" not in keywords(c))
        # Put on top of the draw pile = next hand: hand -> top (Thinking Ahead) keeps a leftover card, discard ->
        # top (Headbutt) brings back a discarded or just-played one.
        used = [(c, start[1][g] - counts[g] - gone[g], moves[g]) for g, (c, _) in enumerate(groups)]

        def tops(src, cands):
            n = sum(k * m[1]["count"] for c, k, m in used if k and m[1] and m[1]["from"] == src
                    and m[1]["to"] == "Draw" and m[1]["top"])
            return sorted(cands, key=lambda c: -worth_next(c, vuln))[:n] if n else []
        on_top = list(kept) + (tops("Hand", [c for (c, _), k in zip(groups, counts) for _ in range(k)
                                if not junk(c) and c["type"] != "Potion" and "Retain" not in keywords(c)])
                  + tops("Discard", piles["Discard"] + [c for c, k, m in used if k and c["type"] != "Potion"
                                                          and not exhausts(c) and not m[1]]))
        retained += sum(RETAIN_KEEP * worth_next(c, vuln) for c in on_top)
        top_block = sum(c["vars"].get("Block", 0) for c in on_top)
        drawn = sum(k * m[0] for c, k, m in used) + (0 if no_draw else sum(relic_draws(R, plays, before)))
        if soulbound:  # ponytail: only Souls made into hand (makes) are counted; valued like a kept card
            souls = sum(k * n for g, (c, k, m) in enumerate(used) for mg, n in makes.get(g, ())
                        if groups[mg][0]["name"] == "Soul")
            retained += souls * soulbound * RETAIN_KEEP * worth_next(gd_card("Soul"), vuln)
        if drawn and pool and state.get("draw_ordered") and state.get("draw"):
            top = pool[:drawn]  # the real top cards (draw_hand); past the pile's end: its mean
            retained += (sum(w for cost, w in top if 0 <= cost <= energy) + max(0, drawn - len(top))
                         * sum(w for cost, w in pool if 0 <= cost <= energy) / len(pool))
        else:
            retained += drawn * sum(w for cost, w in pool if 0 <= cost <= energy) / len(pool) if drawn and pool else 0
        blocked_all = block >= attacks
        killed = [a[0] > 0 and b[0] == 0 for a, b in zip(start_en, en)]
        # Enemy Weak ticks down at the end of each enemy turn: Weak w after this plan still cuts forecast turn k
        # (0 = the enemy turn after this one) while w > k + 1. Weak 3 now: this enemy turn and the next two.
        cut = [[1 - weak_cut * (2 if debil[i] else 1) * (e[3] > k + 1) for k in range(len(fc))]
               for i, (e, fc) in enumerate(zip(en, forecast))]
        nxt = [n * (c[0] if c else 1) for n, c in zip(next_turn, cut)]
        fut = [sum(t["dmg"] * FUTURE_DISCOUNT ** k * c[k] for k, t in enumerate(fc[:RESUMMON_TURNS] if r else fc))
               for fc, c, r in zip(forecast, cut, respawn)]
        hit_next = (sum(n * (BACK_ATTACK if behind(f, facing) else 1)
                        for n, a, ob, f in zip(nxt, alive, off_balance, foes) if a and not (ob and blocked_all))
                    + sum(x for x, k in zip(erupt, killed) if k))
        future_left = (sum(fu for fu, a in zip(fut, alive) if a) + sum(x for x, k in zip(erupt, killed) if k)
                       - hit_next + exposure(hit_next, carried))
        # User: the eruption can't be dodged (the fight only ends when it explodes) and grows ERUPT_STEP a turn while
        # the Giant lives, so a living one carries it too, plus a step: killing it now is never the costlier choice.
        future_left += sum(x + ERUPT_STEP for x, a, e0 in zip(erupt, alive, start_en) if a and x and e0[0] < UNKILLABLE)
        future_left -= sum(n for n, a, ob in zip(nxt, alive, off_balance) if a and ob and blocked_all)
        future_left += (sucked + WAKE_COST * woken) * (not win)
        pool_e = sum(max(c, 1) for c, w in pool if c >= 0)
        # a thief about to escape (Escape Artist at 1) but stunned now stays a turn: half a chance at its loot
        held = [e[0] > 0 and a[14] > 0 and e[14] == 0 and 0 < pw(f, "EscapeArtistPower") <= 1
                for a, e, f in zip(start_en, en, foes)]
        retained += (hp_w * (sum(x for x, k in zip(loot, killed) if k) + STUN_KEEP * sum(x for x, h in zip(loot, held) if h))
                     + ((tears * (energy >= 1) + art * (plays.attacks == 0)) * sum(w for c, w in pool if c >= 0) / pool_e
                        if pool_e else 0))
        projected = min(hp_after, me["hp"]) - exposure(hit_next, top_block + carried)
        unknown = sum(1 for s in steps if not s[2])
        score = (W_WIN * win + W_KILL * kills + value_per_damage * weighted
                 - hp_w * max(0, net_loss - free_hp) * danger(projected, me["max_hp"])
                 - hp_w * FUTURE_WEIGHT * future_left * (not win)
                 + UNKNOWN_VALUE * unknown - W_DEATH * (hp_after <= 0) - sum(potion_price(s[3]) for s in steps if s[3]["type"] == "Potion")
                 + (SANDPIT_VALUE * escapes / max(sand_left, 1) if sandpit else 0)
                 - (value_per_damage * SANDPIT_RACE * max(0, sum(en[i][0] for i in pit_owners)
                                                           - team_dpt * (sand_left - 1)) if sandpit and not win else 0)
                 - DAZED_COST * (sum(e[8] for e in en) + stabbed)
                 + value_per_damage * POISON_FUTURE * poison_future - hp_w * OSTY_HP * soaked
                 + vuln_later(en, alive)
                 - sum(DEBUFF_CUT * (value_per_damage * our_dpt if DEBUFF_CURSES[c["name"]] == "WeakPower"
                                     else fut_w * 0.5 * deck_dpt(state, "Block", 0))
                       for c in rest if c["name"] in DEBUFF_CURSES)
                 - ETHEREAL_LOSS * ethereal - exhaust_cost - plays.lost + retained + (0 if win else lasting(steps))
                 + sum(ORB_HELD.get(n, 0) + (value_per_damage * FUTURE_WEIGHT * ((v or 6) + ORB_PASSIVE["Dark"] + focus)
                                             if n == "Dark" else 0) for n, v in orbs))
        return Plan(list(steps), damage, block - start[3], net_loss, kills, win, score, energy, end=tuple(map(tuple, en)),
                    drawn=int(drawn), plays=plays.cards, on_top=tuple(on_top))

    def allowed(card, plays, steps, buffs, auto=False):
        """Play limits: Sloth cap, Smog (decompiled SmoggyPower: after our first Skill every Skill gets Smog, and a Smog
        card can't be played: one Skill per turn; a Smog card in hand means it's already used), Ringing (only as
        the first card of the turn),
        Bound (one per turn, counting one played before this replan: reader's bound_played).
        auto: a Sly auto-play (the game checks the same limits; only Enthralled lets auto-plays through)."""
        aff = card.get("affliction")
        gone = plays.gone or (0,) * len(groups)
        # Normality / Enthralled only bind while they're still in hand (a discard or exhaust removes them)
        if any(c["name"] == "Normality" for c in dead[plays.tossed:]) and before + plays.cards >= 3:
            return False  # Normality in hand: max 3 cards this turn
        enth_left = enthralled - sum(s[3]["name"] == "Enthralled" for s in steps) - sum(
            n for (c, _), n in zip(groups, gone) if c["name"] == "Enthralled")
        if not auto and card["name"] != "Enthralled" and enth_left > 0:
            return False  # Enthralled in hand must be played before anything else
        if aff == "Smog" or (card["type"] == "Skill" and smoggy and plays.skills + skills_before >= 1):
            return False
        return (plays.cards < cap and not (aff == "Ringing" and before + plays.cards)
                and not (aff == "Bound" and (state.get("bound_played") or bget(buffs, "BoundPlayed"))))

    plans, seen = [], set()
    deadline = time.monotonic() + seconds

    def dfs(st, steps):
        key = (st, facing_after(steps)) if surround else st  # same cards, other facing = a different outcome
        if key in seen or time.monotonic() > deadline:
            return
        seen.add(key)
        plans.append(evaluate(st, steps))
        energy, counts, en = st[0], st[1], st[2]
        for g in order:
            card = groups[g][0]
            if counts[g] == 0 or (cost_of(card, st[6]) > energy and not card["x_cost"]) or (card["x_cost"] and energy == 0):
                continue
            if card["name"] in NEEDS_ORBS and not st[10]:  # Dualcast with no orb: does nothing
                continue
            if card["type"] != "Potion" and star_cost(card) > st[11]:
                continue
            if card["type"] != "Potion" and not allowed(card, st[6], steps, st[12]):
                continue
            if (card["type"] != "Potion" and timing(card["name"], "Energy")[1] == "if"
                    and not exhausted_before + st[6].exhausted):
                continue  # Forgotten Ritual (v0.107.1) before any exhaust: it would just burn itself for nothing
            nxt = counts[:g] + (counts[g] - 1,) + counts[g + 1:]
            potion = card["type"] == "Potion"
            pm = any(moves[g]) or bool(no_draw and set(card["vars"]) & PILE_VARS)  # No Draw: known to do nothing
            vs = (set(card["vars"]) - PILE_VARS if pm else set(card["vars"])) - card_buffs(card)[1]
            modeled = potion or junk(card) or card.get("ops") or card["name"] in COUNTED or bool(engine_powers(card)) or ((bool(vs & MODELED) or bool(channels(card)) or pm or bool(card_buffs(card)[0]))
                                               and vs <= MODELED
                                               and not deferred(card))
            label = ("POTION " if potion else "") + card["name"] + ("+" if card["upgraded"] else "")
            targets = [i for i, e in enumerate(en) if e[0] > 0] if card["target"] == "AnyEnemy" else [None]
            # Bound: marked before it resolves (the game flags it in BeforeCardPlayed), so a Sly Bound card it
            # discards doesn't auto-play
            b12 = badd(st[12], "BoundPlayed", 1) if card.get("affliction") == "Bound" and not potion else st[12]
            for t in targets:
                s2 = play(card, t, (st[0], nxt) + st[2:12] + (b12,), g)
                if not potion:  # played an extra time: Strength / Vulnerable gained so far count again
                    extra = next((n for n, ok in (("DuplicationPower", True), ("OneTwoPunchPower", card["type"] == "Attack"),
                                                  ("BurstPower", card["type"] == "Skill")) if ok and bget(st[12], n)), None)
                    if extra or echo > before + st[6].cards:
                        paid = st[0] if card["x_cost"] else cost_of(card, st[6])  # the copy is free (same X)
                        buffs2 = badd(s2[12], extra, -1) if extra else s2[12]
                        s2 = play(card, t, (s2[0] + paid,) + s2[1:12] + (buffs2,), g)
                dfs(s2, steps + [(label, names[t] if t is not None else None, modeled, card,
                                  foes[t].get("combat_id") if t is not None else None,
                                  "potion" if potion else "card")])

    # Strength / Attack-multiplier sources are tried first, then Vulnerable: of two orders reaching the same state the first is kept,
    # so a Strength Potion / Bash that wins either way comes before the attacks (more damage if a replan falls short).
    def boost(c):  # gives Strength or an Attack multiplier for this turn
        return c["vars"].get("StrengthPower", 0) > 0 or any(k in card_buffs(c)[0] for k in (
            "GigantificationPower", "DoubleDamagePower", "VigorPower"))
    order = sorted(range(len(groups)), key=lambda g: (not boost(groups[g][0]),
                                                      groups[g][0]["vars"].get("VulnerablePower", 0) <= 0))
    dfs(start, [])
    # Equal scores: spend the energy (a harmless extra Defend beats ending the turn early; not once the fight is
    # won), then the shorter plan.
    plans.sort(key=lambda p: (-round(p.score, 6), 0 if p.win else p.energy_left, len(p.steps)))
    if ahead and not kept and seconds >= SOLVE_SECONDS and "draw" in state and fight_type(state) in ("Elite", "Boss"):
        plans = look_ahead(state, plans, deadline)
    # a card potion's stand-in plays stay in the score but leave the steps: the real cards are only known once
    # offered, and the plan is redone then
    real = lambda st: not (isinstance(st[3], dict) and st[3].get("stand_in"))
    return [dc_replace(p, steps=[st for st in p.steps if real(st)], guessed=any(not real(st) for st in p.steps))
            for p in plans[:top]]
