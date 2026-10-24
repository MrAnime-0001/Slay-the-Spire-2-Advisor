"""Deck balance: expected HP lost in the current act's boss and elite fights, from simulated hands.

Each sampled turn draws 5 cards (plus draw cards' extra cards), plays the best affordable set, and records
damage dealt and damage taken (incoming minus block). A fight then costs turns x unblocked damage per turn,
with turns = enemy HP / damage per turn. Too few attacks -> long fights; too little block -> big hits land;
dead cards (curses, statuses, weak filler) -> worse hands. Picks, shop and removal compare decks by this.

Common random numbers: turn k gives deck position i the fixed key KEYS[k][i] and draws the lowest keys, so
adding a card changes only the hands it lands in (differences between candidate cards aren't noise).
"""
import heapq
import random
from functools import lru_cache

from enemies import attacks, num
from relics import GAMEDATA

SIM_HANDS, HAND_SIZE, MAX_DECK = 150, 5, 120
KEYS = [[r.random() for _ in range(MAX_DECK)] for r in [random.Random(7)] for _ in range(SIM_HANDS)]
AOE_MULT, VULN_MULT, WEAK_MULT = 1.5, 1.5, 0.75  # AoE vs several enemies; Vulnerable/Weak inside a hand
POWER_UPTIME = 0.5  # a Strength/Dexterity power is in play for about half the fight
TURN_CAP = 60
LAMBDAS = (0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5)
GROWTH = 0.08  # enemies hit ~5% harder each turn (Strength buffs, Ritual...): long fights cost more than linear
# Engines: a poison stack P deals P + (P-1) + ... over a fight, ~POISON_DMG x P in a typical one; an orb's
# passive fires about ORB_TURNS times before it's evoked or the fight ends. Doom counts as its amount.
POISON_DMG, ORB_TURNS = 2.5, 3
STAR_BASE = 0.4  # Divine Right: 3 stars per fight, ~8 turns
FIGHT_TURNS, RETAIN_SIM = 8, 1.15


def card_keywords(name, upgraded=False):
    """Printed keywords, minus/plus what the upgrade changes (upgrades[X].keywords like "-Exhaust")."""
    kw = set(GAMEDATA["cards"].get(name, {}).get("keywords", []))
    for k in GAMEDATA.get("upgrades", {}).get(name, {}).get("keywords", []) if upgraded else []:
        (kw.discard if k[0] == "-" else kw.add)(k[1:])
    return kw
HALLWAY = 0.4  # share of a deck's value judged on hallway fights (most fights; HP lost there carries over)
FALLBACK = [(1.0, 150, (8, 20, 14), False)]  # (weight, HP, incoming per turn, several enemies) when act data is missing
# ponytail: enemy scaling over a fight, card draw beyond one level, exhaust/retain and orbs aren't simulated;
# the tier blend in picker.rate covers engines the numbers miss.


def card_key(c):
    return (c["name"], c.get("upgraded", False), c["type"], c.get("target"), c["cost"], c.get("x_cost", False),
            tuple(sorted(c["vars"].items())))


def encounter(name, asc):
    """(HP, incoming per turn, several enemies) of one encounter from the monster tables."""
    mons = GAMEDATA["encounters"].get(name, {}).get("monsters", [])
    hp, seqs = 0, []
    for m in mons:
        ai = GAMEDATA["monsters"].get(m)
        if not ai:
            continue
        hp += sum(num(x, asc) or 0 for x in ai["hp"]) / max(len(ai["hp"]), 1)
        seqs.append([sum(d * h for d, h in attacks(mv, asc)) for mv in ai["moves"].values()] or [0])
    # per-turn incoming: turn k uses each monster's move k (mod its move count), so big hits come as spikes
    # ponytail: move order = table order, not the real cycle; good enough for spike sizes and frequency
    n = max((len(s) for s in seqs), default=1)
    return hp, tuple(sum(s[k % len(s)] for s in seqs) for k in range(n)), len(mons) > 1


@lru_cache(maxsize=64)
def threats(act_name, asc):
    """[(weight, HP, incoming, multi)]: boss pool, elite pool, hallway pool (HALLWAY weight; bosses listed first)."""
    act = GAMEDATA.get("acts", {}).get(act_name)
    if not act:
        return FALLBACK
    enc = GAMEDATA["encounters"]
    bosses = [e for e in act.get("bosses", []) if e in enc]
    elites = [e for e in act.get("encounters", []) if enc.get(e, {}).get("class") == "Elite"]
    hallway = [e for e in act.get("encounters", []) if enc.get(e, {}).get("class") == "Monster"]
    out = []
    for pool, w in ((bosses, (1 - HALLWAY) / 2), (elites, (1 - HALLWAY) / 2), (hallway, HALLWAY)):
        for e in pool:
            hp, incs, multi = encounter(e, asc)
            if hp:
                out.append((w / len(pool), hp, incs, multi))
    return out or FALLBACK


@lru_cache(maxsize=256)
def _rate(keys, hp, incs, multi, energy):
    return fight(hands(keys, energy), hp, incs, multi, 0, 0)[4]


def exchange_rate(state):
    """HP one point of damage is worth in the live fight: the sim's HP-minimising pilot for this deck against this
    encounter's attacks and the enemies' remaining HP. None without cards or enemies."""
    deck, enc, foes = state.get("deck") or state.get("hand"), state.get("encounter"), state.get("enemies", [])
    if not deck or not foes:
        return None
    if enc in GAMEDATA["encounters"]:
        _, incs, multi = encounter(enc, state.get("ascension", 0))
    else:  # unknown fight: assume the enemies keep hitting like they intend to now
        incs = (sum((it.get("damage") or 0) * max(it.get("hits", 1), 1) for e in foes for it in e.get("intents", [])),)
        multi = len(foes) > 1
    hp = sum(e["creature"]["hp"] for e in foes)
    return _rate(tuple(card_key(c) for c in deck), round(hp, -1) or 1, incs, multi, state.get("max_energy", 3))


def effect(key):
    """(cost, dmg single, dmg aoe, block, draw, energy, vuln, weak, self hp loss, perm str, perm dex) or None."""
    name, _, ctype, target, cost, x, v = key
    v = dict(v)
    if ctype in ("Curse", "Status") or (cost < 0 and not x):
        return None  # takes a hand slot, does nothing
    times = 2 if x else 1  # ponytail: X costs treated as 2 energy for 2x effect
    cost = 2 if x else cost
    made = GAMEDATA["cards"].get(name, {}).get("creates")
    if made and made[0] in GAMEDATA["cards"]:  # Blade Dance: 3 Shivs played the same turn, not "draw 3"
        n = v.pop(made[1], 1) if made[1] else 1
        mv = GAMEDATA["cards"][made[0]]
        cost += mv["cost"] * n if mv["type"] not in ("Status", "Curse") else 0
        for k2, x2 in (mv["vars"].items() if mv["type"] not in ("Status", "Curse") else ()):
            v[k2] = v.get(k2, 0) + x2 * n
        if mv["type"] == "Attack" and ctype != "Attack":
            ctype = "Attack"  # its damage counts
    dmg = v.get("Damage", 0) * max(v.get("Repeat", 1), 1) * times if ctype == "Attack" else 0
    aoe = target in ("AllEnemies", "RandomEnemy")
    power = ctype == "Power"
    block = v.get("Block", 0) * times
    if not power:  # character engines, as damage/block-equivalents per play
        poison = v.get("PoisonPower", 0) * (max(v.get("Repeat", 1), 1) if ctype != "Attack" else 1)  # Bouncing Flask
        dmg += (v.get("OstyDamage", 0) + POISON_DMG * poison + v.get("DoomPower", 0)) * times
        block += v.get("Summon", 0) * times  # Osty soaks hits
        for orb in GAMEDATA["cards"].get(name, {}).get("channel", []):
            dmg += ORB_TURNS * {"Lightning": 3, "Dark": 6, "Glass": 3}.get(orb, 0)
            block += ORB_TURNS * (orb == "Frost") * 2
    return (cost, 0 if aoe else dmg, dmg if aoe else 0, block, int(v.get("Cards", 0)),
            v.get("Energy", 0), v.get("VulnerablePower", 0) > 0 and not power, v.get("WeakPower", 0) > 0 and not power,
            v.get("HpLoss", 0), v.get("StrengthPower", 0) if power else 0, v.get("DexterityPower", 0) if power else 0)


def options(hand, energy):
    """Maximal affordable sets of a hand as (dmg single, dmg aoe, block, weak, hp loss)."""
    cards = [e for e in hand if e]
    n = len(cards)
    need = [0] * (1 << n)
    for m in range(1, 1 << n):
        low = (m & -m).bit_length() - 1
        need[m] = need[m & (m - 1)] + cards[low][0] - cards[low][5]
    ok = [need[m] <= energy for m in range(1 << n)]
    out = []
    for m in range(1 << n):
        if not ok[m] or any(ok[m | (1 << i)] for i in range(n) if not m >> i & 1):
            continue  # unaffordable, or another card still fits
        picked = [cards[i] for i in range(n) if m >> i & 1]
        vuln = VULN_MULT if any(e[6] for e in picked) else 1
        out.append((sum(e[1] for e in picked) * vuln, sum(e[2] for e in picked) * vuln, sum(e[3] for e in picked),
                    any(e[7] for e in picked), sum(e[8] for e in picked)))
    return out or [(0, 0, 0, False, 0)]


@lru_cache(maxsize=512)
def hands(keys, energy):
    """Per sampled turn, the options of the hand drawn from this deck (keys = card_key per deck card)."""
    eff = [effect(k) for k in keys[:MAX_DECK]]
    # Stars (Regent): a star-cost card only fires as often as the deck makes stars. Income per turn = Divine
    # Right's 3 per fight spread over a fight + the Stars cards drawn per turn; its numbers scale by income/cost.
    income = STAR_BASE + HAND_SIZE * sum(dict(k[6]).get("Stars", 0) for k in keys) / max(len(keys), 1)
    for i, k in enumerate(keys[:MAX_DECK]):
        sc = GAMEDATA["cards"].get(k[0], {}).get("star_cost", 0)
        if sc > 0 and eff[i]:
            f = min(1, income / sc)
            eff[i] = (eff[i][0], eff[i][1] * f, eff[i][2] * f, eff[i][3] * f) + eff[i][4:]
    # Exhaust: plays once per fight, so per draw it's worth deck size / (cards drawn in a fight) of itself.
    # Retain: held until the right turn, a bit more reliable.
    for i, k in enumerate(keys[:MAX_DECK]):
        kw = card_keywords(k[0], k[1])
        f = (min(1, len(keys) / (HAND_SIZE * FIGHT_TURNS)) if "Exhaust" in kw and k[2] != "Power" else 1)             * (RETAIN_SIM if "Retain" in kw else 1)
        if f != 1 and eff[i]:
            eff[i] = (eff[i][0], eff[i][1] * f, eff[i][2] * f, eff[i][3] * f) + eff[i][4:]
    out = []
    for row in KEYS:
        order = heapq.nsmallest(HAND_SIZE + 5, range(len(eff)), key=row.__getitem__)
        hand = [eff[i] for i in order[:HAND_SIZE]]
        extra = min(5, sum(e[4] for e in hand if e))
        out.append(options(hand + [eff[i] for i in order[HAND_SIZE:HAND_SIZE + extra]], energy))
    return tuple(out)


def fight(opts, hp, incs, multi, perm_str, perm_dex):
    """(turns, HP lost, dmg per turn, block per turn, lam) for one encounter. Each hand plays the set maximising
    lam * damage - damage taken; lam (how much a point of damage is worth in HP not taken) is chosen from
    LAMBDAS to minimise the fight's total HP lost, so the deck isn't judged by a timid or reckless pilot."""
    results = []
    for lam in LAMBDAS:
        dealt = taken = blocked = 0.0
        for k, cand in enumerate(opts):
            inc = incs[k % len(incs)]
            best = None
            for single, aoe, block, weak, self_hp in cand:
                d = single + aoe * (AOE_MULT if multi else 1)
                hit = inc * (WEAK_MULT if weak else 1)
                t = max(0.0, hit - block) + self_hp
                score = lam * d - t
                if best is None or score > best[0]:
                    best = (score, d, t, min(block, hit))
            dealt += best[1]
            taken += best[2]
            blocked += best[3]
        k = len(opts)
        dpt = dealt / k + perm_str * POWER_UPTIME * 2  # ~2 attacks a turn gain the Strength
        tpt = max(0.0, taken / k - perm_dex * POWER_UPTIME * 1.5)  # ~1.5 block cards a turn gain the Dex
        turns = min(TURN_CAP, hp / max(dpt, 1))
        results.append((turns, turns * tpt * (1 + GROWTH * turns / 2), dpt, blocked / k, lam))
    # Several rates often play the same hands (same loss): the sim has no preference among them, so take the
    # middle one instead of the timid end of the grid.
    low = min(r[1] for r in results)
    tied = [r for r in results if r[1] <= low * 1.01 + 0.01]
    return tied[len(tied) // 2]

@lru_cache(maxsize=4096)
def _hp_lost(keys, act_name, asc, energy):
    opts = hands(keys, energy)
    eff = [effect(k) for k in keys]
    perm_str = sum(e[9] for e in eff if e)
    perm_dex = sum(e[10] for e in eff if e)
    return sum(w * fight(opts, hp, inc, multi, perm_str, perm_dex)[1]
               for w, hp, inc, multi in threats(act_name, asc))


def hp_lost(deck, state):
    """Expected HP lost per act boss/elite fight with this deck (weighted over the act's pools)."""
    return _hp_lost(tuple(card_key(c) for c in deck), state.get("act_name"), state.get("ascension", 0),
                    state.get("max_energy", 3))


def profile(state):
    """Overlay line: what the deck does per turn vs what the act's boss pool asks."""
    deck = state.get("deck", [])
    if not deck:
        return ""
    keys = tuple(card_key(c) for c in deck)
    asc, energy = state.get("ascension", 0), state.get("max_energy", 3)
    opts = hands(keys, energy)
    eff = [effect(k) for k in keys]
    perm = (sum(e[9] for e in eff if e), sum(e[10] for e in eff if e))
    bosses = [t for t in threats(state.get("act_name"), asc) if t[0] > 0][:3]
    hp = sum(t[1] for t in bosses) / max(len(bosses), 1)
    inc = sum(sum(t[2]) / len(t[2]) for t in bosses) / max(len(bosses), 1)
    turns, lost, dpt, bpt, _ = fight(opts, hp, bosses[0][2] if bosses else (inc,), any(t[3] for t in bosses), *perm)
    return (f"deck ~{dpt:.0f} dmg / {bpt:.0f} block a turn · boss ~{hp:.0f} HP hitting ~{inc:.0f}: "
            f"~{turns:.0f} turns, ~{lost:.0f} HP lost")
