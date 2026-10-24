"""Full-run autopilot (F8): fights, loot, card picks, map, events, rest sites, shops, chests, deck grids,
and with auto-retry on (F9) a new run after death.

Every click is hover-verified like the combat sweeps: move the mouse over the screen's likely area, read the
reader's ui.hover ("reward:gold:45", "map:3,4", "event:<key>", "card:Bash", ...) and click only when it
names the wanted button. Every hovered button's position is remembered, so later clicks go straight there.
Keys where the game binds one: E = proceed/confirm/dialogue, Enter = open chest/shop/game-over, Esc = skip
card reward / close the shop. Decisions come from the same advisors the overlay shows."""
import time

import autoplay as ap
from autoplay import SCAN_E, SCAN_ESC, click, find_game, tap, user32, wait_until
from pathing import best_routes
from picker import (SKIP_BELOW, UPGRADES, choose_card, event_choice, hand_pick, pick, potion_value, rate,
                    relic_value, removal_target, rest_choice, shop_basket, upgrade_gain)
from relics import GAMEDATA
from solver import solve

SCAN_ENTER = 0x1C
MAX_RETRIES = 3     # failed actions in a row before stopping, with auto-retry on
STUCK_AFTER = 30    # seconds on an unchanged screen with nothing to do -> error
WAIT = "wait"       # handler result: nothing to do yet (animations, enemy turn, loading)


def grid(xs, ys):
    return [(x / 100, y / 100) for y in ys for x in xs]


# Sweep areas per screen in client fractions. ponytail: guessed from 1920x1080 screenshots; a miss is logged
# to autopilot_debug.log with every hover seen, widen the area there.
AREAS = {
    "reward": grid((50, 42, 58), range(22, 82, 3)),
    "card": grid(range(16, 85, 4), (45, 38, 52, 60, 30)),
    "grid": grid(range(14, 87, 5), range(18, 88, 6)),
    "map": grid(range(26, 75, 2), range(84, 12, -4)),
    "event": grid((70, 60, 80, 50, 88, 40), range(34, 94, 4)),
    "rest": grid(range(18, 83, 5), range(28, 76, 5)),
    "shop": grid(range(10, 91, 4), range(12, 92, 6)),
    "chest": grid(range(30, 71, 4), range(22, 72, 5)),
    "menu": grid(range(4, 97, 4), range(16, 97, 4)),
}
spots = {}  # hover descriptor -> (fx, fy) where it was last seen
last = {}   # area -> (fx, fy) of its last hit: sweeps start near it (map rows sit at the same height)


def sig(s):
    """What a click or key should change (hover alone doesn't count)."""
    ui = s.get("ui") or {}
    return (s.get("status"), ui.get("screen"), tuple(ui.get("targets") or ()), s.get("gold"), len(s.get("deck") or ()),
            len(s.get("potions") or ()), len(s.get("relics") or ()), str((s.get("map") or {}).get("current")),
            str(s.get("event")), str(s.get("rest_site")), str(s.get("card_grid")), str(s.get("choose_card")))


def press(want, area, get_state, cancel, log, what):
    """Sweep area (known spots first) until ui.hover satisfies want(hover), click, and check something changed."""
    game = find_game()
    if not game:
        return "game window not found"
    hwnd, left, top, w, h = game
    user32.SetForegroundWindow(hwnd)
    at = lambda fx, fy: user32.SetCursorPos(int(left + w * fx), int(top + h * fy))
    pts = AREAS[area]
    if area in last:
        lx, ly = last[area]
        pts = sorted(pts, key=lambda p: (p[0] - lx) ** 2 + 4 * (p[1] - ly) ** 2)
    seen = []
    for fx, fy in [p for d, p in spots.items() if want(d)] + pts:
        if cancel.is_set():
            return "cancelled"
        at(fx, fy)
        time.sleep(ap.STEP_DELAY)
        hov = (get_state().get("ui") or {}).get("hover")
        if hov:
            spots[hov] = (fx, fy)
        if hov and want(hov):
            time.sleep(ap.STEP_DELAY)  # re-read: a lagging reader could report the previous point's button
            if (get_state().get("ui") or {}).get("hover") != hov:
                continue
            last[area] = (fx, fy)
            before = sig(get_state())
            click()
            log(f"clicked {hov}")
            if not wait_until(lambda: sig(get_state()) != before, 3, cancel):
                return f"clicking {hov} did nothing"
            time.sleep(0.5)
            return None
        seen.append(f"({fx:.2f},{fy:.2f}) {hov}")
    ap.debug(what, seen, game, get_state())
    return f"couldn't find {what}"


def key(scan, get_state, cancel, log, what, check=True):
    """Tap a key; with check, error unless the state changed (some keys change nothing the reader sees)."""
    game = find_game()
    if not game:
        return "game window not found"
    user32.SetForegroundWindow(game[0])
    before = sig(get_state())
    tap(scan)
    log(what)
    if not check:
        time.sleep(1.0)
        return None
    if not wait_until(lambda: sig(get_state()) != before, 3, cancel):
        return f"{what}: nothing happened"
    time.sleep(0.5)
    return None


def is_card(name):
    return lambda d: d.startswith("card:") and d[5:].rstrip("+") == name


def act(s, get_state, cancel, log, mem, retry):
    """Do one thing on the current screen. Returns None (did something), WAIT, or an error."""
    a = (get_state, cancel, log)
    ui = s.get("ui") or {}
    screen, targets = ui.get("screen") or "", ui.get("targets") or []
    status = s.get("status")
    for k, v in (("skipped", False), ("shop_open", False), ("shop_done", False), ("no_buy", set()),
                 ("picked", []), ("missing", set())):
        mem.setdefault(k, v)
    if screen not in ("NRewardsScreen", "NCardRewardSelectionScreen"):
        mem["skipped"] = False  # a skipped card reward stays listed: don't reopen it on this screen
    if screen != "MerchantRoom":
        mem.update(shop_open=False, shop_done=False, no_buy=set())
    if not s.get("card_grid"):
        mem.update(picked=[], missing=set())

    if status == "game_over":
        if not retry():
            return "you died (auto-retry off)"
        return key(SCAN_ENTER, *a, "game over: continue") if screen == "NGameOverScreen" else WAIT
    if status == "no_run":
        return new_run(s, mem, a) if retry() else "no run in progress (auto-retry off starts none)"
    if status not in ("out_of_combat", "combat"):
        return WAIT
    if s.get("choose_card"):
        return choose(s, a)
    if s.get("card_grid"):
        return grid_pick(s, mem, a)
    if s.get("card_reward"):
        best, _ = pick(s["card_reward"], s["deck"], s.get("relics", []), s)
        if best == "Skip":
            mem["skipped"] = True
            return key(SCAN_ESC, *a, "card reward: skip")
        return press(is_card(best), "card", *a, f"card reward {best}")
    if screen == "NRewardsScreen":
        return loot(s, targets, mem, a)
    if (s.get("rest_site") or {}).get("chosen") is False:
        choice, card = rest_choice(s)
        rest = [t for t in targets if t.startswith("rest:")]
        want = f"rest:{choice}" if f"rest:{choice}" in rest or not rest else rest[0]
        log(f"rest site: {choice}" + (f" ({card})" if card else ""))
        return press(lambda d: d == want, "rest", *a, want)
    if s.get("event") and not s.get("map_open"):
        opt = event_choice(s)
        if not opt or not any(t.startswith("event:") for t in targets):
            return key(SCAN_E, *a, "event: advance dialogue", check=False)
        return press(lambda d: d == f"event:{opt['key']}", "event", *a, f"event option {opt['key']}")
    if screen == "MerchantRoom" and s.get("shop") and not s.get("map_open"):
        return shop(s, mem, a)
    if screen == "TreasureRoom":
        chest = [t for t in targets if t.startswith("chest:") and relic_value(t[6:], s) >= 0]
        if chest:
            return press(lambda d: d == chest[0], "chest", *a, chest[0])
        if "treasure" in targets:
            return key(SCAN_ENTER, *a, "open chest")
    if s.get("map_open"):
        routes = best_routes(s, top=1)
        want = "map:%d,%d" % tuple(routes[0].coords[0]) if routes else None
        if want and want not in targets and any(t.startswith("map:") for t in targets):
            want = None  # route model and map disagree: any travelable point beats stopping
        log(f"map: {want or 'any travelable point'}")
        return press(lambda d: d == want if want else d.startswith("map:"), "map", *a, want or "a map point")
    if "proceed" in targets and status == "out_of_combat":
        return key(SCAN_E, *a, "proceed")
    return WAIT


def loot(s, targets, mem, a):
    """Claim rewards in order gold, relic, potion, specific card, card reward, removal; then proceed."""
    free = s.get("potion_slots", 3) - len(s.get("potions", []))
    deck, relics = s.get("deck", []), s.get("relics", [])

    def wanted(t):
        kind, _, name = t.partition(":")[2].partition(":")
        if kind == "relic":
            return relic_value(name, s) >= 0
        if kind == "potion":
            return free > 0 and potion_value(name, s) >= 2
        if kind == "special":
            return rate({"name": name, **card_info(name)}, deck, relics, s)[0] >= SKIP_BELOW
        return kind in ("gold", "remove") or (kind == "card" and not mem["skipped"])

    order = ("gold", "relic", "potion", "special", "card", "remove")
    todo = sorted((t for t in targets if t.startswith("reward:") and wanted(t)),
                  key=lambda t: order.index(t.split(":")[1]) if t.split(":")[1] in order else 9)
    if todo:
        return press(lambda d: d == todo[0], "reward", *a, todo[0])
    return key(SCAN_E, *a, "rewards: proceed")


def card_info(name):
    c = GAMEDATA["cards"].get(name, {})
    return {"type": c.get("type", "Skill"), "target": c.get("target", "Self"), "cost": c.get("cost", 1),
            "vars": c.get("vars", {}), "upgraded": False, "rarity": c.get("rarity"), "x_cost": False}


def choose(s, a):
    best, _ = choose_card(s)
    return press(is_card(best), "card", *a, f"choose {best}")


def grid_pick(s, mem, a):
    """Deck grid (upgrade / remove / transform / enchant / pick): click the best card(s), then confirm."""
    g, deck, relics = s["card_grid"], s.get("deck", []), s.get("relics", [])
    need = max(g.get("min") or 1, 1)
    if len(mem["picked"]) >= need:
        return key(SCAN_E, *a, "grid: confirm")
    pool = [c for c in g["cards"] if c["name"] not in mem["missing"]]
    for n in mem["picked"]:  # already selected: clicking again would deselect
        hit = next((c for c in pool if c["name"] == n), None)
        if hit:
            pool.remove(hit)
    prompt = (g.get("prompt") or "").upper()
    if "UPGRADE" in prompt or g["screen"] == "NDeckUpgradeSelectScreen":
        pool = [c for c in pool if not c["upgraded"] and c["name"] in UPGRADES] or pool
        best = max(pool, key=lambda c: upgrade_gain(c, deck, UPGRADES, relics, s) if c["name"] in UPGRADES else -99,
                   default=None)
    elif "REMOVE" in prompt or "TRANSFORM" in prompt or "Transform" in g["screen"]:
        best = removal_target(pool, relics, s)
    else:
        best = max(pool, key=lambda c: rate(c, deck, relics, s)[0], default=None)
    if not best:
        return "nothing to pick in the card grid"
    err = press(is_card(best["name"]), "grid", *a, f"grid card {best['name']}")
    if err and err.startswith("couldn't find"):
        mem["missing"].add(best["name"])  # scrolled out of view: take the next best visible one
    elif not err:
        mem["picked"].append(best["name"])
    return err


def shop(s, mem, a):
    if not mem["shop_open"]:
        mem["shop_open"] = True
        return key(SCAN_ENTER, *a, "shop: open merchant", check=False)
    basket = [b for b in shop_basket(s) if b not in mem["no_buy"]]
    if basket and not mem["shop_done"]:
        kind, name = basket[0]
        want = "shop:remove" if kind == "remove" else f"shop:{kind}:{name}"
        a[2](f"shop: buy {name}" if kind != "remove" else f"shop: remove {name}")
        err = press(lambda d: d == want, "shop", *a, want)
        if err:
            mem["no_buy"].add(basket[0])
        return err
    if not mem["shop_done"]:
        mem["shop_done"] = True
        return key(SCAN_ESC, *a, "shop: close", check=False)
    return key(SCAN_E, *a, "shop: proceed")


MENU_ORDER = ("standard", "singleplayer", "single_player", "embark", "start")


def new_run(s, mem, a):
    """Main menu -> singleplayer -> standard -> same character -> confirm. ponytail: menu keys are matched by
    substring (not verified live); ascension stays whatever the character screen remembers."""
    targets = (s.get("ui") or {}).get("targets") or []
    char = mem.get("character")
    chars = [t for t in targets if t.startswith("char:")]
    if chars:
        want = f"char:{char}" if f"char:{char}" in chars else chars[0]
        if mem.get("char_picked") != want:
            err = press(lambda d: d == want, "menu", *a, want)
            if not err:
                mem["char_picked"] = want
            return err
        return key(SCAN_E, *a, "character select: confirm")
    mem["char_picked"] = None
    for word in MENU_ORDER:
        hit = next((t for t in targets if t.startswith("menu:") and word in t.lower()), None)
        if hit:
            return press(lambda d: d == hit, "menu", *a, hit)
    return WAIT


def run(get_state, cancel, log, retry):
    """Play until cancelled. retry() is read live (F9): on, failed actions are retried up to MAX_RETRIES and a
    new run starts after death; off, the first failure stops with its reason (for diagnosis)."""
    mem, fails, still, idle = {}, 0, None, time.time()
    shown = log

    def log(m):  # overlay line + autopilot_debug.log, so a stop can be diagnosed afterwards
        shown(m)
        with open(ap.DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {m}\n")

    while not cancel.is_set():
        s = get_state()
        if s.get("character"):
            mem["character"] = s["character"]
        if s.get("status") == "combat" and not s.get("choose_card"):
            result = ap.autopilot(get_state, lambda x: solve(x)[0], hand_pick, cancel, log,
                                  choose=lambda g, c, l: choose(g(), (g, c, l)))
            err = None if result.startswith(("combat over", "you died")) else result
        else:
            err = act(s, get_state, cancel, log, mem, retry)
        if cancel.is_set() or err == "cancelled":
            return "cancelled"
        if err == WAIT:
            now = sig(get_state())
            if now != still:
                still, idle = now, time.time()
            elif time.time() - idle > STUCK_AFTER:
                err, idle = f"stuck on {(s.get('ui') or {}).get('screen')} with nothing to do", time.time()
            if err == WAIT:
                time.sleep(0.3)
                continue
        if err is None:
            fails = 0
            continue
        fails += 1
        log(f"{err} (try {fails})")
        if not retry() or fails > MAX_RETRIES:
            return err
        time.sleep(1.5)
    return "cancelled"
