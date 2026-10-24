"""Map route advice (advice only): try every path to the boss, simulate HP and gold along it, and score
what each room gives. Elites pay a relic and a rare card but cost HP; camps heal or upgrade; shops are
worth what your gold buys."""
import re
import statistics
import struct
from collections import namedtuple

from deck import hp_lost, known_boss
from relics import COMMUNITY, GAMEDATA, KNOWLEDGE
from runplan import character

# Tuning knobs. Values are in "card reward = 4" points.
# Fight cost: your real HP lost per fight in this act's encounter pool (knowledge.json, tools/calibrate.py),
# as (mean, 90th percentile). FIGHT_DMG is the fallback when the act or its fights aren't in your history.
FIGHT_DMG = {"Monster": (8, 12, 16), "Elite": (22, 30, 38), "Boss": (35, 45, 55)}  # expected HP lost, per act
FIGHT_GOLD = {"Monster": 15, "Elite": 40, "Unknown": 10}  # game: monster 10-20, elite 35-45 (Spire Codex)
CARD_REWARD, RELIC, EVENT, ANCIENT, UPGRADE = 4, 12, 5, 10, 6
# Card rewards (wiki Mechanics "Cards"): each of the 3 cards is rare 3% (normal fight) / 10% (elite), halved at
# A7+; reward cards come upgraded 0% / 25% / 50% in acts 1 / 2 / 3, halved at A7+. A rare in the reward is worth
# RARE_PREMIUM more; an upgraded pick about half an upgrade.
RARE_ODDS, UPGRADED_ODDS, RARE_PREMIUM = {"Monster": 0.03, "Elite": 0.10}, (0, 0.25, 0.5), 8
EVENT_RISK = 0.3        # share of ? rooms that turn out to be fights
SHOP_PER_100, SHOP_CAP = 6, 300  # value per 100 gold spent, most a shop visit spends
GOLD_LEFT = 0.02        # leftover gold at the boss still buys things next act
HP_VALUE = 0.3          # per HP walking into the boss, up to its bad case: more is healed after (Ancient) or unused
SAFETY, RISK = 1.5, 3   # entering a fight with less HP than its bad-case (p90) cost costs RISK per missing HP
DEATH = -1000
REST_BELOW, REST_WARY, LOOKAHEAD = 0.5, 0.7, 3  # rest under 50% HP, or under 70% with an elite/boss close
REST_MARGIN = 1.2       # ...or under 1.2x that elite/boss's bad-case cost, whichever is higher
HEAL_FRACTION = 0.3     # game's base rest heal (HealRestSiteOption.GetBaseHealAmount)
OVERHEAL = 0.5          # don't rest when more than half the heal would go past max HP: smith instead
MAX_PATHS = 20000
FIGHTS = ("Monster", "Elite", "Boss")
Route = namedtuple("Route", "coords types score hp gold rolled", defaults=(None,))  # rolled: what each "?" becomes
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
# No relic heal modifiers.
# B2/B3: the reader's pre-rolled rooms name the act's boss(es) and the elite order; each is priced on its own
# (your history, else the community's average for your character) instead of the act pool's mean.


def enc_cost(e, ch=None):
    """(mean, p90) HP lost in encounter e: your run history first, else Spire Codex's average for character ch
    (overall when that character has none), p90 ~ SAFETY x mean. None when unknown."""
    own = KNOWLEDGE["encounters"].get(e, {}).get("dmg")
    if own:
        return own["mean"], own["p90"]
    c = COMMUNITY.get("encounters", {}).get(e) or {}
    d = (c.get("by_char", {}).get(ch) or {}).get("dmg") or c.get("dmg")
    return (d, SAFETY * d) if d else None


M64 = (1 << 64) - 1
ROOM_ORDER = ["Unassigned", "Monster", "Elite", "Boss", "Treasure", "Shop", "Event", "RestSite", "Map"]  # RoomType


def f32(x):
    return struct.unpack("f", struct.pack("f", x))[0]


class UnknownRolls:
    """Replays the game's "?" room rolls (UnknownMapPointOdds.Roll) from the reader's `rooms.unknown`: its own
    xoshiro256** stream (MegaRandom) and the odds, in float32 like the game. That stream is used by nothing else, so
    the n-th "?" room on any path takes the n-th roll; only the Shop blacklist (after a shop / before only shops) and
    Juzu Bracelet change which types it can roll. None when unreadable or a relic/card overrides the rolls.
    ponytail: Golden Compass / Lantern Key (all-event "?" rooms) and the first-run tutorial aren't replayed."""

    def __init__(self, u, state):
        self.s = [int(x) for x in u["state"]]
        self.odds = [[k, f32(v)] for k, v in u["odds"]]
        self.base = [[k, f32(v)] for k, v in u["base_odds"]]
        self.juzu = any(r.get("name") == "JuzuBracelet" for r in state.get("relics", []))

    @staticmethod
    def make(state):
        u = (state.get("rooms") or {}).get("unknown")
        names = {r.get("name") for r in state.get("relics", [])} | {c.get("name") for c in state.get("deck", [])}
        if not u or len(u.get("state", [])) != 4 or names & {"GoldenCompass", "LanternKey"}:
            return None
        return UnknownRolls(u, state)

    def clone(self):
        c = object.__new__(UnknownRolls)
        c.s, c.odds, c.base, c.juzu = list(self.s), [list(x) for x in self.odds], self.base, self.juzu
        return c

    def _double(self):
        s0, s1, s2, s3 = self.s
        out = ((((s1 * 5) & M64) << 7 | ((s1 * 5) & M64) >> 57) & M64) * 9 & M64
        t = (s1 << 17) & M64
        s2 ^= s0
        s3 ^= s1
        s1 ^= s2
        s0 ^= s3
        s2 ^= t
        s3 = ((s3 << 45) | (s3 >> 19)) & M64
        self.s = [s0, s1, s2, s3]
        return (out >> 11) * 1.1102230246251565e-16

    def roll(self, blacklist=()):
        types = ({k for k, _ in self.odds} | {"Event"}) - set(blacklist) - ({"Monster"} if self.juzu else set())
        rolled = "Event" if "Event" in types else min(types, key=ROOM_ORDER.index)
        f, cum = f32(self._double()), 0.0
        for k, v in self.odds:
            if k in types and v >= 0:
                cum = f32(cum + v)
                if f <= cum:
                    rolled = k
                    break
        odds = dict(self.odds)
        for k, b in self.base:
            if k == rolled:
                odds[k] = b
            elif k in types:
                odds[k] = f32(odds[k] + b)
        self.odds = [[k, odds[k]] for k, _ in self.odds]
        return rolled


def resolve_unknowns(types, rolls, nexts=None, prev=None):
    """types along a path with each "Unknown" replaced by what it rolls (a copy of `rolls` is used). nexts[i]: the
    types of node i's children (all shops -> no shop roll); prev: the room we're in now (a shop -> no shop roll)."""
    if not rolls:
        return list(types)
    rolls, out = rolls.clone(), []
    for i, t in enumerate(types):
        if t == "Unknown":
            before = out[-1] if out else prev
            black = {"Shop"} if before == "Shop" or (nexts and nexts[i] and all(n == "Shop" for n in nexts[i])) else set()
            t = rolls.roll(black)
        out.append(t)
    return out


# The event queue skips events whose IsAllowed fails when you enter a "?" (RoomSet.EnsureNextEventIsValid; rules from
# each event's IsAllowed, same in v0.107.1 and v0.111.0). Judged on your state now: gold / HP when you get there may
# differ. Events already seen this run (reader `events_seen`) are skipped too.
# ponytail: relic / enchantment / pet / card-pool conditions count as allowed; co-op checks every player, this only you.
def _ev(s):
    p, deck = s.get("player") or {}, s.get("deck") or []
    floor = 99 if s.get("act", 0) else ((s.get("map") or {}).get("current") or [0, 0])[1] + 1
    return (s.get("act", 0), s.get("gold", 0), p.get("hp", 0), p.get("max_hp", 1), len(s.get("potions") or []),
            floor, deck, s)


EVENT_ALLOWED = {
    "Amalgamator": lambda a, g, hp, mx, pots, fl, deck, s: sum("Strike" in c["name"] for c in deck) >= 2
    and sum("Defend" in c["name"] for c in deck) >= 2,
    "BrainLeech": lambda a, *_: a < 2,
    "ColossalFlower": lambda a, g, hp, *_: hp >= 19,
    "CrystalSphere": lambda a, g, *_: g >= 100 and a > 0,
    "DollRoom": lambda a, *_: a == 1,
    "EndlessConveyor": lambda a, g, *_: g >= 120,
    "FakeMerchant": lambda a, g, hp, mx, pots, fl, deck, s: a >= 1 and (s.get("players") or s.get("party") or 1) == 1 and (
        g >= 100 or any(x.get("name") == "FoulPotion" for x in s.get("potions") or [])),
    "LuminousChoir": lambda a, g, *_: g >= 149,
    "MorphicGrove": lambda a, g, hp, mx, pots, fl, deck, s: g >= 100 and len(deck) >= 2,
    "PotionCourier": lambda a, *_: a > 0,
    "PunchOff": lambda a, g, hp, mx, pots, fl, *_: fl >= 6,
    "RanwidTheElder": lambda a, g, hp, mx, pots, *_: a > 0 and g >= 100 and pots > 0,
    "RelicTrader": lambda a, g, hp, mx, pots, fl, deck, s: a > 0 and len(s.get("relics") or []) >= 5,
    "RoomFullOfCheese": lambda a, *_: a < 2,
    "RoundTeaParty": lambda a, g, hp, *_: hp >= 12,
    "SlipperyBridge": lambda a, g, hp, mx, pots, fl, *_: fl > 6,
    "StoneOfAllTime": lambda a, g, hp, mx, pots, *_: a == 1 and pots > 0,
    "Symbiote": lambda a, *_: a > 0,
    "TeaMaster": lambda a, g, *_: a < 2 and g >= 150,
    "TheFutureOfPotions": lambda a, g, hp, mx, pots, *_: pots >= 2,
    "TheLegendsWereTrue": lambda a, g, hp, *_: a == 0 and hp >= 10,
    "TrashHeap": lambda a, g, hp, *_: hp > 5,
    "UnrestSite": lambda a, g, hp, mx, *_: hp <= 0.7 * mx,
    "WarHistorianRepy": lambda *_: False,
    "WaterloggedScriptorium": lambda a, g, *_: g >= 55,
    "WelcomeToWongos": lambda a, g, *_: a == 1 and g >= 100,
    "WhisperingHollow": lambda a, g, *_: g >= 44,
    "ZenWeaver": lambda a, g, *_: g >= 125,
}


def next_events(state, n):
    """The next n events "?" rooms would give, skipping the ones the game would skip now (EVENT_ALLOWED)."""
    r = state.get("rooms") or {}
    lst, k, out, args = r.get("events") or [], r.get("events_visited", 0), [], _ev(state)
    seen = set(r.get("events_seen") or [])
    ok = lambda e: EVENT_ALLOWED.get(e, lambda *_: True)(*args) and re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])", "_", e).upper() not in seen
    for _ in range(n if lst else 0):
        for _ in range(len(lst)):
            if ok(lst[k % len(lst)]):
                break
            k += 1
        out.append(lst[k % len(lst)])
        seen.add(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", lst[k % len(lst)]).upper())
        k += 1
    return out


def route_names(state, route):
    """[(room type, encounter / event name or None)] along a route: fights, elites and events are taken from the act's
    pre-rolled queues in order (a "?" that rolls a fight takes the next hallway fight too)."""
    r = state.get("rooms") or {}
    queue = {"Monster": ("fights", "fights_visited"), "Elite": ("elites", "elites_visited"),
             "Event": ("events", "events_visited")}
    seen, out = {}, []
    events = next_events(state, sum(t == "Event" for t in route.rolled or route.types))
    for t in route.rolled or route.types:
        name = None
        if t == "Event" and events:
            name = events.pop(0)
        elif t == "Boss":
            name = r.get("boss")
        elif t in queue and r.get(queue[t][0]):
            lst, k = r[queue[t][0]], r.get(queue[t][1], 0) + seen.get(t, 0)
            name, seen[t] = lst[k % len(lst)], seen.get(t, 0) + 1
        out.append((t, name))
    return out


def fight_costs(state):
    """{Monster|Elite|Boss: (mean, p90)} HP lost per fight in the current act."""
    act = min(state.get("act", 0), 2)
    out = {k: (v[act], SAFETY * v[act]) for k, v in FIGHT_DMG.items()}
    pool = GAMEDATA.get("acts", {}).get(state.get("act_name"), {}).get("encounters", [])
    ch = character(state)
    for cls in FIGHTS:
        seen = [c for e in set(pool) if GAMEDATA["encounters"].get(e, {}).get("class") == cls and (c := enc_cost(e, ch))]
        if seen:
            out[cls] = (statistics.mean(c[0] for c in seen), statistics.mean(c[1] for c in seen))
    boss = [enc_cost(b, ch) for b in known_boss(state)]
    if boss and all(boss):
        out["Boss"] = (sum(c[0] for c in boss), sum(c[1] for c in boss))  # A10: both final bosses, HP carries
    elif act == 2 and state.get("ascension", 0) >= DOUBLE_BOSS:
        out["Boss"] = tuple(2 * x for x in out["Boss"])
    rooms = state.get("rooms") or {}
    order = rooms.get("elites") or []
    if order:  # the elites still to come, in the game's order (it cycles the act's list)
        k = rooms.get("elites_visited", 0)
        out["Elites"] = [enc_cost(e, ch) or out["Elite"] for e in order[k % len(order):] + order[:k % len(order)]]
    f = deck_factor(state, out)
    return {k: [(m * f, p * f) for m, p in v] if k == "Elites" else (v[0] * f, v[1] * f) for k, v in out.items()}


def deck_factor(state, costs):
    """This deck's fight cost vs your recorded average (1 = typical; < 1 = stronger deck)."""
    if not state.get("deck") or not state.get("act_name"):  # the deck sim needs the act's enemy pool
        return 1
    ref = (costs["Elite"][0] + costs["Boss"][0]) / 2
    return min(DECK_CLAMP[1], max(DECK_CLAMP[0], hp_lost(state["deck"], state) / (SIM_BIAS * ref)))


def should_rest(hp, max_hp, ahead, costs=None):
    """Rest rule shared by the route search and rest-site advice. Returns the reason to rest, or None (smith)."""
    if max_hp - hp < OVERHEAL * HEAL_FRACTION * max_hp:
        return None  # most of the heal would be wasted
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
    score, elites = 0, 0
    gold_mult = 0.75 if asc >= POVERTY else 1
    for i, t in enumerate(types):
        saved = POTION_ELITE_HP * min(pots, POTIONS_COUNTED) if t in ("Elite", "Boss") else 0
        pots = 0 if saved else pots  # drunk at that fight
        cost = costs.get(t)
        if t == "Elite" and costs.get("Elites"):  # this route's n-th elite is the game's n-th in line
            cost, elites = costs["Elites"][elites % len(costs["Elites"])], elites + 1
        if t in FIGHTS:
            score -= RISK * max(0, cost[1] - saved - hp)
        if t == "Boss":
            break
        if t in ("Monster", "Elite"):
            hp -= max(0, cost[0] - saved / 2)
            gold += FIGHT_GOLD[t] * gold_mult
            score += reward_value(t, act, asc) + (RELIC if t == "Elite" else 0)
        elif t == "Unknown":
            hp -= EVENT_RISK * costs["Monster"][0]
            gold += FIGHT_GOLD[t] * gold_mult
            score += EVENT
        elif t == "Event":  # a "?" known to roll an event (UnknownRolls)
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
    return Route(None, types, score + HP_VALUE * min(hp, costs["Boss"][1]) + GOLD_LEFT * gold, round(hp), gold)


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
    rolls = UnknownRolls.make(state)  # B10: what each "?" on the path turns into
    prev = nodes[cur]["type"] if cur in nodes else None

    def walk(path, rolled, rs):
        if left[0] <= 0:
            return
        node = nodes[path[-1]]
        nxt, t = node["next"], node["type"]
        if t == "Unknown" and rs:  # rolled as the walk goes: shared prefixes roll once
            rs = rs.clone()
            before = rolled[-1] if rolled else prev
            shops = nxt and all(nodes[tuple(n)]["type"] == "Shop" for n in nxt)
            t = rs.roll({"Shop"} if before == "Shop" or shops else ())
        rolled = rolled + [t]
        if not nxt:
            left[0] -= 1
            types = [nodes[c]["type"] for c in path]
            r = simulate(rolled, *args)._replace(coords=path, types=types, rolled=rolled if rolls else None)
            if path[0] not in best or r.score > best[path[0]].score:
                best[path[0]] = r
        for c in nxt:
            walk(path + [tuple(c)], rolled, rs)

    for f in firsts:
        left[0] = MAX_PATHS // len(firsts)
        walk([f], [], rolls)
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
