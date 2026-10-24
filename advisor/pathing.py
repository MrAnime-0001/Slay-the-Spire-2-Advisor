"""Map route advice (advice only): try every path to the boss, simulate HP and gold along it, and score
what each room gives. Elites pay a relic and a rare card but cost HP; camps heal or upgrade; shops are
worth what your gold buys."""
import statistics
from collections import namedtuple

from deck import hp_lost
from relics import GAMEDATA, KNOWLEDGE

# Tuning knobs. Values are in "card reward = 4" points.
# Fight cost: your real HP lost per fight in this act's encounter pool (knowledge.json, tools/calibrate.py),
# as (mean, 90th percentile). FIGHT_DMG is the fallback when the act or its fights aren't in your history.
FIGHT_DMG = {"Monster": (8, 12, 16), "Elite": (22, 30, 38), "Boss": (35, 45, 55)}  # expected HP lost, per act
FIGHT_GOLD = {"Monster": 15, "Elite": 30, "Unknown": 10}
CARD_REWARD, RELIC, EVENT, ANCIENT, UPGRADE = 4, 12, 5, 10, 6
# Card rewards (wiki Mechanics "Cards"): each of the 3 cards is rare 3% (normal fight) / 10% (elite), halved at
# A7+; reward cards come upgraded 0% / 25% / 50% in acts 1 / 2 / 3, halved at A7+. A rare in the reward is worth
# RARE_PREMIUM more; an upgraded pick about half an upgrade.
RARE_ODDS, UPGRADED_ODDS, RARE_PREMIUM = {"Monster": 0.03, "Elite": 0.10}, (0, 0.25, 0.5), 8
EVENT_RISK = 0.3        # share of ? rooms that turn out to be fights
SHOP_PER_100, SHOP_CAP = 6, 300  # value per 100 gold spent, most a shop visit spends
GOLD_LEFT = 0.02        # leftover gold at the boss still buys things next act
HP_VALUE = 0.3          # per HP walking into the boss
SAFETY, RISK = 1.5, 3   # entering a fight with less HP than its bad-case (p90) cost costs RISK per missing HP
DEATH = -1000
REST_BELOW, REST_WARY, LOOKAHEAD = 0.5, 0.7, 3  # rest under 50% HP, or under 70% with an elite/boss close
REST_MARGIN = 1.2       # ...or under 1.2x that elite/boss's bad-case cost, whichever is higher
HEAL_FRACTION = 0.3     # game's base rest heal (HealRestSiteOption.GetBaseHealAmount)
MAX_PATHS = 20000
FIGHTS = ("Monster", "Elite", "Boss")
Route = namedtuple("Route", "coords types score hp gold")
# Ascension (code-facts.md): A3 Poverty = fight gold x0.75, A7 Scarcity = rare odds halved (elite rare card worth
# half), A10 DoubleBoss = two bosses at the end of act 3 (boss cost x2).
POVERTY, SCARCITY, DOUBLE_BOSS = 3, 7, 10
# v19 Phase D: fight costs scale with this deck. deck.hp_lost (deck sim) runs ~2.4x the recorded fight cost (median
# over 104 recorded decks, 2026-10-22) but tracks deck strength, so it's used relative to that, clamped.
# Each usable potion (max 2) takes POTION_ELITE_HP off the next Elite/Boss's bad case (spent there).
SIM_BIAS, DECK_CLAMP = 2.4, (0.6, 1.5)
POTION_ELITE_HP, POTIONS_COUNTED = 8, 2
# ponytail: one bias for every act (Overgrowth runs ~2.6x, Underdocks ~1.1-1.8x); per-act bias if routes look off.
# ponytail: your history mixes ascensions (A0-A8) and characters; enemies only get stronger at A8/A9.
# No relic heal modifiers, no second boss.


def fight_costs(state):
    """{Monster|Elite|Boss: (mean, p90)} HP lost per fight in the current act."""
    act = min(state.get("act", 0), 2)
    out = {k: (v[act], SAFETY * v[act]) for k, v in FIGHT_DMG.items()}
    pool = GAMEDATA.get("acts", {}).get(state.get("act_name"), {}).get("encounters", [])
    for cls in FIGHTS:
        seen = [KNOWLEDGE["encounters"][e]["dmg"] for e in set(pool)
                if GAMEDATA["encounters"].get(e, {}).get("class") == cls and "dmg" in KNOWLEDGE["encounters"].get(e, {})]
        if seen:
            out[cls] = (statistics.mean(d["mean"] for d in seen), statistics.mean(d["p90"] for d in seen))
    if act == 2 and state.get("ascension", 0) >= DOUBLE_BOSS:
        out["Boss"] = tuple(2 * x for x in out["Boss"])
    f = deck_factor(state, out)
    return {k: (m * f, p90 * f) for k, (m, p90) in out.items()}


def deck_factor(state, costs):
    """This deck's fight cost vs your recorded average (1 = typical; < 1 = stronger deck)."""
    if not state.get("deck") or not state.get("act_name"):  # the deck sim needs the act's enemy pool
        return 1
    ref = (costs["Elite"][0] + costs["Boss"][0]) / 2
    return min(DECK_CLAMP[1], max(DECK_CLAMP[0], hp_lost(state["deck"], state) / (SIM_BIAS * ref)))


def should_rest(hp, max_hp, ahead, costs=None):
    """Rest rule shared by the route search and rest-site advice. Returns the reason to rest, or None (smith)."""
    if hp < REST_BELOW * max_hp:
        return "HP under half"
    near = next((i for i, t in enumerate(ahead[:LOOKAHEAD], 1) if t in ("Elite", "Boss")), None)
    if near:
        kind = ahead[near - 1]
        wary = max(REST_WARY * max_hp, REST_MARGIN * costs[kind][1] if costs else 0)
        if hp < wary:
            return f"{kind.lower()} in {near}"
    return None


def reward_value(kind, act, asc):
    """A card reward after a Monster / Elite fight, on the route scale."""
    half = 0.5 if asc >= SCARCITY else 1
    rare = 1 - (1 - RARE_ODDS[kind] * half) ** 3
    return CARD_REWARD + RARE_PREMIUM * rare + UPGRADED_ODDS[min(act, 2)] * half * UPGRADE / 2


def simulate(types, hp, max_hp, gold, costs, asc=0, pots=0, act=0):
    score = 0
    gold_mult = 0.75 if asc >= POVERTY else 1
    for i, t in enumerate(types):
        saved = POTION_ELITE_HP * min(pots, POTIONS_COUNTED) if t in ("Elite", "Boss") else 0
        pots = 0 if saved else pots  # drunk at that fight
        if t in FIGHTS:
            score -= RISK * max(0, costs[t][1] - saved - hp)
        if t == "Boss":
            break
        if t in ("Monster", "Elite"):
            hp -= max(0, costs[t][0] - saved / 2)
            gold += FIGHT_GOLD[t] * gold_mult
            score += reward_value(t, act, asc) + (RELIC if t == "Elite" else 0)
        elif t == "Unknown":
            hp -= EVENT_RISK * costs["Monster"][0]
            gold += FIGHT_GOLD[t] * gold_mult
            score += EVENT
        elif t == "Treasure":
            score += RELIC
        elif t == "Ancient":
            score += ANCIENT
        elif t == "Shop":
            spent = min(gold, SHOP_CAP)
            score += SHOP_PER_100 * spent / 100
            gold -= spent
        elif t == "RestSite":
            if should_rest(hp, max_hp, types[i + 1:], costs):
                hp = min(max_hp, hp + int(HEAL_FRACTION * max_hp))
            else:
                score += UPGRADE
        if hp <= 0:
            return Route(None, types, DEATH, 0, gold)
    return Route(None, types, score + HP_VALUE * hp + GOLD_LEFT * gold, round(hp), gold)


def best_routes(state, top=3):
    """Best route per next-step choice, best first. Empty when there is no map or nowhere left to go."""
    m = state.get("map")
    if not m:
        return []
    nodes = {(n["c"], n["r"]): n for n in m["nodes"]}
    cur = tuple(m["current"]) if m["current"] else None
    firsts = [tuple(c) for c in nodes[cur]["next"]] if cur in nodes else [tuple(m["start"])]
    p = state["player"]
    args = (p["hp"], p["max_hp"], state.get("gold", 0), fight_costs(state), state.get("ascension", 0),
            len(state.get("potions", [])), state.get("act", 0))
    best, left = {}, [0]

    def walk(path):
        if left[0] <= 0:
            return
        nxt = nodes[path[-1]]["next"]
        if not nxt:
            left[0] -= 1
            r = simulate([nodes[c]["type"] for c in path], *args)._replace(coords=path)
            if path[0] not in best or r.score > best[path[0]].score:
                best[path[0]] = r
        for c in nxt:
            walk(path + [tuple(c)])

    for f in firsts:
        left[0] = MAX_PATHS // len(firsts)
        walk([f])
    return sorted(best.values(), key=lambda r: -r.score)[:top]


def direction(state, coord):
    """left / middle / right of the next step, among the current node's children."""
    m = state["map"]
    if not m["current"]:
        return ""
    kids = sorted(next(n["next"] for n in m["nodes"] if [n["c"], n["r"]] == m["current"]))
    names = {1: ["only path"], 2: ["left", "right"], 3: ["left", "middle", "right"]}.get(len(kids))
    i = kids.index(list(coord))
    word = names[i] if names else f"{i + 1}{'st' if i == 0 else 'nd' if i == 1 else 'rd' if i == 2 else 'th'} from left"
    dots = " ".join("●" if k == i else "○" for k in range(len(kids)))  # the paths as on screen, ours filled
    return word if len(kids) == 1 else f"{word}  {dots}"
