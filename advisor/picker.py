"""Card-reward, rest-site and shop advice: rate cards from their numbers, what the deck lacks, and relics."""
import math
import re
from itertools import combinations

from pathing import CARD_REWARD, HEAL_FRACTION, HP_VALUE, RELIC, UPGRADE, best_routes, fight_costs, should_rest
from enemies import predict
from relics import GAMEDATA, KNOWLEDGE, Relics, dmg_add
from solver import deck_dpt, solve

# Value per point of each card var. Tune these.
VAR_VALUE = {"Damage": 1, "Block": 0.9, "VulnerablePower": 4, "WeakPower": 3,
             "StrengthPower": 6, "DexterityPower": 5, "Cards": 3, "Energy": 6, "HpLoss": -1.5}
UNKNOWN_VALUE = 3      # flat credit for effects we can't read from numbers (shown as "?")
AOE_NEEDED_BONUS = 4   # card hits all enemies and the deck has no AoE attack
BLOCK_NEEDED_BONUS = 2  # card gives block and under 30% of the deck does
DECK_SIZE_FREE = 20    # past this, every pick costs a little (dilution)
DECK_SIZE_PENALTY = 0.3
SKIP_BELOW = 6
# Relic synergies: relic -> (which cards benefit, bonus). Damage relics (Strike Dummy etc.) are applied
# directly through relics.dmg_add instead.
ATTACK_COUNT_RELICS = {"Kunai", "Shuriken", "OrnamentalFan", "PenNib", "Nunchaku", "Kusarigama"}
SYNERGY = [
    (ATTACK_COUNT_RELICS, lambda c: c["type"] == "Attack" and c["cost"] <= 1, 1),
    ({"LetterOpener", "TuningFork"}, lambda c: c["type"] == "Skill", 1),
    ({"Vambrace", "PaelsLegion", "Orichalcum"}, lambda c: "Block" in c["vars"], 1),
    ({"LostWisp", "Permafrost", "GamePiece", "MummifiedHand"}, lambda c: c["type"] == "Power", 2),
]
# Community tier (knowledge.json cards[X].tier, from Mobalytics) blended 50/50 with the stats score: the
# stats can't see engines (exhaust, Strength scaling) and overrate one-turn buffs like Setup Strike.
TIER_VALUE = {"S": 16, "A": 12, "B": 9, "C": 6, "D": 3}
TIER_BLEND = 0.5
# ponytail: no card-to-card synergy model beyond SYNERGY and the tier list.


def is_aoe(card):
    return card["type"] == "Attack" and card["target"] in ("AllEnemies", "RandomEnemy")


def rate(card, deck, relics=()):
    R = Relics(relics, GAMEDATA["relics"])
    v = card["vars"]
    hits = max(v.get("Repeat", 1), 1)
    value = sum(VAR_VALUE[k] * n for k, n in v.items() if k in VAR_VALUE and k not in ("Damage",))
    if "Damage" in v:
        value += (v["Damage"] + dmg_add(card, R)) * hits * (1.5 if is_aoe(card) else 1)
    modeled = set(v) <= set(VAR_VALUE) | {"Repeat"} and bool(v)
    if not modeled:
        value += UNKNOWN_VALUE
    score = value / max(3 if card["x_cost"] else card["cost"], 1)
    if is_aoe(card) and not any(is_aoe(c) for c in deck):
        score += AOE_NEEDED_BONUS
    if "Block" in v and sum("Block" in c["vars"] for c in deck) < 0.3 * len(deck):
        score += BLOCK_NEEDED_BONUS
    score += sum(bonus for names, fits, bonus in SYNERGY if fits(card) and any(n in R for n in names))
    score -= DECK_SIZE_PENALTY * max(0, len(deck) - DECK_SIZE_FREE)
    tier = KNOWLEDGE["cards"].get(card["name"], {}).get("tier")
    if tier:
        score = (1 - TIER_BLEND) * score + TIER_BLEND * TIER_VALUE[tier]
        modeled = True  # the tier covers what the numbers miss
    return round(score, 1), modeled


def pick(cards, deck, relics=()):
    rated = [(c["name"], *rate(c, deck, relics)) for c in cards]
    best = max(rated, key=lambda r: r[1], default=None)
    return (best[0] if best and best[1] >= SKIP_BELOW else "Skip"), rated


# --- rest site ---
# Rest-or-smith thresholds live in pathing.should_rest, so the route search and this screen agree.
KEYWORD_VALUE = {"-Exhaust": 3, "-Ethereal": 2, "+Innate": 1, "+Retain": 1}
UPGRADES = GAMEDATA["upgrades"]


def upgraded(card, up):
    v = dict(card["vars"])
    for k, d in up.get("vars", {}).items():
        v[k] = v.get(k, 0) + d
    return {**card, "vars": v, "cost": max(0, card["cost"] + up.get("cost", 0)), "upgraded": True}


def upgrade_gain(card, deck, upgrades=UPGRADES, relics=()):
    up = upgrades[card["name"]]
    gain = rate(upgraded(card, up), deck, relics)[0] - rate(card, deck, relics)[0]
    gain += sum(KEYWORD_VALUE.get(k, 1) for k in up.get("keywords", []))
    return round(gain, 1)


def rest_advice(state, upgrades=UPGRADES):
    """Return (headline, detail lines) for the rest-site screen."""
    p, deck, opts = state["player"], state["deck"], state["rest_site"]["options"]
    relics = state.get("relics", [])
    heal = min(int(p["max_hp"] * HEAL_FRACTION), p["max_hp"] - p["hp"])
    seen, gains = set(), []
    for c in deck:
        if not c["upgraded"] and c["name"] in upgrades and c["name"] not in seen:
            seen.add(c["name"])
            gains.append((c["name"], upgrade_gain(c, deck, upgrades, relics)))
    gains.sort(key=lambda g: -g[1])
    others = [o for o in opts if o not in ("Heal", "Smith")]
    tail = [f"also available: {', '.join(others)} (not rated)"] if others else []
    ahead = (best_routes(state, top=1) or [None])[0]  # rooms on the best route after this camp
    reason = should_rest(p["hp"], p["max_hp"], ahead.types if ahead else [], fight_costs(state))
    if "Smith" in opts and gains and not reason:
        return f"Smith -> upgrade {gains[0][0]}", [f"{n:<16} +{g}" for n, g in gains[:3]] + tail
    return f"Rest  (heal +{heal})", [f"HP {p['hp']}/{p['max_hp']}" + (f"  ({reason})" if reason else "")] + tail


# --- in-combat hand picks (Armaments, True Grit, discard effects...) ---
BAD_PICKS = {"TO_EXHAUST", "TO_DISCARD", "TO_REMOVE", "TO_TRANSFORM"}
# ponytail: eligible cards are guessed (upgrade: upgradable cards; else the whole hand). Card filters like
# "exhaust a Skill" aren't read, so autopilot stops if a pick doesn't register.


def hand_pick(state, upgrades=UPGRADES):
    """Cards to pick when a card asks. Returns (number keys, highest first, since picked cards leave the
    row; names). Upgrade: biggest gain. Exhaust/discard: Curse/Status, then weakest. Else: strongest."""
    sel, hand, deck, relics = state["hand_select"], state["hand"], state.get("deck", []), state.get("relics", [])
    upgrade = sel["mode"] == "upgrade"
    pool = [c for c in hand if not upgrade or (not c["upgraded"] and c["name"] in upgrades)]
    junk = lambda c: c["type"] in ("Curse", "Status")
    if upgrade:
        order = sorted(range(len(pool)), key=lambda i: -upgrade_gain(pool[i], deck, upgrades, relics))
        n = sel["max"]
    elif sel["prompt"] in BAD_PICKS:
        order = sorted(range(len(pool)), key=lambda i: (not junk(pool[i]), rate(pool[i], deck, relics)[0]))
        n = max(sel["min"], min(sel["max"], sum(map(junk, pool))))  # optional picks: only junk
    else:
        order = sorted(range(len(pool)), key=lambda i: -rate(pool[i], deck, relics)[0])
        n = max(sel["min"], min(sel["max"], 1))
    chosen = sorted(order[:max(0, n - sel["selected"])], reverse=True)
    return [i + 1 for i in chosen], [pool[i]["name"] for i in chosen]


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
        scored = [(c["name"], rate(c, state.get("deck", []), state.get("relics", []))[0]) for c in cards]
    scored.sort(key=lambda r: -r[1])
    return scored[0][0], [(n, s, f"score {s}") for n, s in scored]


# --- shop ---
RELIC_VALUE = {"Common": 12, "Uncommon": 14, "Rare": 16, "Shop": 13}  # by rarity, when untiered
RELIC_TIER_VALUE = {"S": 20, "A": 15, "B": 11, "C": 7, "D": 3}  # knowledge.relics tier (Mobalytics)
POTION_VALUE = {"Common": 3, "Uncommon": 4, "Rare": 5}
# knowledge.potions tier (Mobalytics): value in act 1 / later acts
POTION_TIER_VALUE = {"Good Always": (6, 6), "Good Early": (5, 3), "Good Late": (3, 5), "Niche": (2, 2),
                     "Mediocre": (1, 1)}
REMOVE_VALUE = {"Curse": 14, "Status": 10, "Basic": 7}  # by what gets removed; anything else 3
GOLD_KEEP, GOLD_KEEP_SHOP_AHEAD = 0.02, 0.05  # value of a gold not spent (more if another shop is coming)
MAX_BASKET_ITEMS = 14  # brute-force cap: 2^14 baskets
# ponytail: relics valued by rarity only; add per-relic overrides if it buys bad relics.


def removal_target(deck, relics=()):
    """Curse/Status first, then the weakest Basic card (Strike/Defend), then the weakest card."""
    def badness(c):
        tier = 0 if c["type"] in ("Curse", "Status") else 1 if c.get("rarity") == "Basic" else 2
        return tier, rate(c, deck, relics)[0]
    return min(deck, key=badness, default=None)


def relic_value(name):
    tier = KNOWLEDGE["relics"].get(name, {}).get("tier")
    return RELIC_TIER_VALUE[tier] if tier else RELIC_VALUE.get(GAMEDATA["relics"].get(name, {}).get("rarity"), 12)


def shop_advice(state):
    """Return (headline, detail lines): the most valuable basket that fits in your gold and potion slots."""
    shop, deck, relics, gold = state["shop"], state["deck"], state.get("relics", []), state.get("gold", 0)
    route = (best_routes(state, top=1) or [None])[0]
    keep = GOLD_KEEP_SHOP_AHEAD if route and "Shop" in route.types else GOLD_KEEP
    items = []  # (label, price, value, is_potion)
    for e in shop["cards"]:
        value = max(0, rate(e["card"], deck, relics)[0] - SKIP_BELOW)
        items.append((e["card"]["name"] + (" (sale)" if e["sale"] else ""), e["price"], value, False))
    for r in shop["relics"]:
        items.append((r["name"], r["price"], relic_value(r["name"]), False))
    for p in shop["potions"]:
        tier = KNOWLEDGE["potions"].get(p["name"], {}).get("tier")
        value = (POTION_TIER_VALUE[tier][state.get("act", 0) > 0] if tier
                 else POTION_VALUE.get(GAMEDATA["potions"].get(p["name"], {}).get("rarity"), 3))
        items.append((p["name"], p["price"], value, True))
    worst = removal_target(deck, relics)
    if shop.get("removal") is not None and worst:
        tier = worst["type"] if worst["type"] in ("Curse", "Status") else worst.get("rarity")
        items.append((f"Remove {worst['name']}", shop["removal"], REMOVE_VALUE.get(tier, 3), False))
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
        return f"Leave - save gold ({gold})", lines
    spent = sum(items[i][1] for i in best)
    return f"Buy: {' + '.join(items[i][0] for i in best)}  [{spent}/{gold}]", lines


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
        val = v.get(m[1], "?")
        if m[2]:  # {Cards:plural:card|cards}
            one, many = m[2].split(":", 1)[-1].split("|")
            return one if val == 1 else many
        return str(val)
    return re.sub(r"\{(\w+)(?::([^}]*))?\}", fill, text)


def option_value(state, option, text):
    """(score, parsed anything?) for one event option."""
    if option.get("relic"):
        return relic_value(option["relic"]), True
    p = state["player"]
    n = lambda s: WORDS.get(s.lower(), None) if not s.isdigit() else int(s)
    score, hp, hit = 0.0, p["hp"], False
    rules = [
        (r"Lose (\d+) Max HP", lambda k: -MAX_HP_VALUE * k - HP_VALUE * k),
        (r"Gain (\d+) Max HP", lambda k: MAX_HP_VALUE * k),
        (r"(?:Lose|Take) (\d+) (?:HP|damage)", "hp"),
        (r"Heal (\d+) HP", "heal"),
        (r"(?:Pay|Lose) (\d+) Gold", lambda k: -EVENT_GOLD * k),
        (r"Gain (\d+) Gold", lambda k: EVENT_GOLD * k),
        (r"Obtain (a|\d+) random (?:\w+ )?Relics?", lambda k: RELIC * k),
        (r"Obtain (a|\d+) (?:random )?(?:[\w-]+ )?(?:cards?|Power)\b|Choose (\d+) of \d+ random", lambda k: CARD_REWARD * k),
        (r"Upgrade (a|\d+) random cards?", lambda k: 0.7 * UPGRADE * k),
        (r"Upgrade (a) card", lambda k: UPGRADE * k),
        (r"Remove (a|\d+) (?:cards?|Strikes|Defends)", lambda k: REMOVE_VALUE["Basic"] * k),
        (r"Transform (a|\d+) cards?|(a) card to Transform", lambda k: TRANSFORM * k),
        (r"Procure (a|\d+) (?:random )?(?:\w+ )?Potions?", lambda k: POTION_PICK * k),
        (r"Enchant (an?) |Choose (an?) \w+ to Enchant", lambda k: ENCHANT),
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
    for m in re.finditer(r"Add ([\w' ]+?) to your Deck", text):
        hit = True
        card = GAMEDATA["cards"].get(m[1].replace(" ", "").replace("'", ""), {})
        score += CURSE if card.get("type") == "Curse" or m[1] in ("Regret", "Doubt") else CARD_REWARD
    m = re.search(r"Obtain (?:the )?([A-Z][\w' ]+)\.", text)
    if m and "random" not in m[1]:
        name = m[1].replace(" ", "").replace("'", "")
        if name in GAMEDATA["relics"]:
            hit, score = True, score + relic_value(name)
    if hp <= 0:
        score = -1000
    return round(score, 1), hit


def event_advice(state):
    """(headline, detail lines) for an event: every option's text and score."""
    ev = state["event"]
    rated = []
    for o in ev["options"]:
        text = option_text(state, o) or (o["relic"] or o["key"] or "?")
        score, ok = option_value(state, o, text)
        if o["locked"]:
            score = -math.inf
        rated.append((score, ok, text, o))
    best = max((r for r in rated if not r[3]["locked"]), key=lambda r: r[0], default=None)
    title = lambda r: GAMEDATA.get("text", {}).get((r[3]["desc"] or "").replace(".description", ".title")) \
        or r[3]["relic"] or r[3]["key"]
    lines = [f"{'LOCKED ' if r[3]['locked'] else ''}{title(r)}: {r[2][:70]}  "
             f"[{'?' if not r[1] else f'{r[0]:+.0f}'}]" for r in rated]
    first = next((o["desc"] for o in ev["options"] if o["desc"]), "")
    name = GAMEDATA.get("text", {}).get(first.split(".pages")[0] + ".title", ev["name"])
    return (f"{name}: {title(best)}" if best else name), lines