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
TURN_CAP = 30
FALLBACK = [(1.0, 150, 14, False)]  # (weight, HP, incoming per turn, several enemies) when act data is missing
# ponytail: enemy scaling over a fight, card draw beyond one level, exhaust/retain and orbs aren't simulated;
# the tier blend in picker.rate covers engines the numbers miss.


def card_key(c):
    return (c["name"], c.get("upgraded", False), c["type"], c.get("target"), c["cost"], c.get("x_cost", False),
            tuple(sorted(c["vars"].items())))


def encounter(name, asc):
    """(HP, incoming per turn, several enemies) of one encounter from the monster tables."""
    mons = GAMEDATA["encounters"].get(name, {}).get("monsters", [])
    hp = inc = 0
    for m in mons:
        ai = GAMEDATA["monsters"].get(m)
        if not ai:
            continue
        hp += sum(num(x, asc) or 0 for x in ai["hp"]) / max(len(ai["hp"]), 1)
        moves = list(ai["moves"].values())
        inc += sum(sum(d * h for d, h in attacks(mv, asc)) for mv in moves) / max(len(moves), 1)
    return hp, inc, len(mons) > 1


@lru_cache(maxsize=64)
def threats(act_name, asc):
    """[(weight, HP, incoming, multi)]: the act's boss pool (half) and elite pool (half)."""
    act = GAMEDATA.get("acts", {}).get(act_name)
    if not act:
        return FALLBACK
    enc = GAMEDATA["encounters"]
    bosses = [e for e in act.get("bosses", []) if e in enc]
    elites = [e for e in act.get("encounters", []) if enc.get(e, {}).get("class") == "Elite"]
    out = []
    for pool in (bosses, elites):
        for e in pool:
            hp, inc, multi = encounter(e, asc)
            if hp:
                out.append((0.5 / len(pool), hp, inc, multi))
    return out or FALLBACK


def effect(key):
    """(cost, dmg single, dmg aoe, block, draw, energy, vuln, weak, self hp loss, perm str, perm dex) or None."""
    name, _, ctype, target, cost, x, v = key
    v = dict(v)
    if ctype in ("Curse", "Status") or (cost < 0 and not x):
        return None  # takes a hand slot, does nothing
    times = 2 if x else 1  # ponytail: X costs treated as 2 energy for 2x effect
    cost = 2 if x else cost
    dmg = v.get("Damage", 0) * max(v.get("Repeat", 1), 1) * times if ctype == "Attack" else 0
    aoe = target in ("AllEnemies", "RandomEnemy")
    power = ctype == "Power"
    return (cost, 0 if aoe else dmg, dmg if aoe else 0, v.get("Block", 0) * times, int(v.get("Cards", 0)),
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
    out = []
    for row in KEYS:
        order = heapq.nsmallest(HAND_SIZE + 5, range(len(eff)), key=row.__getitem__)
        hand = [eff[i] for i in order[:HAND_SIZE]]
        extra = min(5, sum(e[4] for e in hand if e))
        out.append(options(hand + [eff[i] for i in order[HAND_SIZE:HAND_SIZE + extra]], energy))
    return tuple(out)


def fight(opts, hp, inc, multi, perm_str, perm_dex):
    """(turns, HP lost, dmg per turn, block per turn) for one encounter. The hand's play maximises
    lam * damage - damage taken, lam = taken per turn / dealt per turn (the fight's exchange rate)."""
    lam, res = 1.0, None
    for _ in range(3):
        dealt = taken = blocked = 0.0
        for cand in opts:
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
        res = (turns, turns * tpt, dpt, blocked / k)
        lam = max(0.2, tpt / max(dpt, 1))
    return res


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
    inc = sum(t[2] for t in bosses) / max(len(bosses), 1)
    turns, lost, dpt, bpt = fight(opts, hp, inc, any(t[3] for t in bosses), *perm)
    return (f"deck ~{dpt:.0f} dmg / {bpt:.0f} block a turn · boss ~{hp:.0f} HP hitting ~{inc:.0f}: "
            f"~{turns:.0f} turns, ~{lost:.0f} HP lost")
