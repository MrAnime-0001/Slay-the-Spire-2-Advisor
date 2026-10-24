"""Card-reward, rest-site and shop advice: rate cards from their numbers, what the deck lacks, and relics."""
import json
import math
import os
import re
from itertools import combinations

from pathing import (CARD_REWARD, GOLD_LEFT, HEAL_FRACTION, HP_VALUE, RELIC, SHOP_PER_100, UPGRADE, best_routes,
                     fight_costs, should_rest)
from enemies import predict
from relics import COMMUNITY, GAMEDATA, KNOWLEDGE, Relics, dmg_add
from archetypes import ARCHETYPES, commitment, expand_power, snake, synergy
from deck import encounter, hp_lost, known_boss, known_foes, slot_cost
from runplan import WIKI, card_fit, character, plan, relic_fit
from solver import (ENGINES, ORB_PASSIVE, channels, creates, deck_dpt, engine_powers, from_ops, gd_card,
                    SOLVE_SECONDS, keywords, solve, toss_key)
from archetypes import card_text


def pile_fetch(prompt):
    """Gamedata fetch {from, to, top, count, filter} of the card/potion whose pick prompt this is
    ("THINKING_AHEAD.selectionScreenPrompt" -> ThinkingAhead), else None."""
    if not prompt or not prompt.endswith(".selectionScreenPrompt"):
        return None
    name = "".join(w.capitalize() for w in prompt.split(".")[0].split("_"))
    return (GAMEDATA["cards"].get(name) or GAMEDATA["potions"].get(name) or {}).get("fetch")


# A pick screen solves once per candidate card: its whole search shares this budget, so a 7-card hand or a 20-card
# discard pile answers in ~3 s instead of stalling the autopilot.
PICK_SECONDS = 3.0


def on_top_score(state, hand, card, seconds=SOLVE_SECONDS):
    """Best plan score for the rest of this turn with `card` put on top of the draw pile (next hand)."""
    return solve({**state, "hand": hand, "hand_select": None, "card_grid": None}, kept=[card], seconds=seconds)[0].score

# Value per point of each card var. Tune these.
VAR_VALUE = {"Damage": 1, "Block": 0.9, "VulnerablePower": 4, "WeakPower": 3,
             "StrengthPower": 6, "DexterityPower": 5, "Cards": 3, "Energy": 6, "HpLoss": -1.5,
             "PoisonPower": 2.5, "DoomPower": 1, "OstyDamage": 1, "Summon": 0.9}  # engines: same as deck.py
UNKNOWN_VALUE = 3      # flat credit for effects we can't read from numbers (shown as "?")
AOE_NEEDED_BONUS = 4   # card hits all enemies and the deck has no AoE attack
BLOCK_NEEDED_BONUS = 2  # card gives block and under 30% of the deck does
DECK_SIZE_FREE = 20    # no act data: past this, every pick costs a little (dilution)
DECK_SIZE_PENALTY = 0.3
DILUTION_TOL = 0.5     # HP/fight a simmed card must save to be worth a deck slot (below: the tier list can't rescue it)
SKIP_BELOW = 6
EXHAUST_STATS, RETAIN_STATS = 0.5, 1.15  # stats-only scoring (no deck sim): Exhaust = once a fight
SCARCITY_SKIP = 1
# Relic synergies: relic -> (which cards benefit, bonus). Damage relics (Strike Dummy etc.) are applied
# directly through relics.dmg_add instead.
ATTACK_COUNT_RELICS = {"Kunai", "Shuriken", "OrnamentalFan", "PenNib", "Nunchaku", "Kusarigama"}
SYNERGY = [
    (ATTACK_COUNT_RELICS, lambda c: c["type"] == "Attack" and c["cost"] <= 1, 1),
    ({"LetterOpener", "TuningFork"}, lambda c: c["type"] == "Skill", 1),
    ({"Vambrace", "PaelsLegion", "Orichalcum"}, lambda c: "Block" in c["vars"], 1),
    ({"LostWisp", "Permafrost", "GamePiece", "MummifiedHand"}, lambda c: c["type"] == "Power", 2),
]
# Community tier blended 50/50 with the stats score: the stats can't see engines (exhaust, Strength scaling) and
# overrate one-turn buffs like Setup Strike. knowledge.json cards[X]: tier (Mobalytics), stl (slaythetierlist, via
# Spire-Forge), guide (Research Notes); the sources disagree a lot, so a card's tier value is their mean.
TIER_VALUE = {"S": 16, "A": 12, "B": 9, "C": 6, "D": 3}
TIER_BLEND = 0.5
TIER_BLEND_SIM = 0.25  # cards the deck sim scores: the tier list can't see what THIS deck lacks (block vs damage)
# Deck sim (state given): score = SKIP_BELOW + HP_SAVED_SCALE * (HP saved per act fight - DILUTION_TOL), so a card
# that saves nothing (it dilutes as much as it adds) lands under the skip line. Build synergy (0..2) x SYNERGY_VALUE on top.
HP_SAVED_SCALE, SYNERGY_VALUE = 0.5, 3
# Two-card combos (knowledge.pairs, Spire-Forge SYNERGY_PAIRS, bonus 8-16): the partner already in the deck adds
# PAIR_SCALE x bonus, capped. Anti-synergy (knowledge.anti): a card that works against a build the deck is in.
PAIR_SCALE, PAIR_CAP, ANTI_VALUE = 0.12, 2.4, 3
PAIRS = {}
for _a, _b, _bonus in KNOWLEDGE.get("pairs", []):
    PAIRS.setdefault(_a, {})[_b] = PAIRS.setdefault(_b, {})[_a] = _bonus


def tier_value(name):
    """Mean of the card's community tiers (None if it has none)."""
    k = KNOWLEDGE["cards"].get(name, {})
    tiers = [TIER_VALUE[t] for t in (k.get("tier"), k.get("stl"), k.get("guide")) if t in TIER_VALUE]
    return sum(tiers) / len(tiers) if tiers else None


def combo(card, deck, relics=()):
    """Combo bonus minus anti-synergy for this card against the deck."""
    names = {c["name"] for c in deck}
    bonus = min(PAIR_CAP, PAIR_SCALE * sum(b for other, b in PAIRS.get(card["name"], {}).items() if other in names))
    built = commitment(deck, relics) if deck else {}
    return bonus - ANTI_VALUE * sum(built.get(b, 0) for b, cards in KNOWLEDGE.get("anti", {}).items()
                                    if card["name"] in cards)


def is_aoe(card):
    return card["type"] == "Attack" and card["target"] in ("AllEnemies", "RandomEnemy")


# Engine cards (R1, from a won A3 Defect run): a Power's worth is what it adds EVERY turn of the fight, not the
# numbers printed on it. engine_value reads the card's own text/ops: per-turn channels (Spinner, Lightning Rod),
# Focus (x orbs held), orb slots, extra draws (Machine Learning), evoke / per-play triggers (Thunder), and the
# solver's ENGINES table (Noxious Fumes...). Damage-equivalent per turn; x POWER_TURNS turns of benefit.
# L1: what finished runs taught (tools/learn.py -> learned.json): per character, the win-rate lift of decks that
# ended with a card / relic, already shrunk toward 0. A nudge: x LEARN_WEIGHT, capped at LEARN_CAP, and never above
# the deck sim's dilution check. STS2_NO_LEARN=1 turns it off (tests).
LEARN_WEIGHT, LEARN_CAP = 20, 3
try:
    LEARNED = {} if os.environ.get("STS2_NO_LEARN") else json.load(
        open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "learned.json"), encoding="utf-8"))
except (OSError, ValueError):
    LEARNED = {}


def learned(kind, name, state):
    """Score nudge from past runs for this character, or 0."""
    ch = LEARNED.get("chars", {}).get(character(state) or "", {}) if state else {}
    lift = ch.get(kind, {}).get(name, {}).get("lift", 0)
    # + the community's skill-adjusted lift for this character (tools/community.py): ~2M runs vs your ~100
    lift += COMMUNITY.get("chars", {}).get(character(state) or "", {}).get(kind, {}).get(name, 0) if state else 0
    return max(-LEARN_CAP, min(LEARN_CAP, LEARN_WEIGHT * lift))


POWER_TURNS = 4  # an average fight runs ~5 turns; a Power played early works for the rest
POWER_TURNS_RANGE = (2, 8)
ACT_FIGHTS = {"Monster": 8, "Elite": 2.5, "Boss": 1}  # fights per act by kind (A1 Swarming Elites: more elites)


def power_turns(state):
    """B4: turns a Power works per fight in this act: the mean fight length (Spire Codex turns, per kind over the
    act's pool, known boss in place of the boss pool) weighted by fights per act x HP lost there, minus the turn it's
    played. Long elite/boss fights are where setup pays and where HP is lost. POWER_TURNS without data."""
    act = GAMEDATA.get("acts", {}).get((state or {}).get("act_name")) or {}
    enc = COMMUNITY.get("encounters", {})
    kinds = {k: [e for e in act.get("encounters", []) if GAMEDATA["encounters"].get(e, {}).get("class") == k]
             for k in ("Monster", "Elite")}
    kinds["Boss"] = list(known_boss(state)) or act.get("bosses", [])
    elites, fights = known_foes(state)  # B11: the pre-rolled fights ahead, when the reader has them
    kinds["Elite"], kinds["Monster"] = list(elites) or kinds["Elite"], list(fights) or kinds["Monster"]
    num = den = 0
    for k, names in kinds.items():
        rows = [enc[e] for e in names if (enc.get(e) or {}).get("dmg") and enc[e].get("turns")]
        if rows:
            dmg = sum(r["dmg"] for r in rows) / len(rows)
            num += ACT_FIGHTS[k] * dmg * (sum(r["turns"] for r in rows) / len(rows) - 1)
            den += ACT_FIGHTS[k] * dmg
    if not den:
        return POWER_TURNS
    turns = num / den
    return max(POWER_TURNS_RANGE[0], min(POWER_TURNS_RANGE[1], turns))
ORB_SLOTS = 3


def orb_deck(deck):
    """How many cards channel orbs (0: Focus and slots are worth little)."""
    return sum(1 for c in deck if channels(c) or "Channel" in card_text(c["name"]))


def avg_card(deck):
    """A drawn card's rough output (Damage x hits + 0.9 Block) over the deck."""
    out = [c["vars"].get("Damage", 0) * max(c["vars"].get("Repeat", 1), 1) + 0.9 * c["vars"].get("Block", 0)
           for c in deck if c["type"] in ("Attack", "Skill")]
    return sum(out) / max(len(deck), 1)


def engine_value(card, deck):
    """(damage-equivalent output per turn, recognised) of a card's lasting per-turn effects."""
    v, text = card["vars"], card_text(card["name"])
    orbs = orb_deck(deck) >= 3
    slots = ORB_SLOTS + sum(c["vars"].get("Repeat", 0) for c in deck if c["name"] in ("Capacitor", "Modded"))
    focus = 0  # ponytail: Focus already in the deck isn't added to each orb's value
    per, known = 0.0, False
    for power, n in engine_powers(from_ops(card)):
        kind, unit, ramp = ENGINES[power]
        per += n * unit * (1.5 if kind == "all" else 0.9 if kind == "block" else 1) * ((POWER_TURNS + 1) / 2 if ramp else 1)
        known = True
    m = re.search(r"(?:start of your turn|start of the next[^,]*), Channel (\d+|\{[^}]*\}) (\w+)", text)
    if m:
        orb = m.group(2)
        n = 1 if not m.group(1).isdigit() else int(m.group(1))
        turns = v.get(card["name"] + "Power", POWER_TURNS) if "next" in m.group(0) else POWER_TURNS
        # one more orb a turn: its passive from then on (into full slots it pushes out an orb you already had)
        per += n * (ORB_PASSIVE.get(orb, 2) + focus) * min(turns, POWER_TURNS) / POWER_TURNS
        known = True
    if "FocusPower" in v and "lose" not in text.lower():
        per += v["FocusPower"] * (slots if orbs else 1)  # every orb's passive +1, every turn
        known = True
    if card["name"] in ("Capacitor", "Modded") or "Orb Slot" in text:
        per += v.get("Repeat", 1) * 0.5 * (ORB_PASSIVE["Lightning"] + focus) * orbs
        known = True
    if not known and re.search(r"start of your turn, draw", text) and "Cards" in v:  # (ENGINES may have it)
        per += v["Cards"] * avg_card(deck)
        known = True
    m = re.search(r"Whenever you Evoke (\w+), deal \{(\w+)", text)
    if m:
        per += 0.7 * v.get(m.group(2), 0) * orbs  # ~0.7 evokes of that orb a turn in an orb deck
        known = True
    return per, known


def rate(card, deck, relics=(), state=None, raw=False):
    """(score, modeled, HP saved per fight or None). With `state` (act, ascension, energy), cards the deck sim understands are scored by
    the HP they save in this act's boss/elite fights (deck.py: damage vs block balance, draw odds, dilution);
    everything gets the build-synergy bonus (archetypes.py) and the community tier blend (raw: no tier blend)."""
    R = Relics(relics, GAMEDATA["relics"])
    card = expand_power(card)  # Uppercut's generic "Power" var -> Weak + Vulnerable
    v = dict(card["vars"])
    made = creates(card)  # Blade Dance etc.: value the Shivs it makes, not its count var as card draw
    if made:
        var = GAMEDATA["cards"][card["name"]]["creates"][1]
        v.pop(var, None)
        for k, x in made[0]["vars"].items():
            v[k] = v.get(k, 0) + x * made[1]
    hits = max(v.get("Repeat", 1), 1)
    value = sum(VAR_VALUE[k] * n for k, n in v.items() if k in VAR_VALUE and k not in ("Damage",))
    if card["type"] != "Attack" and "PoisonPower" in v:  # Bouncing Flask: Repeat = bounces, each poisons
        value += VAR_VALUE["PoisonPower"] * v["PoisonPower"] * (hits - 1)
    if "Damage" in v:
        value += (v["Damage"] + dmg_add(card, R)) * hits * (1.5 if is_aoe(card) else 1)
    kw = set(keywords(card))
    if "Exhaust" in kw and card["type"] != "Power":  # one hit/Block per fight; lasting debuffs/buffs keep value
        once = v.get("Damage", 0) * hits * (1.5 if is_aoe(card) else 1) + VAR_VALUE["Block"] * v.get("Block", 0)
        value -= (1 - EXHAUST_STATS) * once
    if "Retain" in kw:
        value *= RETAIN_STATS
    modeled = set(v) <= set(VAR_VALUE) | {"Repeat"} and bool(v)
    engine, known = engine_value(card, deck)
    if known:  # its every-turn output replaces the printed numbers it was read from
        value = (value if card["type"] != "Power" else 0) + engine * power_turns(state)
        modeled = True
    if not modeled:
        value += UNKNOWN_VALUE
    score = value / max(3 if card["x_cost"] else card["cost"], 1)
    if is_aoe(card) and not any(is_aoe(c) for c in deck):
        score += AOE_NEEDED_BONUS
    if "Block" in v and sum("Block" in c["vars"] for c in deck) < 0.3 * len(deck):
        score += BLOCK_NEEDED_BONUS
    score += sum(bonus for names, fits, bonus in SYNERGY if fits(card) and any(n in R for n in names))
    simmed = state is not None and modeled and card["type"] in ("Attack", "Skill")
    saved = None
    if simmed:
        saved = hp_lost(deck, state) - hp_lost(list(deck) + [card], state)
        score = SKIP_BELOW + HP_SAVED_SCALE * (saved - DILUTION_TOL) + sum(
            bonus for names, fits, bonus in SYNERGY if fits(card) and any(n in R for n in names))
    elif state is None:
        score -= DECK_SIZE_PENALTY * max(0, len(deck) - DECK_SIZE_FREE)
    elif card["type"] != "Power":  # Powers leave the piles once played; anything else is drawn every cycle
        score -= HP_SAVED_SCALE * slot_cost(deck, state)
    fit = None
    if state is not None:  # run plan (runplan.py): build fit, gaps, wiki synergies, off-build penalty
        fit = card_fit(card, plan({**state, "deck": list(deck), "relics": list(relics)}), deck)[0]
        score += combo(card, deck, relics)
    else:
        score += SYNERGY_VALUE * synergy(card, deck, relics) + combo(card, deck, relics)
    sim = score
    tier = None if raw else tier_value(card["name"])
    if tier is not None:
        blend = TIER_BLEND_SIM if simmed else TIER_BLEND
        score = (1 - blend) * score + blend * tier
        modeled = True  # the tier covers what the numbers miss
    score += learned("cards", card["name"], state)
    if simmed and saved < DILUTION_TOL:
        score = min(score, sim)  # the deck fights worse with it: the tier list can't overrule the draw odds
    if fit is not None:  # after the tier blend: the plan's off-build penalty isn't diluted by a card's tier
        score += fit
    if "Forge" in v:
        score -= FORGE_OVER * max(0, sum("Forge" in c["vars"] for c in deck) + 1 - FORGE_SHARE * (len(deck) + 1))
    return round(score, 1), modeled, None if saved is None else round(saved, 1)


LATE_SKIP, LATE_DECK = 3, 25
# B4: Forge cards (Regent) feed one Sovereign Blade: past ~15% of the deck (2-3 in 20 cards) each more one adds less
# than the energy it takes to play the Blade (research: Forge density cap). FORGE_OVER per card over the share.
FORGE_SHARE, FORGE_OVER = 0.15, 2
COMMIT_SKIP = 2  # a committed deck needs a better card to take one (more Skips): + COMMIT_SKIP x commitment


def pick(cards, deck, relics=(), state=None):
    """(best name or "Skip", [(name, score, modeled, HP saved, reason)])."""
    p = plan({**state, "deck": list(deck), "relics": list(relics)}) if state is not None else None
    rated = [(c["name"], *rate(c, deck, relics, state), card_fit(c, p, deck)[1] if p else "") for c in cards]
    hope = upgrade_hope(cards, deck, relics, state) if state is not None else {}
    rated = [(r[0], r[1] + hope[r[0]], *r[2:-1], "; ".join(x for x in (r[-1], f"smith ahead +{hope[r[0]]:.0f}") if x))
             if r[0] in hope else r for r in rated]
    best = max(rated, key=lambda r: r[1], default=None)
    # A7 Scarcity halves rare odds: good cards come less often, so the bar to take one drops a little
    bar = SKIP_BELOW - (SCARCITY_SKIP if (state or {}).get("ascension", 0) >= 7 else 0)
    bar += COMMIT_SKIP * p["level"] if p and p["committed"] else 0
    # Act 3 with a working deck (committed, or already big): another card mostly dilutes it (the won A3 run skipped
    # 4 of 6 late rewards the advisor wanted to take)
    bar += LATE_SKIP if p and p["act"] >= 2 and (p["committed"] or len(deck) >= LATE_DECK) else 0
    return (best[0] if best and best[1] >= bar else "Skip"), rated


# B11: a reward card counts what its upgrade adds when a smith this act would pick it. Smiths ahead = rest sites on
# the best route (minus the next one when HP says heal there); the card earns the margin over the upgrade those
# smiths would make anyway (the n-th best in the deck now), damped by UPGRADE_HOPE (routes and HP change).
# ponytail: later acts' smiths, Armaments-style in-combat upgrades and Whetstone-like relics aren't counted.
UPGRADE_HOPE = 0.6


def upgrade_hope(cards, deck, relics, state):
    """{offered card name: score bonus for being upgraded at a smith ahead this act}."""
    route = (best_routes(state, top=1) or [None])[0] if state.get("map") else None
    if route is None:
        return {}
    p = state["player"]
    smiths = (route.rolled or route.types).count("RestSite") - bool(should_rest(p["hp"], p["max_hp"], route.types,
                                                                              fight_costs(state)))
    offered = [c for c in cards if not c["upgraded"] and c["name"] in UPGRADES]
    if smiths <= 0 or not offered:
        return {}
    have = sorted((upgrade_gain(c, deck, UPGRADES, relics, state) for c in {c["name"]: c for c in deck
                   if not c["upgraded"] and c["name"] in UPGRADES}.values()), reverse=True)
    bar = have[smiths - 1] if len(have) >= smiths else 0  # the upgrade the last smith would make without the card
    out = {}
    for c in offered:
        g = upgrade_gain(c, list(deck) + [c], UPGRADES, relics, state)
        if g > bar:
            out[c["name"]] = round(UPGRADE_HOPE * (g - bar), 1)
    return out


# --- rest site ---
# Rest-or-smith thresholds live in pathing.should_rest, so the route search and this screen agree.
KEYWORD_VALUE = {"-Exhaust": 3, "-Ethereal": 2, "+Innate": 1, "+Retain": 1}
UPGRADES = GAMEDATA["upgrades"]


def upgraded(card, up):
    v = dict(card["vars"])
    for k, d in up.get("vars", {}).items():
        v[k] = v.get(k, 0) + d
    return {**card, "vars": v, "cost": max(0, card["cost"] + up.get("cost", 0)), "upgraded": True}


# Upgrade value (v19 Phase A): the score change without the tier blend (a tiered card's gain was damped, a basic's
# wasn't), plus what the deck sim can't see: bigger Vulnerable/Weak/Strength/Poison amounts (it treats them as on/off)
# and bigger engine Powers (Noxious Fumes 2 -> 3). Basics count BASIC_UP: they're the first cards you remove.
FLAG_VARS = ("VulnerablePower", "WeakPower", "StrengthPower", "DexterityPower", "PoisonPower")
POWER_UP_VALUE, COST_UP_VALUE, BASIC_UP = 4, 2, 0.3


def upgrade_gain(card, deck, upgrades=UPGRADES, relics=(), state=None):
    up = upgrades[card["name"]]
    rest = list(deck)
    if state is not None and card in rest:
        rest.remove(card)  # compare deck+card vs deck+upgraded card
    better = upgraded(card, up)
    gain = rate(better, rest, relics, state, raw=True)[0] - rate(card, rest, relics, state, raw=True)[0]
    gain += sum(KEYWORD_VALUE.get(k, 1) for k in up.get("keywords", []))
    delta = up.get("vars", {})
    if state is not None and card["type"] in ("Attack", "Skill"):  # simmed: amounts beyond on/off
        gain += sum(VAR_VALUE[k] * delta[k] for k in FLAG_VARS if k in delta)
    if card["type"] == "Power" and not engine_value(card, rest)[1]:  # known engines: rate() already has the change
        gain += POWER_UP_VALUE * sum(1 for k, d in delta.items() if d > 0 and k not in VAR_VALUE)
    gain += COST_UP_VALUE * max(0, -up.get("cost", 0))
    if (card.get("rarity") or GAMEDATA["cards"].get(card["name"], {}).get("rarity")) == "Basic":
        gain *= BASIC_UP
    return round(gain, 1)


def rest_choice(state, upgrades=UPGRADES):
    """("Smith", card name) or ("Heal", None): what autopilot clicks at a rest site."""
    head, _ = rest_advice(state, upgrades)
    return ("Smith", head.split("upgrade ", 1)[1]) if head.startswith("Smith") else ("Heal", None)


def rest_advice(state, upgrades=UPGRADES):
    """Return (headline, detail lines) for the rest-site screen."""
    p, deck, opts = state["player"], state["deck"], state["rest_site"]["options"]
    relics = state.get("relics", [])
    heal = min(int(p["max_hp"] * HEAL_FRACTION), p["max_hp"] - p["hp"])
    seen, gains = set(), []
    for c in deck:
        if not c["upgraded"] and c["name"] in upgrades and c["name"] not in seen:
            seen.add(c["name"])
            gains.append((c["name"], upgrade_gain(c, deck, upgrades, relics, state)))
    gains.sort(key=lambda g: -g[1])
    others = [o for o in opts if o not in ("Heal", "Smith")]
    tail = [f"also available: {', '.join(others)} (not rated)"] if others else []
    ahead = (best_routes(state, top=1) or [None])[0]  # rooms on the best route after this camp
    reason = should_rest(p["hp"], p["max_hp"], ahead.types if ahead else [], fight_costs(state))
    if "Smith" in opts and gains and not reason:
        tag = lambda n: "  (basic: little gain)" if GAMEDATA["cards"].get(n, {}).get("rarity") == "Basic" else ""
        return f"Smith -> upgrade {gains[0][0]}", [f"{n:<16} +{g}{tag(n)}" for n, g in gains[:3]] + tail
    return f"Rest  (heal +{heal})", [f"HP {p['hp']}/{p['max_hp']}" + (f"  ({reason})" if reason else "")] + tail


# --- in-combat hand picks (Armaments, True Grit, discard effects...) ---
BAD_PICKS = {"TO_EXHAUST", "TO_DISCARD", "TO_REMOVE", "TO_TRANSFORM"}


# The game hides hand cards its pick filter rejects and numbers only the shown ones (NPlayerHand.ActiveHolders), so
# number keys must index the shown cards. Filters from the decompiled CardSelectCmd.FromHand callers; upgrade picks
# show IsUpgradable cards.
HAND_FILTERS = {
    "SNAP.selectionScreenPrompt": lambda c: "Retain" not in keywords(c),
    "DUAL_WIELD.selectionScreenPrompt": lambda c: c["type"] in ("Attack", "Power"),
    "HAND_TRICK.selectionScreenPrompt": lambda c: c["type"] == "Skill" and "Sly" not in keywords(c),
    "HEIRLOOM_HAMMER.selectionScreenPrompt": lambda c: GAMEDATA["cards"].get(c["name"], {}).get("pool") == "Colorless",
}


def hand_pick(state, upgrades=UPGRADES):
    """Cards to pick when a card asks. Returns (number keys, highest first, since picked cards leave the
    row; names). Upgrade: biggest gain. Exhaust/discard: Curse/Status, then weakest. Else: strongest."""
    sel, hand, deck, relics = state["hand_select"], state["hand"], state.get("deck", []), state.get("relics", [])
    upgrade = sel["mode"] == "upgrade"
    shown = [c for c in hand if not c["upgraded"] and c["type"] not in ("Status", "Curse")] if upgrade else         [c for c in hand if HAND_FILTERS.get(sel["prompt"], lambda c: True)(c)]
    at = [k for k, c in enumerate(shown) if not upgrade or c["name"] in upgrades]  # pool card i = shown card at[i]
    pool = [shown[k] for k in at]
    junk = lambda c: c["type"] in ("Curse", "Status")
    if upgrade:
        order = sorted(range(len(pool)), key=lambda i: -upgrade_gain(pool[i], deck, upgrades, relics, state))
        n = sel["max"]
    elif (f := pile_fetch(sel["prompt"])) and f["to"] == "Draw":
        # hand -> top of draw (Thinking Ahead, Glimmer, Photon Cut): keep the card worth more next turn than now
        # (the Defend on a quiet turn before a big hit; the big attack this turn can't afford).
        order = sorted(range(len(pool)), key=lambda i: -on_top_score(state, pool[:i] + pool[i + 1:], pool[i],
                                                                        PICK_SECONDS / len(pool)))
        n = sel["max"]
    elif sel["prompt"] in BAD_PICKS:
        # same order the solver plans with (solver.toss_key): junk, then Sly (discarded = played free), then weakest
        sly = lambda c: sel["prompt"] == "TO_DISCARD" and "Sly" in keywords(c)
        order = sorted(range(len(pool)), key=lambda i: toss_key(pool[i], sel["prompt"] == "TO_DISCARD",
                                                                lambda c: rate(c, deck, relics)[0]))
        n = max(sel["min"], min(sel["max"], sum(junk(c) or sly(c) for c in pool)))  # optional picks: junk + Sly
    else:
        order = sorted(range(len(pool)), key=lambda i: -rate(pool[i], deck, relics)[0])
        n = max(sel["min"], min(sel["max"], 1))
    chosen = sorted(order[:max(0, n - sel["selected"])], key=lambda i: -at[i])  # keys count the shown cards
    return [at[i] + 1 for i in chosen], [pool[i]["name"] for i in chosen]


def pile_pick(state):
    """Card names to pick on an in-combat pile screen (card_grid: Headbutt, Hologram, Dredge, Secret Weapon...).
    To hand: whichever makes the best plan now (at its printed cost). To top of draw: best next turn.
    Anything else (exhaust/transform from a pile): junk first, then the weakest."""
    grid = state["card_grid"]
    cards, f = grid["cards"], pile_fetch(grid.get("prompt"))
    rest = {**state, "hand_select": None, "card_grid": None}
    if f and f["to"] == "Hand":
        key = lambda c: -solve({**rest, "hand": state["hand"] + [c]}, seconds=PICK_SECONDS / len(cards))[0].score
    elif f and f["to"] == "Draw":
        key = lambda c: -on_top_score(state, state["hand"], c, PICK_SECONDS / len(cards))
    else:
        key = lambda c: toss_key(c, False, lambda x: rate(x, state.get("deck", []), state.get("relics", []))[0])
    n = max(grid["min"], min(grid["max"], len(cards)))
    return [c["name"] for c in sorted(cards, key=key)[:n]]


# --- "choose a card" screen (Knowledge Demon's curse, Attack/Skill/Power potions, Discovery, some relics) ---
# Knowledge Demon: each pick is a permanent power for the fight. Cost in HP over the rest of the fight:
# Disintegration = its damage x turns left; the others cut our output by a fraction f, which stretches
# the fight by turns_left * f / (1 - f) extra turns of the demon's hits.
DEMON_CURSES = {"Disintegration", "MindRot", "Sloth", "WasteAway"}
HAND_SIZE, DEMON_HIT = 5, 15  # cards drawn per turn; fallback demon damage per turn
# ponytail: one fight's worth of turns, ignores later curse cycles stacking; block lost to a weaker
# deck isn't counted separately (it's in the same output fraction).


def demon_choice(state, cards):
    """[(name, HP cost)] cheapest first."""
    p, foes = state["player"], state.get("enemies", [])
    turns = sum(e["creature"]["hp"] for e in foes) / deck_dpt(state)
    fc = [t["dmg"] for e in foes for t in predict(e, state.get("ascension", 0), 4)]
    hit = sum(fc) / len(fc) if fc else DEMON_HIT
    energy = state.get("max_energy", 3)
    deck = [c for c in state.get("deck", []) if c["type"] in ("Attack", "Skill", "Power")]
    per_turn = energy / max(sum(max(c["cost"], 0.5) for c in deck) / max(len(deck), 1), 0.5)
    sloth = next((pw for pw in p["powers"] if pw["name"] == "SlothPower"), None)
    out = []
    for c in cards:
        n, v = c["name"], c["vars"]
        if n == "Disintegration":
            cost = v.get("DisintegrationPower", 6) * turns
        else:
            limit = v.get("SlothPower", 3) + (sloth["amount"] if sloth else 0)
            f = {"MindRot": v.get("MindRotPower", 1) / HAND_SIZE,
                 "WasteAway": v.get("WasteAwayPower", 1) / max(energy, 1),
                 "Sloth": max(0, 1 - limit / per_turn)}.get(n, 0)
            cost = turns * min(f, 0.9) / (1 - min(f, 0.9)) * hit
        out.append((n, round(cost)))
    return sorted(out, key=lambda r: r[1])


def choose_card(state):
    """(best name, [(name, score or HP cost, label)]) for the choose-a-card screen."""
    sel = state["choose_card"]
    cards = sel["cards"]
    if any(c["name"] in DEMON_CURSES for c in cards):
        costs = demon_choice(state, cards)
        return costs[0][0], [(n, c, f"~{c} HP over the fight") for n, c in costs]
    if state.get("status") == "combat":  # generated card, free this turn: whichever makes the best plan now
        scored = [(c["name"], round(solve({**state, "hand": state["hand"] + [{**c, "cost": 0}],
                                           "choose_card": None})[0].score, 1)) for c in cards]
    else:
        scored = [(c["name"], rate(c, state.get("deck", []), state.get("relics", []), state)[0]) for c in cards]
    scored.sort(key=lambda r: -r[1])
    if state.get("status") == "combat":  # plan scores: only the difference to the best pick means anything
        return scored[0][0], [(n, s, "best plan" if s == scored[0][1] else f"{s - scored[0][1]:+.1f} vs best")
                              for n, s in scored]
    return scored[0][0], [(n, s, f"score {s}") for n, s in scored]


# --- shop ---
RELIC_VALUE = {"Common": 12, "Uncommon": 14, "Rare": 16, "Shop": 13}  # by rarity, when untiered
RELIC_TIER_VALUE = {"S": 20, "A": 15, "B": 11, "C": 7, "D": 3}  # knowledge.relics tier (Mobalytics)
POTION_VALUE = {"Common": 3, "Uncommon": 4, "Rare": 5}
POTION_FIT = 2  # bonus for a potion that feeds the deck's committed build
# knowledge.potions tier (Mobalytics): value in act 1 / later acts
POTION_TIER_VALUE = {"Good Always": (6, 6), "Good Early": (5, 3), "Good Late": (3, 5), "Niche": (2, 2),
                     "Mediocre": (1, 1)}
REMOVE_VALUE = {"Curse": 14, "Status": 10, "Basic": 7}  # by what gets removed; anything else 3
REMOVE_COMMIT = 3  # x commitment: a Basic removed from a committed deck draws the build more often
# Value of a gold not spent: what pathing says a later shop on the route buys with it (SHOP_PER_100), else
# what leftover gold is worth at the boss (GOLD_LEFT), so the map and shop advice price gold the same way.
GOLD_KEEP, GOLD_KEEP_SHOP_AHEAD = GOLD_LEFT, SHOP_PER_100 / 100
MAX_BASKET_ITEMS = 14  # brute-force cap: 2^14 baskets
# ponytail: relics valued by rarity only; add per-relic overrides if it buys bad relics.


def potion_value(name, state):
    """Mobalytics tier value for this act (act 1 vs later), else by rarity, plus POTION_FIT x how committed the
    deck is to a build the potion's text feeds (Poison Potion in a Poison deck). Under 2 = not worth a slot."""
    tier = KNOWLEDGE["potions"].get(name, {}).get("tier")
    value = (POTION_TIER_VALUE[tier][state.get("act", 0) > 0] if tier
             else POTION_VALUE.get(GAMEDATA["potions"].get(name, {}).get("rarity"), 3))
    text = GAMEDATA.get("text", {}).get(f"potions:{snake(name)}.description", "")
    built = commitment(state.get("deck", []), state.get("relics", []))
    fit = max((v for b, v in built.items() if text and re.search(ARCHETYPES[b.split(":")[0]][b.split(":")[1]][0], text)),
              default=0)
    return value + POTION_FIT * fit + foe_fit(name, state)


# B11: a potion is worth more when the fights it helps with are the ones coming. Kinds by the potion's own numbers:
# area (hits all enemies) helps vs groups; defense helps vs fights that hit harder than the act's usual; scaling
# (lasting buffs, poison) helps vs fights with more HP (longer). The next elites + the boss (reader queues) are
# compared with the act's elite/boss pool, so the tier value stays the average case.
POTION_KINDS = {"defense": {"Block", "BufferPower", "DexterityPower", "IntangiblePower", "PlatingPower", "RegenPower",
                            "HealPercent", "WeakPower", "DamageDecrease"},
                "scaling": {"StrengthPower", "FocusPower", "RitualPower", "PoisonPower", "DoomPower"}}
FOE_FIT, FOE_FIT_CAP = 3, 2


def foe_fit(name, state):
    """Potion value change for the known elites + boss ahead vs the act's usual ones (0 when the queue is unknown)."""
    pot = GAMEDATA["potions"].get(name) or {}
    kinds = [k for k, vs in POTION_KINDS.items() if vs & set(pot.get("vars", {}))]
    kinds += ["area"] if pot.get("target") == "AllEnemies" else []
    elites, _ = known_foes(state)
    ahead = list(elites) + list(known_boss(state))
    act = GAMEDATA.get("acts", {}).get(state.get("act_name")) or {}
    pool = act.get("bosses", []) + [e for e in act.get("encounters", [])
                                    if GAMEDATA["encounters"].get(e, {}).get("class") == "Elite"]
    if not kinds or not ahead or not pool:
        return 0
    asc = state.get("ascension", 0)
    stats = lambda names: [(hp, sum(inc) / max(len(inc), 1), multi) for hp, inc, multi, _ in
                           (encounter(e, asc) for e in names) if hp]
    now, usual = stats(ahead), stats(pool)
    if not now or not usual:
        return 0
    mean = lambda xs, i: sum(x[i] for x in xs) / len(xs)
    rel = lambda i: mean(now, i) / max(mean(usual, i), 1) - 1  # +0.3 = 30% more than usual
    fit = {"defense": rel(1), "scaling": rel(0), "area": mean(now, 2) - mean(usual, 2)}
    return round(max(-FOE_FIT_CAP, min(FOE_FIT_CAP, FOE_FIT * sum(fit[k] for k in kinds) / len(kinds))), 1)


SWAP_MARGIN = 1.5  # a new potion replaces the worst held one only when worth this much more


def potion_swap(state, name):
    """(held potion to drop, value gained) when every slot is full and `name` beats the worst held by SWAP_MARGIN."""
    held = [p["name"] for p in state.get("potions", [])]
    if not held or len(held) < state.get("potion_slots", 3):
        return None
    worst = min(held, key=lambda n: potion_value(n, state))
    gain = potion_value(name, state) - potion_value(worst, state)
    return (worst, round(gain, 1)) if gain >= SWAP_MARGIN else None


# What a curse costs a run, on the event scale (CURSE = a plain dead draw), from each card's game code: turn-end
# HP (Bad Luck 13 unblockable, Regret = hand size, Decay 2), a play cap (Normality 3), forced energy (Enthralled
# 2, played first), a debuff on us (Doubt Weak, Shame Frail), clogging (Innate Writhe/Folly, Retain Poor Sleep).
# Guilty removes itself after 5 combats (counted half used); Spore Mind can be played away for 1 energy.
CURSE_COST = {"BadLuck": 16, "Normality": 14, "Regret": 12, "Enthralled": 10, "Decay": 9, "Doubt": 9, "Shame": 9,
              "Writhe": 9, "Folly": 9, "PoorSleep": 9, "Debt": 9, "SporeMind": 5, "Guilty": 4}


def curse_cost(name):
    return CURSE_COST.get(name, -CURSE)


def removal_order(deck, relics=(), state=None):
    """Removable cards, worst first: Curse/Status (the costliest curse first, Guilty last: it leaves by itself),
    then the weakest Basic card (Strike/Defend), then the weakest."""
    def badness(c):
        tier = 0 if c["type"] in ("Curse", "Status") else 1 if c.get("rarity") == "Basic" else 2
        if tier == 0:
            return tier, -curse_cost(c["name"])
        rest = list(deck)
        rest.remove(c)
        return tier, rate(c, rest, relics, state)[0]  # what the deck loses without it
    return sorted((c for c in deck if not eternal(c)), key=badness)


def removal_target(deck, relics=(), state=None):
    return next(iter(removal_order(deck, relics, state)), None)


# --- deck grid out of combat (events, rest site, shop: upgrade / remove / transform / enchant a card) ---
DECK_VERBS = {"TO_UPGRADE": "Upgrade", "TO_REMOVE": "Remove", "TO_TRANSFORM": "Transform", "TO_ENCHANT": "Enchant"}


def deck_pick(state):
    """(verb, cards to pick, ranked distinct names) for a deck grid. Upgrade: biggest gain. Remove/Transform: junk,
    then the weakest. Enchant/other: the strongest card. ponytail: enchant ignores which enchantment it is
    (Sown, Sharpness, Nimble...); rate per enchantment if it picks badly."""
    g, deck, relics = state["card_grid"], state.get("deck", []), state.get("relics", [])
    screen = g.get("screen") or ""
    key = g.get("prompt") or next((f"TO_{w.upper()}" for w in ("Upgrade", "Transform", "Enchant") if w in screen), None)
    verb, pool = DECK_VERBS.get(key, "Pick"), list(g["cards"])
    if verb == "Upgrade":
        pool = [c for c in pool if not c["upgraded"] and c["name"] in UPGRADES] or pool
        order = sorted(pool, key=lambda c: -upgrade_gain(c, deck, UPGRADES, relics, state) if c["name"] in UPGRADES else 99)
    elif verb in ("Remove", "Transform"):
        order = removal_order(pool, relics, state)
    else:
        order = sorted(pool, key=lambda c: -rate(c, deck, relics, state)[0])
    picks = [c["name"] + ("+" if c["upgraded"] else "") for c in order[:max(g.get("max") or 1, 1)]]
    return verb, picks, list(dict.fromkeys(c["name"] + ("+" if c["upgraded"] else "") for c in order))


def eternal(card):
    """Eternal cards (Ascender's Bane, Bad Luck, Curse of the Bell...) can't be removed from the deck."""
    return "Eternal" in (card.get("keywords") or GAMEDATA["cards"].get(card["name"], {}).get("keywords", []))


# Relics with a drawback (from their game text): base penalty, and builds the drawback wrecks (penalty doubled
# when the deck is committed to one). Card-count caps hurt spam builds; no gold/potions hurt early.
# Relics that only help cards named "Strike"/"Defend" (game text): full value with WORD_CARDS_FULL such cards.
CARD_WORD_RELICS = {"StrikeDummy": "Strike", "FakeStrikeDummy": "Strike", "NutritiousSoup": "Strike", "PaelsClaw": "Defend"}
WORD_CARDS_FULL = 4
CLASH = 2  # a fully committed build that the drawback wrecks pays the penalty 3x (Velvet Choker + Shivs: skip)
RELIC_DRAWBACK = {
    "VelvetChoker": (4, {"Silent:Shiv", "Silent:Sly", "Defect:Claw", "Necrobinder:Souls"}),
    "DiamondDiadem": (2, {"Silent:Shiv", "Silent:Sly", "Defect:Claw", "Necrobinder:Souls"}),
    "Ectoplasm": (6, set()), "Sozu": (4, set()), "PhilosophersStone": (5, {"Ironclad:Block", "Defect:Orbs"}),
    "BigMushroom": (2, set()), "BlessedAntler": (3, {"Defect:Claw", "Silent:Sly"}),
    "CallingBell": (5, set()), "NeowsBones": (5, set()), "SereTalon": (4, set()),
    # Research Notes "Avoid": random costs break 0-cost/cycle builds; a kept hand stops discard triggers
    "SneckoEye": (2, {"Defect:Claw", "Silent:Sly", "Necrobinder:Souls"}),
    "RunicPyramid": (1, {"Silent:Sly", "Ironclad:Exhaust"}),
}


def relic_value(name, state=None):
    tier = KNOWLEDGE["relics"].get(name, {}).get("tier")
    value = RELIC_TIER_VALUE[tier] if tier else RELIC_VALUE.get(GAMEDATA["relics"].get(name, {}).get("rarity"), 12)
    if name in RELIC_DRAWBACK:
        base, hates = RELIC_DRAWBACK[name]
        built = commitment(state.get("deck", []), state.get("relics", [])) if state else {}
        clash = max((built.get(b, 0) for b in hates), default=0)
        late = state.get("act", 0) >= 2 if state else False  # gold/potion drawbacks matter less at the end
        value -= base * (0.5 if late and name in ("Ectoplasm", "Sozu") else 1) * (1 + CLASH * clash)
    if name in CARD_WORD_RELICS and state:  # Strike Dummy in a deck that removed its Strikes is worth little
        word = CARD_WORD_RELICS[name]
        have = sum(word in c["name"] for c in state.get("deck", []))
        value *= min(1, have / WORD_CARDS_FULL)
    if state:
        value += relic_fit(name, plan(state), state.get("deck", []))[0] + learned("relics", name, state)
    return value


def shop_basket(state):
    """[(kind, name)] to buy: kind is card / relic / potion / remove (name = the card to remove)."""
    return _shop(state)[2]


def shop_advice(state):
    """Return (headline, detail lines): the most valuable basket that fits in your gold and potion slots."""
    return _shop(state)[:2]


def _shop(state):
    shop, deck, relics, gold = state["shop"], state["deck"], state.get("relics", []), state.get("gold", 0)
    route = (best_routes(state, top=1) or [None])[0]
    keep = GOLD_KEEP_SHOP_AHEAD if route and "Shop" in route.types else GOLD_KEEP
    items, keys = [], []  # (label, price, value, is_potion); keys[i] = autopilot target ("card", name)...
    for e in shop["cards"]:
        value = max(0, rate(e["card"], deck, relics, state)[0] - SKIP_BELOW)
        items.append((e["card"]["name"] + (" (sale)" if e["sale"] else ""), e["price"], value, False))
        keys.append(("card", e["card"]["name"]))
    for r in shop["relics"]:
        items.append((r["name"], r["price"], relic_value(r["name"], state), False))
        keys.append(("relic", r["name"]))
    swaps = {p["name"]: sw for p in shop["potions"] if (sw := potion_swap(state, p["name"]))}
    best_swap = max(swaps, key=lambda n: swaps[n][1], default=None)  # slots full: one swap per visit
    for p in shop["potions"]:
        if p["name"] == best_swap:  # worth only what it adds over the potion it replaces; needs no free slot
            items.append((f"{p['name']} (drop {swaps[best_swap][0]})", p["price"], swaps[best_swap][1], False))
        else:
            items.append((p["name"], p["price"], potion_value(p["name"], state), True))
        keys.append(("potion", p["name"]))
    worst = removal_target(deck, relics, state)
    if shop.get("removal") is not None and worst:
        tier = worst["type"] if worst["type"] in ("Curse", "Status") else worst.get("rarity")
        p = plan(state)
        bonus = REMOVE_COMMIT * p["level"] if p["committed"] and tier == "Basic" else 0
        items.append((f"Remove {worst['name']}", shop["removal"], REMOVE_VALUE.get(tier, 3) + bonus, False))
        keys.append(("remove", worst["name"]))
    slots = state.get("potion_slots", 3) - len(state.get("potions", []))
    worth = sorted((i for i, it in enumerate(items) if it[2] > keep * it[1]),
                   key=lambda i: -items[i][2] / max(items[i][1], 1))[:MAX_BASKET_ITEMS]
    best, best_score = (), 0
    for n in range(1, len(worth) + 1):
        for basket in combinations(worth, n):
            spent = sum(items[i][1] for i in basket)
            if spent <= gold and sum(items[i][3] for i in basket) <= slots:
                score = sum(items[i][2] for i in basket) - keep * spent
                if score > best_score:
                    best, best_score = basket, score
    lines = [f"{'*' if i in best else ' '} {label:<22} {price:>4}g  value {value:.1f}"
             for i, (label, price, value, _) in sorted(enumerate(items), key=lambda x: -x[1][2])]
    if not best:
        return f"Leave - save gold ({gold})", lines, []
    spent = sum(items[i][1] for i in best)
    return f"Buy: {' + '.join(items[i][0] for i in best)}  [{spent}/{gold}]", lines, [keys[i] for i in best]


# --- events (? rooms and Ancients) ---
# Each option's English text (gamedata.text, from the game's loc files) with the event's numbers filled in,
# scored by phrase on the same scale as pathing (relic 12, upgrade 6, card 4...). HP lost counts 3x
# when it drops you under LOW_HP of max. Unparsed text scores 0 and shows "?".
MAX_HP_VALUE, EVENT_GOLD, POTION_PICK, TRANSFORM, CURSE, FIGHT, ENCHANT = 0.5, 0.05, 3, 3, -8, -6, 4
LOW_HP = 0.4
WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}
# ponytail: one regex per phrase; multi-step events (Abyssal Baths' Linger pages) are scored one page at a time.


def option_text(state, option):
    text = GAMEDATA.get("text", {}).get(option["desc"] or "", "")
    v = state["event"]["vars"]
    def fill(m):
        val, fmt = v.get(m[1], "?"), m[2] or ""
        if fmt.startswith("plural:") and "|" in fmt:  # {Cards:plural:card|cards}, {N:plural:x|{} xs}
            one, many = fmt[len("plural:"):].split("|", 1)
            return (one if val == 1 else many).replace("{}", str(val))
        if icon := re.match(r"(energy|star)Icons\((\d*)\)", fmt):  # {Energy:energyIcons()}, {x:energyIcons(1)}
            return f"{icon[2] or val} {icon[1]}"
        return str(val)
    return re.sub(r"\{(\w+)(?::((?:[^{}]|\{\})*))?\}", fill, text)


# English title -> class, for names in event text and vars ("Sword of Stone" -> SwordOfStone)
TITLES = {k: {t.lower(): c for c in GAMEDATA[k] if (t := GAMEDATA.get("text", {}).get(f"{k}:{snake(c).upper()}.title"))}
          for k in ("cards", "relics", "potions")}


def named(kind, s):
    """Game class for a name (class or English title, wiki "(relic)" tags dropped), else None."""
    s = re.sub(r"\s*\((?:relic|curse|card|potion)\)", "", str(s), flags=re.I).strip(" .")
    key = re.sub(r"\W", "", s)
    return key if key in GAMEDATA[kind] else TITLES[kind].get(s.lower())


def card_value(name, state):
    """A named card gained at an event, on the event scale: what the run plan says it adds over a skip
    (same as the shop), so an off-build card can be worth less than nothing."""
    if GAMEDATA["cards"][name]["type"] == "Curse":
        return -curse_cost(name)
    return rate(gd_card(name), state.get("deck", []), state.get("relics", []), state)[0] - SKIP_BELOW


def trial_value(state):
    """Trial: the next page is one of three verdict pages; you take the better verdict on it. Mean of those."""
    t = GAMEDATA.get("text", {})
    pages = [[v for k, v in t.items() if k.startswith(f"events:TRIAL.pages.{pg}.options.") and k.endswith(".description")]
             for pg in ("MERCHANT", "NOBLE", "NONDESCRIPT")]
    best = [max(option_value(state, {"relic": None}, x)[0] for x in opts) for opts in pages if opts]
    return sum(best) / len(best) if best else 0


def option_value(state, option, text):
    """(score, parsed anything?) for one event option."""
    if option.get("relic"):
        return relic_value(option["relic"], state), True
    p = state["player"]
    n = lambda s: WORDS.get(s.lower(), None) if not s.isdigit() else int(s)
    score, hp, hit = 0.0, p["hp"], False
    rules = [
        (r"Lose (\d+) Max HP", lambda k: -MAX_HP_VALUE * k - HP_VALUE * k),
        (r"Gain (\d+) Max HP", lambda k: MAX_HP_VALUE * k),
        (r"(?:Lose|Take) (\d+) (?:HP|damage)", "hp"),
        (r"Heal (\d+) HP", "heal"),
        (r"(?:Pay|Lose) (\d+) Gold", lambda k: -EVENT_GOLD * k),
        (r"(?:Gain|Obtain) (\d+) Gold", lambda k: EVENT_GOLD * k),
        (r"Gain (a|\d+) card rewards?", lambda k: CARD_REWARD * k),
        (r"Obtain (a|\d+) random (?:\w+ )?Relics?", lambda k: RELIC * k),
        (r"Obtain (a|\d+) (?:random )?(?:[\w-]+ )?(?:[Cc]ards?|Power)\b|Choose (\d+) of \d+ random", lambda k: CARD_REWARD * k),
        (r"Upgrade (a|\d+) random cards?", lambda k: 0.7 * UPGRADE * k),
        (r"Downgrade (a|\d+) random cards?", lambda k: -0.7 * UPGRADE * k),
        (r"Upgrade (a|\d+) cards?\b", lambda k: UPGRADE * k),
        # Tea Master (P5, rough): upgraded starting hand once, Strength for N fights, Dazed in the next fight
        (r"Upgrade your starting (hand)", lambda k: UPGRADE / 2),
        (r"next (\d+) combats, gain \d+ Strength", lambda k: 1.2 * k),
        (r"shuffle (\d+) Dazed", lambda k: -k),
        (r"Face lethal (repercussions)", lambda k: -1000),  # Trial's Double Down
        # Tinker Time riders on the custom card (rough, per play of that card)
        (r"Hits (\d+) additional times", lambda k: 3 * k),
        (r"Draw (\d+) cards\.", lambda k: 1.5 * k),
        (r"the enemy loses (\d+) HP", lambda k: k),
        (r"Gain (\d+) Strength\. Gain \d+ Dexterity", lambda k: 4 * k),
        (r"Apply (\d+) Weak\. Apply \d+ Vulnerable", lambda k: 3 * k),
        (r"Add a random card into your (Hand)", lambda k: 3),
        (r"Powers cost (\d+) \w+ less", lambda k: 2 * k),
        (r"Gain (\d+) energy", lambda k: 3 * k),  # Energized ("At the end of combat, Upgrade a random card": above)
        (r"Create a custom (card) to add to your Deck", lambda k: CARD_REWARD),
        (r"Remove (a|\d+) (?:cards?|Strikes|Defends)", lambda k: REMOVE_VALUE["Basic"] * k),
        (r"Transform (a|\d+) cards?|(a) card to Transform", lambda k: TRANSFORM * k),
        (r"Procure (a|\d+) (?:random )?(?:\w+ )?Potions?", lambda k: POTION_PICK * k),
        (r"Enchant (an?|\d+) |Choose (an?) \w+ to Enchant", lambda k: ENCHANT),
        (r"Fight", lambda k: FIGHT),
    ]
    for pat, f in rules:
        for m in re.finditer(pat, text):
            hit = True
            k = n(next((g for g in m.groups() if g), "1")) or 1
            if f == "hp":
                hp -= k
                score -= HP_VALUE * k * (3 if hp < LOW_HP * p["max_hp"] else 1)
            elif f == "heal":
                score += HP_VALUE * min(k, p["max_hp"] - hp)
            else:
                score += f(k)
    if "Heal to full HP" in text:
        hit, score = True, score + HP_VALUE * (p["max_hp"] - hp)
    m = re.match(r"Make an? (Attack|Skill|Power)\.$", text)  # Tinker Time: the type the run plan lacks most
    if m:
        gaps = plan(state)["gaps"]
        need = {"Attack": gaps.get("aoe", 0), "Skill": gaps.get("block", 0), "Power": gaps.get("scaling", 0)}
        hit, score = True, score + CARD_REWARD * (1 + need[m[1]])
    for m in re.finditer(r"Trade ([\w' ]+?) for ([\w' ]+?)\.", text):  # Relic Trader: one of yours for a new one
        if (old := named("relics", m[1])) and (new := named("relics", m[2])):
            hit, score = True, score + relic_value(new, state) - relic_value(old, state)
    if re.search(r"Serve as today's Decider|not allowed to Reject", text):  # Trial: a random verdict page next
        hit, score = True, score + trial_value(state)
    for m in re.finditer(r"Gain an? ([A-Z][\w' ]+)\.", text):  # Crystal Sphere's Debt (a curse)
        if card := named("cards", m[1]):
            hit, score = True, score + card_value(card, state)
    for m in re.finditer(r"Add ([\w' ]+?) to your Deck", text):
        hit, card = True, named("cards", m[1])
        score += card_value(card, state) if card else CARD_REWARD
    for m in re.finditer(r"card to Transform into ([\w' ]+)", text):  # Wood Carvings: a Basic becomes this card
        if card := named("cards", m[1]):
            hit, score = True, score + REMOVE_VALUE["Basic"] + card_value(card, state)
    for m in re.finditer(r"(Obtain|Receive|Procure) (?:the )?([A-Z][\w' ]+)\.", text):
        if "random" in m[2]:
            continue
        if relic := named("relics", m[2]):
            hit, score = True, score + relic_value(relic, state)
        elif potion := named("potions", m[2]):
            hit, score = True, score + potion_value(potion, state)
        elif card := named("cards", m[2]):  # Spoils Map, Bad Luck
            hit, score = True, score + card_value(card, state)
    if hp <= 0:
        score = -1000
    return round(score, 1), hit


def wiki_option(option, label):
    """The wiki's text for this option (advisor/wiki.json events, matched by the option's English title), else None.
    ponytail: Trial-style "DECIDE: Guilty" sub-options only match by their own label; a page-aware lookup if needed."""
    event = (option.get("desc") or "").split(":")[-1].split(".")[0]
    return next((re.sub(r"\s*\((?:relic|curse|card|potion)\)", "", t, flags=re.I)
                 for lab, t in WIKI.get("events", {}).get(event, {}).get("options", [])
                 if label and lab.lower() == str(label).lower()), None)


def event_choice(state):
    """The option dict autopilot clicks (best unlocked), or None."""
    return event_advice(state, raw=True)


def event_advice(state, raw=False):
    """(headline, detail lines) for an event: every option's text and score."""
    ev = state["event"]
    title = lambda r: GAMEDATA.get("text", {}).get((r[3]["desc"] or "").replace(".description", ".title")) \
        or r[3]["relic"] or r[3]["key"]
    rated = []
    for o in ev["options"]:
        text = option_text(state, o) or (o["relic"] or o["key"] or "?")
        score, ok = option_value(state, o, text)
        if not ok and not o["locked"]:  # the wiki's outcome text (Trial verdicts, Tea of Discourtesy...)
            text = wiki_option(o, title((0, 0, "", o))) or text
            score, ok = option_value(state, o, text)
        if o["locked"]:
            score = -math.inf
        rated.append((score, ok, text, o))
    best = max((r for r in rated if not r[3]["locked"]), key=lambda r: r[0], default=None)
    if raw:
        return best[3] if best else None
    lines = [f"{'LOCKED ' if r[3]['locked'] else ''}{title(r)}: {r[2][:70]}  "
             f"[{'?' if not r[1] else f'{r[0]:+.0f}'}]" for r in rated]
    first = next((o["desc"] for o in ev["options"] if o["desc"]), "")
    name = GAMEDATA.get("text", {}).get(first.split(".pages")[0] + ".title", ev["name"])
    return (f"{name}: {title(best)}" if best else name), lines