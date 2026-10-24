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

from archetypes import expand_power
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
FALLBACK = [(1.0, 150, (8, 20, 14), False, 0, ())]  # (weight, HP, incoming per turn, several enemies, statuses, foes) without act data
# Status-adding enemies (Dazed, Slimed, Burn...): over a ~FIGHT_TURNS fight about this share of what they add sits in
# the piles at once (half the fight, and Ethereal/Exhaust ones leave once drawn). Bigger decks dilute them.
STATUS_SHARE = 0.25
STATUS_KEY = ("Dazed", False, "Status", None, -1, False, ())
# ponytail: enemy scaling over a fight, card draw beyond one level, exhaust/retain and orbs aren't simulated;
# the tier blend in picker.rate covers engines the numbers miss.


def card_key(c):
    c = expand_power(c)
    return (c["name"], c.get("upgraded", False), c["type"], c.get("target"), c["cost"], c.get("x_cost", False),
            tuple(sorted(c["vars"].items())))


# S4: rolled lineups (decomp GenerateMonsters picks with Rng.NextItem / NextBool) as (monster, expected count).
# ponytail: the expected mix, not the seeded roll (Rng(run seed + floor + XxHash64(id)): needs the run seed read).
_RAIDERS = ("AxeRubyRaider", "AssassinRubyRaider", "BruteRubyRaider", "CrossbowRubyRaider", "TrackerRubyRaider")
ROLLED = {"BowlbugsNormal": [("BowlbugRock", 1)] + [(m, 2 / 3) for m in ("BowlbugEgg", "BowlbugSilk", "BowlbugNectar")],
          "BowlbugsWeak": [("BowlbugRock", 1), ("BowlbugEgg", 0.5), ("BowlbugNectar", 0.5)],
          "FlyconidNormal": [("Flyconid", 1), ("LeafSlimeM", 0.5), ("TwigSlimeM", 0.5)],
          "RubyRaidersNormal": [(m, 0.6) for m in _RAIDERS],  # 3 different of the 5
          "SlimesNormal": [("TwigSlimeM", 1), ("LeafSlimeM", 1), ("LeafSlimeS", 1), ("TwigSlimeS", 1)],
          "SlimesWeak": [("LeafSlimeS", 1), ("TwigSlimeS", 1), ("LeafSlimeM", 0.5), ("TwigSlimeM", 0.5)],
          "SlitheringStranglerNormal": [("SlitheringStrangler", 1), ("SnappingJaxfruit", 1 / 3), ("LeafSlimeM", 1 / 6),
                                        ("TwigSlimeM", 1 / 6), ("LeafSlimeS", 1 / 3), ("TwigSlimeS", 1 / 3)]}


def lineup(name):
    """[(monster, expected count)] a fight starts with: repeats kept (Corpse Slugs x3), summons left out."""
    e = GAMEDATA["encounters"].get(name, {})
    if name in ROLLED:
        return ROLLED[name]
    return [(m, 1) for m in e.get("lineup") or e.get("monsters", [])]  # older gamedata: one of each kind


def encounter(name, asc):
    """(HP, incoming per turn, several enemies, statuses in the piles) of one encounter from the monster tables."""
    mons = lineup(name)
    hp, seqs, added = 0, [], 0.0
    for m, w in mons:
        ai = GAMEDATA["monsters"].get(m)
        if not ai:
            continue
        hp += w * sum(num(x, asc) or 0 for x in ai["hp"]) / max(len(ai["hp"]), 1)
        seqs.append([w * sum(d * h for d, h in attacks(mv, asc)) for mv in ai["moves"].values()] or [0])
        added += w * sum(num(e.get("count", 3), asc) or 0 for mv in ai["moves"].values() for e in mv["effects"]
                         if "adds_card" in e) / max(len(ai["moves"]), 1)  # per turn
    # per-turn incoming: turn k uses each monster's move k (mod its move count), so big hits come as spikes
    # ponytail: move order = table order, not the real cycle; good enough for spike sizes and frequency
    n = max((len(s) for s in seqs), default=1)
    # several enemies: more than one at the start, or a summoner (Phrog Parasite's Wrigglers: monsters it can add)
    multi = sum(w for _, w in mons) > 1 or bool(set(GAMEDATA["encounters"].get(name, {}).get("monsters", [])) - {m for m, _ in mons})
    return hp, tuple(sum(s[k % len(s)] for s in seqs) for k in range(n)), multi, round(added * FIGHT_TURNS * STATUS_SHARE)


def foe_list(name, asc, f=1.0):
    """((HP, attack per turn cycle), ...) per monster at the start (8b: each enemy's own HP; a dead one stops hitting).
    Rolled picks count by their expected share (HP and hits x the share)."""
    out = []
    for m, w in lineup(name):
        ai = GAMEDATA["monsters"].get(m)
        if ai:
            out.append((w * f * sum(num(x, asc) or 0 for x in ai["hp"]) / max(len(ai["hp"]), 1),
                        tuple(w * sum(d * h for d, h in attacks(mv, asc)) for mv in ai["moves"].values()) or (0,)))
    return tuple(out)


@lru_cache(maxsize=64)
def threats(act_name, asc, boss=(), foes=((), ()), mp=(1, 1)):
    """[(weight, HP, incoming, multi, statuses, foes)]: boss pool, elite pool, hallway pool (HALLWAY weight; bosses
    listed first); foes = foe_list (each monster's HP and attacks).
    boss: the act's known boss(es) (reader `rooms`, B2); the whole boss pool when unknown.
    foes: (next elites, next hallway fights) from the pre-rolled queues (B11); the act's pools when unknown.
    mp: co-op HP factor per player (fights, bosses), see mp_share."""
    act = GAMEDATA.get("acts", {}).get(act_name)
    if not act:
        return FALLBACK
    enc = GAMEDATA["encounters"]
    bosses = [e for e in act.get("bosses", []) if e in enc]
    bosses = [e for e in boss if e in bosses] or bosses
    elites = [e for e in act.get("encounters", []) if enc.get(e, {}).get("class") == "Elite"]
    hallway = [e for e in act.get("encounters", []) if enc.get(e, {}).get("class") == "Monster"]
    elites = [e for e in foes[0] if e in elites] or elites  # repeats weigh more: two Byrdonis ahead = twice the weight
    hallway = [e for e in foes[1] if e in hallway] or hallway
    out = []
    for pool, w, f in ((bosses, (1 - HALLWAY) / 2, mp[1]), (elites, (1 - HALLWAY) / 2, mp[0]), (hallway, HALLWAY, mp[0])):
        for e in pool:
            hp, incs, multi, statuses = encounter(e, asc)
            if hp:
                out.append((w / len(pool), hp * f, incs, multi, statuses, foe_list(e, asc, f)))
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
        _, incs, multi, _ = encounter(enc, state.get("ascension", 0))
    else:  # unknown fight: assume the enemies keep hitting like they intend to now
        incs = (sum((it.get("damage") or 0) * max(it.get("hits", 1), 1) for e in foes for it in e.get("intents", [])),)
        multi = len(foes) > 1
    hp = sum(e["creature"]["hp"] for e in foes)
    return _rate(tuple(card_key(c) for c in deck), round(hp, -1) or 1, incs, multi, state.get("max_energy", 3))


def effect(key):
    """(cost, dmg single, dmg aoe, block, draw, energy, vuln, weak, self hp loss, perm str, perm dex,
    this-turn str, this-turn dex, attack hits, has Block) or None."""
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
            v.get("HpLoss", 0), v.get("StrengthPower", 0) if power else 0, v.get("DexterityPower", 0) if power else 0,
            0 if power else v.get("StrengthPower", 0), 0 if power else v.get("DexterityPower", 0),
            max(v.get("Repeat", 1), 1) * times if ctype == "Attack" and dmg else 0, v.get("Block", 0) > 0)


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
        # One-turn Strength/Dexterity from a Skill/Attack (Coordinate, Anticipate...) lands on the OTHER attacks /
        # block cards drawn with it: worth something only when drawn together.
        ts, td = sum(e[11] for e in picked), sum(e[12] for e in picked)
        single = sum(e[1] + (ts - e[11]) * e[13] * (not e[2]) for e in picked)
        aoe = sum(e[2] + (ts - e[11]) * e[13] * bool(e[2]) for e in picked)
        block = sum(e[3] + (td - e[12]) * e[14] for e in picked)
        out.append((single * vuln, aoe * vuln, block, any(e[7] for e in picked), sum(e[8] for e in picked)))
    return out or [(0, 0, 0, False, 0)]


STAR_VARS = ("Stars", "StarNextTurnPower")  # Hidden Cache: 1 now + 3 next turn


def star_supply(keys):
    """(stars made per turn, stars star-cost cards want per turn): Divine Right's base + the Stars cards drawn, vs the
    star costs drawn (Stardust spends whatever is left: not counted as demand). S2: producers only pay off while
    demand is unmet, which the sim sees through the spenders' income scaling in hands()."""
    n = max(len(keys), 1)
    made = STAR_BASE + HAND_SIZE * sum(dict(k[6]).get(s, 0) for k in keys for s in STAR_VARS) / n
    wanted = HAND_SIZE * sum(GAMEDATA["cards"].get(k[0], {}).get("star_cost", 0) for k in keys) / n
    return made, wanted


COUNT_CARDS = {"Barrage", "CompileDriver", "Finisher", "LunarBlast", "Flechettes", "PullFromBelow", "Radiate",
               "Stardust", "FlakCannon", "TearAsunder", "HelixDrill", "FiendFire", "BodySlam", "AshenStrike",
               "MindBlast", "Stack", "PerfectedStrike", "CrescentSpear", "Murder", "GoldAxe"}  # deck_counts' keys


def deck_counts(keys, energy, income):
    """Typical value per count card (solver.solve.counted's counts) for this deck, mid-fight: Barrage's orbs, Finisher's
    Attacks before it, Body Slam's Block... The printed var is only the base (Barrage: 0 hits, Body Slam: 0 damage).
    ponytail: averages over the deck, not per hand; Osty / Souls / target-debuff counts stay at the base."""
    n = max(len(keys), 1)
    share = lambda f: HAND_SIZE * sum(1 for k in keys if f(k)) / n  # such cards per hand
    gd = lambda k: GAMEDATA["cards"].get(k[0], {})
    before = max(0, energy - 1)  # cards played before it in a turn
    channels = HAND_SIZE * sum(len(gd(k).get("channel", [])) for k in keys) / n
    slots = 3 * (channels > 0) + sum(dict(k[6]).get("SlotsGained", 0) for k in keys)
    orbs = min(slots, channels * FIGHT_TURNS / 4)  # orbs held mid-fight
    blocks = [dict(k[6]).get("Block", 0) for k in keys if k[2] != "Power" and dict(k[6]).get("Block", 0) > 0]
    blocked = min(before, HAND_SIZE * len(blocks) / n) * sum(blocks) / max(len(blocks), 1)  # Block cards played first
    attacks = share(lambda k: k[2] == "Attack")
    skills = share(lambda k: k[2] == "Skill")
    ethereal = share(lambda k: "Ethereal" in card_keywords(k[0], k[1]))
    exhaust = share(lambda k: "Exhaust" in card_keywords(k[0], k[1]))
    mid = FIGHT_TURNS / 2
    return {"Barrage": orbs, "CompileDriver": min(orbs, 3), "Finisher": min(before, attacks - 1),
            "LunarBlast": min(before, skills), "Flechettes": skills, "PullFromBelow": ethereal * mid,
            "Radiate": income, "Stardust": income, "FlakCannon": sum(k[2] == "Status" for k in keys),
            "TearAsunder": 1 + mid, "HelixDrill": before, "FiendFire": HAND_SIZE - 1 - before,
            "BodySlam": blocked, "AshenStrike": exhaust * mid,
            "MindBlast": n / 2, "Stack": n / 3, "PerfectedStrike": sum("Strike" in k[0] for k in keys),
            "CrescentSpear": sum(gd(k).get("star_cost", 0) > 0 for k in keys),
            "Murder": HAND_SIZE * mid, "GoldAxe": HAND_SIZE * mid * 0.6}


@lru_cache(maxsize=512)
def hands(keys, energy):
    """Per sampled turn, the options of the hand drawn from this deck (keys = card_key per deck card)."""
    eff = [effect(k) for k in keys[:MAX_DECK]]
    # Stars (Regent): a star-cost card only fires as often as the deck makes stars. Income per turn = Divine
    # Right's 3 per fight spread over a fight + the Stars cards drawn per turn; its numbers scale by income/cost.
    income = star_supply(keys)[0]
    # Count cards: their typical count in this deck (Barrage x orbs held, Perfected Strike + per Strike...)
    counts = deck_counts(keys, energy, income)
    for i, k in enumerate(keys[:MAX_DECK]):
        if k[0] in counts and eff[i]:
            v = dict(k[6])
            x = v.get("CalculationBase", 0) + v.get("ExtraDamage", v.get("CalculationExtra", 1)) * counts[k[0]]
            if "ExtraDamage" in v or "Damage" in v:  # damage, or hits of Damage
                d = max(0.0, x if "ExtraDamage" in v else v["Damage"] * x)
                aoe = bool(eff[i][2]) or k[3] in ("AllEnemies", "RandomEnemy")
                eff[i] = eff[i][:1] + ((0, d) if aoe else (d, 0)) + eff[i][3:13] + (max(x, 0) if "Damage" in v else 1,) + eff[i][14:]
            else:
                eff[i] = eff[i][:3] + (max(0.0, x),) + eff[i][4:]
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


def fight(opts, hp, incs, multi, perm_str, perm_dex, foes=()):
    """(turns, HP lost, dmg per turn, block per turn, lam) for one encounter. Each hand plays the set maximising
    lam * damage - damage taken; lam (how much a point of damage is worth in HP not taken) is chosen from
    LAMBDAS to minimise the fight's total HP lost, so the deck isn't judged by a timid or reckless pilot.
    foes (several monsters): fought turn by turn, see _fight_foes."""
    if len(foes) > 1:
        return _fight_foes(opts, foes, perm_str, perm_dex)
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

def _fight_foes(opts, foes, perm_str, perm_dex):
    """8b (user): several enemies, each with its own HP and attack cycle. Turn by turn: single-target damage goes to
    the lowest-HP enemy alive (killing it first stops its hits soonest), AoE hits every one alive, and only living
    enemies attack. ponytail: summons and enemy Block / buffs aren't simulated; lowest HP first, not best ratio."""
    results = []
    for lam in LAMBDAS:
        hps = [h for h, _ in foes]
        dealt = taken = blocked = 0.0
        k = 0
        while any(h > 0 for h in hps) and k < TURN_CAP:
            alive = [i for i, h in enumerate(hps) if h > 0]
            inc = sum(foes[i][1][k % len(foes[i][1])] for i in alive) * (1 + GROWTH * k)
            best = None
            for single, aoe, block, weak, self_hp in opts[k % len(opts)]:
                single += perm_str * POWER_UPTIME * 2  # ~2 attacks a turn gain the Strength
                hit = inc * (WEAK_MULT if weak else 1)
                t = max(0.0, hit - block - perm_dex * POWER_UPTIME * 1.5) + self_hp
                d = single + aoe * len(alive)
                if best is None or lam * d - t > best[0]:
                    best = (lam * d - t, single, aoe, t, min(block, hit))
            _, single, aoe, t, b = best
            for i in alive:
                hps[i] -= aoe
            for i in sorted((i for i in alive if hps[i] > 0), key=lambda i: hps[i]):
                spent = min(single, hps[i])
                hps[i] -= spent
                single -= spent
                if single <= 0:
                    break
            dealt += best[1] + best[2] * len(alive)
            taken += t
            blocked += b
            k += 1
        results.append((k, taken, dealt / max(k, 1), blocked / max(k, 1), lam))
    low = min(r[1] for r in results)
    tied = [r for r in results if r[1] <= low * 1.01 + 0.01]
    return tied[len(tied) // 2]


@lru_cache(maxsize=4096)
def _hp_lost(keys, act_name, asc, energy, boss=(), foes=((), ()), mp=(1, 1)):
    opts = hands(keys, energy)
    eff = [effect(k) for k in keys]
    perm_str = sum(e[9] for e in eff if e)
    perm_dex = sum(e[10] for e in eff if e)
    return sum(w * fight(hands(keys + (STATUS_KEY,) * st, energy) if st else opts, hp, inc, multi, perm_str, perm_dex,
                         fl)[1] for w, hp, inc, multi, st, fl in threats(act_name, asc, boss, foes, mp))


def hp_lost(deck, state):
    """Expected HP lost per act boss/elite fight with this deck (weighted over the act's pools)."""
    return _hp_lost(tuple(card_key(c) for c in deck), state.get("act_name"), state.get("ascension", 0),
                    state.get("max_energy", 3), known_boss(state), known_foes(state), mp_share(state))


def mp_share(state):
    """(fights, bosses) enemy HP factor each deck faces in co-op: the game gives enemies players x 1.1 / 1.2 / 1.2
    HP by act, 1.3 for the act 3 boss (Creature.ScaleHpForMultiplayer, on top of ascension HP); split over the party,
    each deck faces that factor. (1, 1) solo. ponytail: assumes teammates' decks deal about what ours does."""
    if ((state or {}).get("players") or 1) <= 1:
        return (1, 1)
    f = (1.1, 1.2, 1.2)[min(state.get("act") or 0, 2)]
    return (f, 1.3 if (state.get("act") or 0) >= 2 else f)


def known_boss(state):
    """The current act's boss(es) from the reader's pre-rolled rooms (A10: both final bosses), else ()."""
    r = (state or {}).get("rooms") or {}
    return tuple(b for b in (r.get("boss"), r.get("second_boss")) if b)


# B11: how far ahead the pre-rolled queues count: about the rest of an act's elites / hallway fights.
ELITES_AHEAD, FIGHTS_AHEAD = 3, 5


def known_foes(state):
    """(next elites, next hallway fights) from the reader's pre-rolled queues (in order, wrapping like the game's
    room lists), else ((), ()). Card, shop and relic ratings then weigh the fights actually coming."""
    r = (state or {}).get("rooms") or {}
    nxt = lambda q, k, n: tuple(r[q][(r.get(k, 0) + i) % len(r[q])] for i in range(n)) if r.get(q) else ()
    return nxt("elites", "elites_visited", ELITES_AHEAD), nxt("fights", "fights_visited", FIGHTS_AHEAD)


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
    bosses = [t for t in threats(state.get("act_name"), asc, known_boss(state), mp=mp_share(state)) if t[0] > 0][:3]
    hp = sum(t[1] for t in bosses) / max(len(bosses), 1)
    inc = sum(sum(t[2]) / len(t[2]) for t in bosses) / max(len(bosses), 1)
    turns, lost, dpt, bpt, _ = fight(opts, hp, bosses[0][2] if bosses else (inc,), any(t[3] for t in bosses), *perm)
    return (f"deck {len(deck)} cards ~{dpt:.0f} dmg / {bpt:.0f} block a turn · boss ~{hp:.0f} HP hitting ~{inc:.0f}: "
            f"~{turns:.0f} turns, ~{lost:.0f} HP lost · a dead card costs ~{slot_cost(deck, state):.1f} HP/fight")


def slot_cost(deck, state):
    """HP per act fight that one more card doing nothing costs this deck (it pushes good cards out of hands)."""
    return hp_lost(list(deck) + [DEAD], state) - hp_lost(deck, state)


DEAD = {"name": "Dead", "type": "Status", "target": None, "cost": -1, "vars": {}}
