"""One-turn card-sequence solver for the JSON state printed by reader.exe."""
import collections
import functools
import math
import random
import re
from dataclasses import dataclass

from archetypes import card_text, expand_power
from enemies import predict
from relics import (GAMEDATA, NO_PLAYS, Plays, Relics, after_play, block_mult, dmg_add, dmg_mult,
                    end_turn, x_bonus)

# Score weights. Tune these.
W_WIN, W_KILL, W_HP, W_DEATH = 100, 8, 2, 1000  # kills: tie-breaker; their saved hits are in the future terms
UNKNOWN_VALUE = 2  # flat credit for effects we don't model (draw, exhaust, most powers)
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
# Enemy powers (research/effects-audit.md). Killing these doesn't end the fight: Waterfall Giant erupts for its
# Steam Eruption at the end of our next turn, Test Subject revives, Axebot restocks, Decimillipede segments
# reattach, Phrog Parasite bursts into Wrigglers, Illusions revive, Gremlin Merc's Surprise spawns gremlins.
REVIVE_POWERS = {"SteamEruptionPower", "AdaptablePower", "StockPower", "ReattachPower", "InfestedPower",
                 "IllusionPower", "SurprisePower"}
BACK_ATTACK = 1.5  # Surrounded: hits from behind


def behind(f, facing):
    """True if enemy f is behind us (we face `facing`, it attacks from the other side)."""
    return (facing == "Right" and amount(f["creature"]["powers"], "BackAttackLeftPower") > 0
            or facing == "Left" and amount(f["creature"]["powers"], "BackAttackRightPower") > 0)


CRAB_RAGE_STR = 6  # Crusher/Rocket: the survivor gains this Strength (and 99 Block) when its partner dies
UNKILLABLE = 10 ** 8  # a dead Waterfall Giant waiting to erupt sits at 999999999 HP: damage to it is wasted
# Thieves: a stolen card (Swipe, Thieving Hopper) or gold (Heist, Fat Gremlin) comes back only if the thief dies
# before it escapes; Gremlin Merc's Thievery steals more gold each attack. Valued like pathing: a card reward
# (4) / HP (0.3) ~ 13 HP; 6 per 100 gold -> 0.2 HP a gold.
CARD_LOOT_HP, GOLD_LOOT_HP = 13, 0.2
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
# The Insatiable: Frantic Escape (Status, 1 energy) pushes its Sandpit countdown back by 1. Worth more the
# lower the countdown; letting it reach 0 kills you.
ESCAPE, SANDPIT_VALUE = "FranticEscape", 12
DAZED_COST = 1.5  # score per Dazed shuffled into our deck (Entomancer's Personal Hive): prefer big hits
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
    """Cards exhausted earlier this turn (before this plan), from the exhaust pile's growth since the turn started.
    ponytail: undercounts if the first snapshot of the turn comes after an exhaust; use a reader counter if one appears."""
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
    made = GAMEDATA.get("cards", {}).get(card["name"], {}).get("creates")
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
OP_VARS = {"block": "Block", "energy": "Energy", "stars": "Stars", "summon": "Summon",
           "heal": "Heal", "hp_loss": "HpLoss"}
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
        k = a[1:] if neg else a
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
    return c["type"] not in ("Curse", "Status"), not (discard and "Sly" in keywords(c)), value(c)


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
        if cands:
            out.append((cands[int(q * (len(cands) - 1))], n))
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


def next_draw(state):
    """Cards drawn next turn: HAND + Pael's Blood + Ring of the Drake (first N turns).
    ponytail: Pocketwatch (<= 3 cards played) and Pollinous Core (every 4th turn) aren't counted."""
    R = Relics(state.get("relics", []), GAMEDATA["relics"])
    n = HAND + (R.var("PaelsBlood", "Cards") if "PaelsBlood" in R else 0)
    if "RingOfTheDrake" in R and (state.get("turn") or 0) + 1 <= R.var("RingOfTheDrake", "Turns"):
        n += R.var("RingOfTheDrake", "Cards")
    return int(n)


def next_blocks(state, dex):
    """Block each of EXPOSURE_SAMPLES likely next hands can make with next turn's energy (greedy, Block per
    energy). Fewer than HAND cards left to draw: the rest come from the reshuffled discard + this hand.
    Seeded and sorted by name, so the draw pile's (hidden) order never matters."""
    draw, pool = by_name(state["draw"]), by_name(state.get("discard", []) + state["hand"])
    rng, energy, out, k = random.Random(0), state.get("max_energy") or 3, [], next_draw(state)
    for _ in range(EXPOSURE_SAMPLES):
        hand = rng.sample(draw, k) if len(draw) >= k else draw + rng.sample(pool, min(k - len(draw), len(pool)))
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
ENEMY_POWERS = REVIVE_POWERS | {"BackAttackLeftPower", "BackAttackRightPower", "PlowPower", "BurrowedPower", "RavenousPower", "CrabRagePower", "ImbalancedPower",
                                 "SwipePower", "HeistPower", "EscapeArtistPower", "ThieveryPower", "GalvanicPower",
                                 "VitalSparkPower", "StrengthPower", "VigorPower", "TerritorialPower", "VulnerablePower", "WeakPower", "SkittishPower",
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
         "DuplicationPower", "OneTwoPunchPower", "BurstPower")
PLAYER_POWERS |= set(BUFFS)
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


def potion_actions(state):
    """Held potions whose effects the solver can model, shaped like cards (type "Potion", cost 0)."""
    out = []
    for p in state.get("potions", []):
        d = GAMEDATA["potions"].get(p["name"])
        if not d:
            continue
        v = {k: int(n) for k, n in d["vars"].items()}
        pm = "draw" in state and any(pile_moves({"name": p["name"], "type": "Potion", "vars": v}))
        rest = set(v) - PILE_VARS if pm else set(v)
        made = potion_cards(p["name"], state)
        if made:
            rest -= {"Cards"}
        if not (rest & EFFECTS or pm or made or p["name"] == "Duplicator") or not rest <= MODELED:  # Duplicator: no vars
            continue
        if d["target"] != "Self" and "StrengthPower" in v:
            continue  # enemy Strength loss isn't modeled
        out.append({"name": p["name"], "type": "Potion", "target": d["target"], "cost": 0,
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


def solve(state, top=3, kept=()):
    """Best plans for this turn. kept: cards already chosen to go on top of the draw pile (pick screens)."""
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
    dead = sorted((c for c in state["hand"] if c not in playable),
                  key=lambda c: -(end_dmg(c) + end_loss(c) + HAND * (c["name"] == "Regret")))
    normality = any(c["name"] == "Normality" for c in state["hand"])
    enthralled = sum(c["name"] == "Enthralled" for c in state["hand"])
    smoggy = amount(me["powers"], "SmoggyPower")  # ponytail: Skills played before this replan aren't counted
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
    for g, (c, _) in enumerate(list(groups)):
        made = creates(c)
        if made:
            makes[g] = [(add(made[0], 0), made[1])]
        for x, n in c.get("makes") or ():  # card-making potions (stand-in cards, see CARD_POTIONS)
            makes[g] = makes.get(g, []) + [(add(x, 0), n)]
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
    moves = [pile_moves(c) if pile_known else (0, None) for c, _ in groups]
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

    start = (
        state["energy"],
        tuple(n for _, n in groups),
        tuple((f["creature"]["hp"], f["creature"]["block"], pw(f, "VulnerablePower"), pw(f, "WeakPower"),
               pw(f, "SkittishPower") + pw(f, "CurlUpPower"), pw(f, "HardenedShellPower") or -1, pw(f, "SlipperyPower"),
               pw(f, "IntangiblePower"), 0, pw(f, "ArtifactPower"), pw(f, "PoisonPower"), pw(f, "DoomPower"), 0)
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
        tuple(sorted((p["name"], p["amount"]) for p in me["powers"] if p["name"] in BUFFS and p["amount"])),
    )
    focus = amount(me["powers"], "FocusPower")
    echo = amount(me["powers"], "EchoFormPower")  # first N cards each turn are played twice (already in play only)
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

    def enemy_hits(en, skills=0, taint=0, facing=None):
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
                       or (pw(f, "RavenousPower") and ally_died))
            for it in f["intents"] if e[0] > 0 and not stunned else []:
                base = intent_damage(f, it, state.get("ascension", 0))
                if base is not None:
                    per = math.floor((base + s + vigor + taint) * (0.75 if e[3] > 0 else 1) * (1.5 if vuln else 1)
                                     * (BACK_ATTACK if behind(f, facing) else 1))
                    total += [per] * max(it["hits"], 1)
                    vigor = 0
            out.append(total)
        return out

    def incoming_each(en, skills=0, taint=0, facing=None):
        return [sum(h) for h in enemy_hits(en, skills, taint, facing)]

    def take_hits(hits, block, osty, buffs, pre_dmg, pre_loss):
        """(HP lost, Osty soaked, Block left) in the game's order: our turn-end damage (blockable) and HP loss, then
        each enemy hit. Block soaks each hit; Osty takes unblocked attack damage; Intangible caps each at 1; Buffer
        cancels the next HP loss."""
        buffer, intangible = bget(buffs, "BufferPower"), bget(buffs, "IntangiblePower") > 0
        lost = soaked = 0
        for dmg, attack, blockable in [(pre_dmg, False, True), (pre_loss, False, False)] + [
                (h, True, True) for hs in hits for h in hs]:
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
            lost += through
        return lost, soaked, block

    def incoming(en, skills=0, taint=0, facing=None):
        return sum(incoming_each(en, skills, taint, facing))

    # Infested Prism's Vital Spark N: our Skills are Tainted; each one played gives us N Tainted (+N damage per
    # enemy attack hit this turn). ponytail: still counted if the plan kills the Prism before the Skill.
    # Surrounded (Crusher + Rocket): we face one side; enemies on the other side (Back Attack Left/Right) hit 1.5x.
    # A targeted card or potion aimed at an enemy behind us turns us to face it.
    # ponytail: the auto-turn when every enemy left is on one side isn't modeled.
    surround = next((x for x in me["powers"] if x["name"] == "SurroundedPower"), None)
    face0 = ("Left" if surround.get("facing") == 1 else "Right") if surround else None
    side = {f.get("combat_id"): "Left" if pw(f, "BackAttackLeftPower") else "Right" if pw(f, "BackAttackRightPower")
            else None for f in foes}

    def facing_after(steps):
        return next((side[s[4]] for s in reversed(steps) if s[4] is not None and side.get(s[4])), face0)

    spark = max((pw(f, "VitalSparkPower") for f in foes), default=0)
    tainted_now = amount(me["powers"], "TaintedPower")
    galvanic = max((pw(f, "GalvanicPower") for f in foes), default=0)  # Globe Head: Galvanized Powers hurt us on play
    revives = [any(pw(f, p) > 0 for p in REVIVE_POWERS) for f in foes]
    erupt = [pw(f, "SteamEruptionPower") for f in foes]
    loot = [CARD_LOOT_HP * (pw(f, "SwipePower") > 0) + GOLD_LOOT_HP * pw(f, "HeistPower") for f in foes]
    # Bowlbug Rock (Imbalanced): fully blocking its attack knocks it off balance, so it skips its next attack.
    off_balance = [pw(f, "ImbalancedPower") > 0 and any(intent_damage(f, it, state.get("ascension", 0))
                                                        for it in f["intents"]) for f in foes]
    tears = R.var("PaelsTears", "Energy") if "PaelsTears" in R else 0  # unspent energy -> +2 energy next turn

    # The Insatiable's Sandpit: a countdown that kills you at 0; each Frantic Escape played adds 1.
    sandpit = max((pw(f, "SandpitPower") for f in foes), default=0)
    escape_groups = [g for g, (c, _) in enumerate(groups) if c["name"] == ESCAPE]

    # Ironclad's Burning Blood / Black Blood heal after a won fight; only the part that would overflow max HP
    # makes damage free (at 80/80 a 3 hit costs nothing; at 70/80 it does), and only what the rest of the fight
    # won't take anyway (set below, once the expected hits are known). Everyone else pays full price.
    after_heal = sum(GAMEDATA["relics"].get(r["name"], {}).get("vars", {}).get("Heal", 0)
                     for r in state.get("relics", []) if r["name"] in POST_FIGHT_HEAL)
    free_hp = max(0, min(after_heal, me["hp"] + after_heal - me["max_hp"]))
    hp_w = W_HP * HP_WEIGHT.get(fight_type(state), 1)  # HP lost now / to the enemies left; future-turn credits
    # (draws, retained cards, Dexterity, Plating, kept exhaust cards) use fut_w: expected, not certain, so never
    # above W_HP, but no dearer than this fight's HP (a boss's Impervious saved for later saves cheap HP)
    fut_w = min(hp_w, W_HP)
    turns_left = sum(f["creature"]["hp"] for f in foes) / deck_dpt(state)
    # A played card comes back only after the draw pile runs out (reshuffle).
    left = turns_left - 1 - (len(state["draw"]) / HAND if pile_known else 0)
    redraws = min(REDRAW_CAP, max(0.0, left) * HAND / max(len(state.get("deck") or state["hand"]), 1))
    pack = state.get("deck") or state["hand"]
    atk_hits = HAND * sum(max(c["vars"].get("Repeat", 1), 1) for c in pack
                          if c["type"] == "Attack" and "Damage" in c["vars"]) / max(len(pack), 1)
    blk_cards = HAND * sum("Block" in c["vars"] for c in pack) / max(len(pack), 1)
    buff_turns = min(max(turns_left - 1, 0.0), BUFF_TURNS)

    def lasting(steps):
        """Score of the Strength/Dexterity this plan leaves for later turns."""
        out = 0
        for s in steps:
            c = s[3]
            if c["type"] == "Potion":
                continue  # ponytail: potion Strength isn't valued past this turn
            for key, per in (("StrengthPower", atk_hits * value_per_damage), ("DexterityPower", blk_cards * fut_w * 0.5)):
                sign, when = (1, "now") if c.get("ops") else timing(c["name"], key)
                n = sign * c["vars"].get(key, 0)
                if when == "now" and (c["type"] == "Power" or c["target"] not in ("AnyAlly", "AllAllies")):
                    out += n * per * buff_turns
                elif when == "turn":  # +n at the start of each later turn: n, 2n, 3n...
                    out += n * per * buff_turns * (buff_turns + 1) / 2
            plating = card_buffs(c)[0].get("PlatingPower", 0)  # Plating keeps giving Block: n-1, n-2... later turns
            out += fut_w * 0.5 * sum(max(0, plating - k) for k in range(1, round(buff_turns) + 1))
        return FUTURE_DISCOUNT * out

    def worth(c):  # rough value of one play of a card: its damage + half its Block, in score
        v = c["vars"]
        return value_per_damage * v.get("Damage", 0) * max(v.get("Repeat", 1), 1) + fut_w * 0.5 * v.get("Block", 0)

    def exhausts(c):
        return "Exhaust" in keywords(c) or (c["type"] == "Skill" and "CorruptionPower" in mine)
    exhausted_before = exhausted_this_turn(state)
    forecast = [predict(f, state.get("ascension", 0), FUTURE_TURNS) for f in foes]
    future = [sum(t["dmg"] * FUTURE_DISCOUNT ** k for k, t in enumerate(fc)) for fc in forecast]
    next_turn = [fc[0]["dmg"] if fc else 0 for fc in forecast]
    # Threat looks further ahead than the HP math (up to the fight's expected length): an enemy that scales
    # (Ritual, Enlarge, Territorial...) keeps hitting harder, so its damage now is worth more.
    t_turns = min(THREAT_TURNS_MAX, max(FUTURE_TURNS, math.ceil(turns_left)))
    scaled = [sum(t["dmg"] * THREAT_DISCOUNT ** k for k, t in enumerate(fc))
              for fc in (predict(f, state.get("ascension", 0), t_turns) for f in foes)]
    threat = [n + fu + lo + GOLD_LOOT_HP * pw(f, "ThieveryPower")
              for n, fu, lo, f in zip(incoming_each(start[2]), scaled, loot, foes)]
    # Focus fire (Smith's rule): damage counts by threat per HP left, so the scaler / nearly dead enemy is
    # finished first instead of chipping everyone. Damage into Block (Skittish) still counts 0.
    ratio = [t / max(f["creature"]["hp"] + f["creature"]["block"], 1) for t, f in zip(threat, foes)]
    mean_threat = sum(ratio) / max(len(ratio), 1)
    weight = [0 if f["creature"]["hp"] >= UNKILLABLE else min(THREAT_MAX, max(THREAT_MIN, t / mean_threat))
              if mean_threat else 1 for t, f in zip(ratio, foes)]
    # Potions get cheaper when THIS turn's hits would leave us low. A big hit predicted for next turn
    # is a reason to keep the potion for that turn, not to spend it now.
    pot_cost = potion_cost(state, me["hp"] + me["block"] - incoming(start[2])) * HP_WEIGHT.get(fight_type(state), 1)
    # next hand's Block: sampled from the draw pile, else (pile unknown) what the deck blocks per turn on average
    blocks = next_blocks(state, start[5]) if pile_known else [deck_dpt(state, "Block", 0)]

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
    pool = [(c["cost"], 0 if junk(c) else worth(c)) for c in pool]

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
        energy, counts, en, block, strength, dex, plays, pots, hp, osty, orbs, stars, buffs = st
        potion = card["type"] == "Potion"
        attack = card["type"] == "Attack"
        cost = energy if card["x_cost"] else cost_of(card, plays)
        times = energy + x_bonus(R) if card["x_cost"] else 1
        v = {} if junk(card) else card["vars"]
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
        hp -= v.get("HpLoss", 0) + (galvanic if card.get("affliction") == "Galvanized" else 0)  # on play
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
                slow = 1 + 0.1 * (before + plays.cards) if slowed[i] and not potion else 1  # Bygone Effigy
                mult = 1 if potion else halved[i] * shrink
                per = min(math.floor(base * (1.5 if en[i][2] > 0 and attack else 1) * slow * mult), hard_cap[i])
                n_hits = max(v.get("Repeat", 1) if card["name"] != "Spite" or hp < me["hp"] else 1, 1) * times
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
                if attack and bget(buffs, "EnvenomPower"):  # Envenom: Poison per unblocked hit; Artifact blocks each
                    for _ in range(sum(1 for h in hits if h)):
                        if en[i][9] > 0:
                            en[i][9] -= 1
                        else:
                            en[i][10] += bget(buffs, "EnvenomPower")
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
            gain(gained * (1 if potion else block_mult(R, plays)))
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
                for slot, key in ((2, "VulnerablePower"), (3, "WeakPower"), (10, "PoisonPower"), (11, "DoomPower")):
                    if v.get(key):
                        if en[i][9] > 0:  # Artifact blocks one debuff per stack
                            en[i][9] -= 1
                        elif slot == 10:  # Bouncing Flask: Repeat bounces, split over the enemies it can hit
                            en[i][slot] += v[key] * max(v.get("Repeat", 1), 1) * times // max(len(targets), 1)                                 if card["target"] == "RandomEnemy" else v[key] * times
                        else:
                            en[i][slot] += v[key]
            if v.get("StrengthPerVulnerable") and targets:  # Dominate: after its own Vulnerable lands
                strength += v["StrengthPerVulnerable"] * en[targets[0]][2]
        energy -= cost
        for mg, n in makes.get(g, ()):  # made/fetched cards land in hand, playable later this turn
            counts = counts[:mg] + (counts[mg] + n * times,) + counts[mg + 1:]
        if potion:
            pots += 1
        else:
            t = card["type"]
            # ops cards count their exhausts in toss() below; others by text (True Grit, Burning Pact...)
            exhausting = exhausts(card) or (not card.get("ops") and exhausts_others(card))
            plays = plays._replace(attacks=plays.attacks + (t == "Attack"), skills=plays.skills + (t == "Skill"),
                                   powers=plays.powers + (t == "Power"), cards=plays.cards + 1,
                                   block=plays.block + ("Block" in v), exhausted=plays.exhausted + exhausting)
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
        for name, n in card_buffs(card)[0].items():  # powers this card gives us (Stone Armor -> Plating...)
            buffs = badd(buffs, name, n)
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
            c, g = min(hand, key=lambda x: toss_key(x[0], not exhaust, lambda c: worth_next(c) if x[1] is not None
                                                    else -(end_dmg(c) + end_loss(c))))
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
        block += bget(buffs, "FeelNoPainPower") * out  # ponytail: Juggernaut isn't triggered by this Block
        plays = plays._replace(tossed=tossed, gone=tuple(gone), lost=lost, exhausted=plays.exhausted + out)
        st = (energy, counts, en, block, strength, dex, plays, pots, hp, osty, orbs, stars, buffs)
        for c in sly:
            alive = [i for i, e in enumerate(en) if e[0] > 0]
            st = play({**c, "cost": 0, "x_cost": False}, alive[0] if alive and c["target"] == "AnyEnemy" else None, st)
        return st

    def evaluate(st, steps):
        energy, counts, en, block, _, _, plays, pots, hp, osty, orbs, _, buffs = st
        gone = plays.gone or (0,) * len(groups)  # cards a pick took from hand: not played
        rest = dead[plays.tossed:]  # unplayable cards still in hand at turn end
        left = len(state["hand"]) - plays.cards - plays.tossed - sum(gone)
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
        hits = [] if win else enemy_hits(en, plays.skills, tainted_now + spark * taints, facing)
        attacks = sum(map(sum, hits))
        unblockable = (sum(c["name"] == "Regret" for c in rest) * left + sum(map(end_loss, rest))
                       + sum(n * end_loss(c) for (c, _), n in zip(groups, counts)))
        lost, soaked, block_left = (0, 0, block) if win else take_hits(hits, block, osty, buffs, burn + held, unblockable)
        hp_after = hp - lost
        back = bget(buffs, "ThornsPower") + bget(buffs, "FlameBarrierPower")  # hit back per enemy attack hit
        if back and hits:
            for e, hs in zip(en, hits):
                if e[0] > 0 and hs:
                    hit(e, back * len(hs))  # ponytail: a kill here doesn't cancel that enemy's later hits
            kills = sum(1 for a, b in zip(start_en, en) if a[0] > 0 and b[0] == 0)
        carried = block_left if bget(buffs, "BarricadePower") or bget(buffs, "BlurPower") else 0  # kept next turn
        escapes = sum(start[1][g] - counts[g] - gone[g] for g in escape_groups)
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
        played = [(c, start[1][g] - counts[g] - gone[g]) for g, (c, _) in enumerate(groups) if c["type"] != "Potion"]
        exhaust_cost = sum(n * (-EXHAUST_JUNK if junk(c) else EXHAUST_WEIGHT * redraws * worth(c))
                           for c, n in played if n > 0 and exhausts(c) and (junk(c) or c["name"] in deck_names))
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
        drawn = sum(k * m[0] for c, k, m in used)
        retained += drawn * sum(w for cost, w in pool if 0 <= cost <= energy) / len(pool) if drawn and pool else 0
        blocked_all = block >= attacks
        killed = [a[0] > 0 and b[0] == 0 for a, b in zip(start_en, en)]
        hit_next = (sum(n * (BACK_ATTACK if behind(f, facing) else 1)
                        for n, a, ob, f in zip(next_turn, alive, off_balance, foes) if a and not (ob and blocked_all))
                    + sum(x for x, k in zip(erupt, killed) if k))
        future_left = (sum(fu for fu, a in zip(future, alive) if a) + sum(x for x, k in zip(erupt, killed) if k)
                       - hit_next + exposure(hit_next, carried))
        future_left -= sum(n for n, a, ob in zip(next_turn, alive, off_balance) if a and ob and blocked_all)
        pool_e = sum(max(c, 1) for c, w in pool if c >= 0)
        retained += (hp_w * sum(x for x, k in zip(loot, killed) if k)
                     + (tears * sum(w for c, w in pool if c >= 0) / pool_e if tears and energy >= 1 and pool_e else 0))
        projected = min(hp_after, me["hp"]) - exposure(hit_next, top_block + carried)
        unknown = sum(1 for s in steps if not s[2])
        score = (W_WIN * win + W_KILL * kills + value_per_damage * weighted
                 - hp_w * max(0, net_loss - free_hp) * danger(projected, me["max_hp"])
                 - hp_w * FUTURE_WEIGHT * future_left * (not win)
                 + UNKNOWN_VALUE * unknown - W_DEATH * (hp_after <= 0) - pot_cost * pots
                 + (SANDPIT_VALUE * escapes / max(sand_left, 1) if sandpit else 0)
                 - DAZED_COST * sum(e[8] for e in en)
                 + value_per_damage * POISON_FUTURE * poison_future - hp_w * OSTY_HP * soaked
                 - ETHEREAL_LOSS * ethereal - exhaust_cost - plays.lost + retained + (0 if win else lasting(steps))
                 + sum(ORB_HELD.get(n, 0) + (value_per_damage * FUTURE_WEIGHT * ((v or 6) + ORB_PASSIVE["Dark"] + focus)
                                             if n == "Dark" else 0) for n, v in orbs))
        return Plan(list(steps), damage, block - start[3], net_loss, kills, win, score, energy)

    def allowed(card, plays, steps):
        """Play limits: Sloth cap, Smog (after a Smog card or with Smoggy: no more Skills this turn), Ringing (only as
        the first card of the turn),
        Bound (one per turn; ponytail: earlier Bound plays this turn aren't read)."""
        aff = card.get("affliction")
        if normality and before + plays.cards >= 3:  # Normality in hand: max 3 cards this turn
            return False
        if card["name"] != "Enthralled" and enthralled > sum(s[3]["name"] == "Enthralled" for s in steps):
            return False  # Enthralled in hand must be played before anything else
        if card["type"] == "Skill" and (any(s[3].get("affliction") == "Smog" for s in steps)
                                        or (smoggy and plays.skills >= 1)):
            return False
        return (plays.cards < cap and not (aff == "Ringing" and before + plays.cards)
                and not (aff == "Bound" and any(s[3].get("affliction") == "Bound" for s in steps)))

    plans, seen = [], set()

    def dfs(st, steps):
        key = (st, facing_after(steps)) if surround else st  # same cards, other facing = a different outcome
        if key in seen:
            return
        seen.add(key)
        plans.append(evaluate(st, steps))
        energy, counts, en = st[0], st[1], st[2]
        for g in order:
            card = groups[g][0]
            if counts[g] == 0 or (cost_of(card, st[6]) > energy and not card["x_cost"]) or (card["x_cost"] and energy == 0):
                continue
            if card["type"] != "Potion" and star_cost(card) > st[11]:
                continue
            if card["type"] != "Potion" and not allowed(card, st[6], steps):
                continue
            if (card["type"] != "Potion" and timing(card["name"], "Energy")[1] == "if"
                    and not exhausted_before + st[6].exhausted):
                continue  # Forgotten Ritual (v0.107.1) before any exhaust: it would just burn itself for nothing
            nxt = counts[:g] + (counts[g] - 1,) + counts[g + 1:]
            potion = card["type"] == "Potion"
            pm = any(moves[g])
            vs = (set(card["vars"]) - PILE_VARS if pm else set(card["vars"])) - card_buffs(card)[1]
            modeled = potion or junk(card) or card.get("ops") or ((bool(vs & MODELED) or bool(channels(card)) or pm or bool(card_buffs(card)[0]))
                                               and vs <= MODELED
                                               and not deferred(card))
            label = ("POTION " if potion else "") + card["name"] + ("+" if card["upgraded"] else "")
            targets = [i for i, e in enumerate(en) if e[0] > 0] if card["target"] == "AnyEnemy" else [None]
            for t in targets:
                s2 = play(card, t, (st[0], nxt) + st[2:], g)
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

    # Strength sources are tried first, then Vulnerable: of two orders reaching the same state the first is kept,
    # so a Strength Potion / Bash that wins either way comes before the attacks (more damage if a replan falls short).
    order = sorted(range(len(groups)), key=lambda g: (groups[g][0]["vars"].get("StrengthPower", 0) <= 0,
                                                      groups[g][0]["vars"].get("VulnerablePower", 0) <= 0))
    dfs(start, [])
    # Equal scores: spend the energy (a harmless extra Defend beats ending the turn early; not once the fight is
    # won), then the shorter plan.
    plans.sort(key=lambda p: (-round(p.score, 6), 0 if p.win else p.energy_left, len(p.steps)))
    return plans[:top]
