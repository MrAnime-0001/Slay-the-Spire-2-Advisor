"""F8 auto-play: plays a solver plan by sending real keyboard/mouse input to the game window.

The game plays a hand card with its number key (1-9, 0), then wants a mouse click: on the enemy
for targeted cards, anywhere above the hand for the rest. Enemy screen positions aren't readable,
so we sweep the mouse and watch the reader's `hovered` combat_id until it matches the target.
autopilot() repeats that every turn and ends the turn with E, until combat ends or F8 cancels."""
import ctypes
import os
import threading
import time
import winsound
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # physical pixels, so coordinates match the game
except (AttributeError, OSError):
    pass

VK_F8, WM_HOTKEY, MOD_NOREPEAT = 0x77, 0x0312, 0x4000
KEYEVENTF_SCANCODE, KEYEVENTF_KEYUP = 0x0008, 0x0002
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
SCAN_ESC, SCAN_E = 0x01, 0x12  # E = end turn (game default binding)
SCAN_DIGIT = {d: 0x02 + (d - 1) % 10 for d in range(1, 11)}  # keys 1..9 -> 0x02..0x0A, 10th card ('0') -> 0x0B
STEP_DELAY = 0.18   # seconds per sweep step: one game frame + one reader poll (100 ms)
# Rows ordered by where monsters usually stand; the hand starts around 78% height, so stay above it.
SWEEP = [(fx / 100, fy / 100) for fy in (60, 50, 68, 40, 30) for fx in range(35, 96, 5)]
# ponytail: fixed sweep grid; a monster outside it fails with "couldn't find". Add rows if one does.
UNTARGETED_CLICK = (0.5, 0.4)
# Potion bar sits in the top bar, left of center (~27-35% across in 1920x1057 screenshots).
POTION_SWEEP = [(fx / 100, fy / 1000) for fy in (35, 50, 25) for fx in range(20, 46)]
# The potion popup opens under its slot; Use is the first button. Searched relative to the slot.
USE_OFFSETS = [(dx / 100, dy / 100) for dy in range(4, 24, 2) for dx in (0, -2, 2)]
DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autopilot_debug.log")
MAX_PLAYS_PER_TURN = 15  # guards against a card that "plays" but keeps coming back


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def tap(scan):
    key = lambda flags: INPUT(1, _INPUTUNION(ki=KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE | flags, 0, 0)))
    _send(key(0))
    time.sleep(0.03)
    _send(key(KEYEVENTF_KEYUP))


def click():
    btn = lambda flags: INPUT(0, _INPUTUNION(mi=MOUSEINPUT(0, 0, 0, flags, 0, 0)))
    _send(btn(MOUSEEVENTF_LEFTDOWN))
    time.sleep(0.04)
    _send(btn(MOUSEEVENTF_LEFTUP))


def find_game():
    """Return (hwnd, left, top, width, height) of the game's client area, or None."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if user32.IsWindowVisible(hwnd) and buf.value.startswith("Slay the Spire 2"):
            found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    if not found:
        return None
    hwnd = found[0]
    rc, pt = wintypes.RECT(), wintypes.POINT(0, 0)
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return hwnd, pt.x, pt.y, rc.right, rc.bottom


def listen_hotkey(callback):
    """Call callback() on every F8 press, from a background thread (needs its own message loop)."""
    def loop():
        if not user32.RegisterHotKey(None, 1, MOD_NOREPEAT, VK_F8):
            return  # F8 taken by another app
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                callback()
    threading.Thread(target=loop, daemon=True).start()


positions = {}  # combat_id -> (fx, fy) client fractions, learned while sweeping
potion_positions = {}  # potion slot -> (fx, fy)
use_offset = []  # [(dx, dy)] from a potion slot to its popup's Use button, once found


def my_turn(s):
    return s.get("status") == "combat" and s.get("phase") == "Play"


def ended(s, turns):
    return f"you died (turn {turns})" if s.get("status") == "game_over" else f"combat over after {turns} turns"


def autopilot(get_state, plan_for, pick_for, cancel, log):
    """Every turn: re-plan from the live state after each card (so kills, draws and mispredictions
    never leave a stale plan), play the plan's first card, and press E once the plan is empty.
    When a card asks to pick hand cards (upgrade/exhaust/discard...), pick_for chooses them first.
    Stops when combat ends, on cancel, or on any failed card (so a targeting bug can't skip turns)."""
    turns = 0
    while not cancel.is_set():
        log(f"turn {turns + 1}: waiting for your turn")
        if not wait_until(lambda: my_turn(get_state()) or get_state().get("status") != "combat"
                          or get_state().get("choose_card"), 60, cancel):
            return "cancelled" if cancel.is_set() else "timed out waiting for turn"
        if get_state().get("choose_card"):
            error = wait_choice(get_state, cancel, log)
            if error:
                return error
            continue
        if get_state().get("status") != "combat":
            return ended(get_state(), turns)
        time.sleep(0.8)  # let the draw animation finish before reading the hand

        plays = 0
        while True:
            s = get_state()
            if cancel.is_set():
                return "cancelled"
            if s.get("status") != "combat":
                return ended(s, turns + 1)
            if s.get("hand_select"):
                error = pick_cards(s, get_state, pick_for, cancel, log)
                if error:
                    return error
                continue
            if s.get("choose_card"):
                error = wait_choice(get_state, cancel, log)
                if error:
                    return error
                continue
            steps = plan_for(s).steps
            if not steps:
                time.sleep(0.3)  # a pick prompt can open just after the last card resolves
                if get_state().get("hand_select") or get_state().get("choose_card"):
                    continue
                break
            plays += 1
            if plays > MAX_PLAYS_PER_TURN:
                return f"over {MAX_PLAYS_PER_TURN} plays this turn, stopping"
            play = play_potion if steps[0][5] == "potion" else play_step
            error = play(steps[0], s, get_state, cancel, log)
            if error:
                return error

        s = get_state()
        log("ending turn")
        tap(SCAN_E)
        if not wait_until(lambda: not my_turn(get_state()) or get_state().get("turn") != s["turn"], 5, cancel):
            return "end turn didn't register"
        turns += 1
    return "cancelled"


def wait_choice(get_state, cancel, log):
    """Choose-a-card screen (Knowledge Demon curse, Attack Potion...): the overlay shows the pick and
    you click it; autopilot carries on once the screen closes.
    ponytail: not clicked automatically, card positions on that screen aren't verified yet."""
    log("CHOOSE A CARD YOURSELF (see overlay) - autopilot waits")
    winsound.MessageBeep(winsound.MB_ICONASTERISK)
    if not wait_until(lambda: not get_state().get("choose_card"), 300, cancel):
        return "cancelled" if cancel.is_set() else "card choice still open after 5 min"
    time.sleep(0.5)
    return None


def pick_cards(s, get_state, pick_for, cancel, log):
    """Hand-pick prompt: number keys pick from the cards still shown (highest first, since picked cards
    leave the row), then E confirms unless the game already closed it at the max picks."""
    keys, names = pick_for(s)
    log(f"picking {', '.join(names) or 'nothing'} ({s['hand_select']['prompt']})")
    game = find_game()
    if not game:
        return "game window not found"
    user32.SetForegroundWindow(game[0])
    for k in keys:
        if k in SCAN_DIGIT:
            tap(SCAN_DIGIT[k])
            time.sleep(0.25)
    if get_state().get("hand_select"):
        tap(SCAN_E)
    if not wait_until(lambda: not get_state().get("hand_select"), 3, cancel):
        return "card pick didn't register - pick it yourself"
    time.sleep(0.35)
    return None


def play_step(step, s, get_state, cancel, log):
    """Play one solver step against live state s. Returns None on success, else a reason."""
    label, target_name, _, card, combat_id, _ = step
    game = find_game()
    if not game:
        return "game window not found"
    hwnd, left, top, w, h = game
    at = lambda fx, fy: user32.SetCursorPos(int(left + w * fx), int(top + h * fy))
    user32.SetForegroundWindow(hwnd)

    idx = next((i for i, c in enumerate(s["hand"])
                if c["name"] == card["name"] and c["upgraded"] == card["upgraded"]), None)
    if idx is None or idx > 9:
        return f"{label} not in hand"
    before = ([c["name"] for c in s["hand"]], s["energy"])
    log(f"playing {label}" + (f" -> {target_name}" if target_name else ""))

    tap(SCAN_DIGIT[idx + 1])
    time.sleep(0.15)
    if card["target"] == "AnyEnemy":
        if not aim(combat_id, at, get_state, cancel, game):
            return missed(get_state, cancel, combat_id, target_name)
    else:
        at(*UNTARGETED_CLICK)
        time.sleep(0.05)
    click()

    if not wait_until(lambda: changed(get_state(), before), 2.0, cancel):
        drop(get_state)
        return None if cancel.is_set() or get_state().get("status") != "combat" else f"{label} didn't play"
    time.sleep(0.35)  # let the play animation settle before picking up the next card
    return None


def play_potion(step, s, get_state, cancel, log):
    """Drink a potion: click its slot in the top bar, click Use in the popup, then the target if any.
    Slot and Use button are found like enemies: move the mouse, watch the reader's hover flags."""
    _, target_name, _, pot, combat_id, _ = step
    game = find_game()
    if not game:
        return "game window not found"
    hwnd, left, top, w, h = game
    at = lambda fx, fy: user32.SetCursorPos(int(left + w * fx), int(top + h * fy))
    user32.SetForegroundWindow(hwnd)
    ui = lambda st: st.get("potion_ui") or {}
    slot = pot["slot"]
    log(f"drinking {pot['name']}" + (f" -> {target_name}" if target_name else ""))

    def learn(st, point):
        if ui(st).get("hovered_slot") is not None:
            potion_positions[ui(st)["hovered_slot"]] = point

    tries = ([potion_positions[slot]] if slot in potion_positions else []) + POTION_SWEEP
    spot = seek(tries, at, lambda st: ui(st).get("hovered_slot") == slot, get_state, cancel,
                f"potion slot {slot} ({pot['name']})", game, learn)
    if not spot:
        return f"couldn't find {pot['name']} in the potion bar"
    click()
    if not wait_until(lambda: ui(get_state()).get("popup_open"), 1.5, cancel):
        return f"{pot['name']} menu didn't open"
    offsets = use_offset + [o for o in USE_OFFSETS if o not in use_offset]
    use = seek([(spot[0] + dx, spot[1] + dy) for dx, dy in offsets], at,
               lambda st: ui(st).get("use_hovered"), get_state, cancel, "potion Use button", game)
    if not use:
        drop(get_state)
        return f"couldn't find the Use button for {pot['name']}"
    use_offset[:] = [(use[0] - spot[0], use[1] - spot[1])]
    before = [p["name"] for p in s.get("potions", [])]
    click()
    if pot["target"] == "AnyEnemy":
        time.sleep(0.15)
        if not aim(combat_id, at, get_state, cancel, game):
            return missed(get_state, cancel, combat_id, target_name)
        click()
    if not wait_until(lambda: [p["name"] for p in get_state().get("potions", [])] != before, 2.0, cancel):
        drop(get_state)
        return None if cancel.is_set() or get_state().get("status") != "combat" else f"{pot['name']} didn't get used"
    time.sleep(0.35)
    return None


def target_gone(st, combat_id):
    return st.get("status") != "combat" or combat_id not in [e.get("combat_id") for e in st.get("enemies", [])]


def drop(get_state):
    """Esc puts a held card back, but outside our turn (fight over) it opens the pause menu instead."""
    if my_turn(get_state()):
        tap(SCAN_ESC)


def missed(get_state, cancel, combat_id, name):
    """After a failed aim: target died / fight ended / cancelled -> None (the loop re-plans or stops
    cleanly); otherwise a real targeting failure."""
    drop(get_state)
    if cancel.is_set() or target_gone(get_state(), combat_id):
        return None
    return f"couldn't find {name} on screen"


def aim(combat_id, at, get_state, cancel, game=None):
    if combat_id is None:
        return False

    def learn(st, point):
        if st.get("hovered") is not None:
            positions[st["hovered"]] = point  # remember every enemy we pass over

    tries = ([positions[combat_id]] if combat_id in positions else []) + SWEEP
    return seek(tries, at, lambda st: st.get("hovered") == combat_id, get_state, cancel,
                f"enemy combat_id={combat_id}", game, learn, gone=lambda st: target_gone(st, combat_id)) is not None


def seek(points, at, matches, get_state, cancel, what, game=None, learn=None, gone=None):
    """Move the mouse over points until matches(state); return the point, or None (and log why).
    Stops early (no log) on cancel or when gone(state) says the target no longer exists."""
    seen = []
    for fx, fy in points:
        if cancel.is_set() or (gone and gone(get_state())):
            return None
        at(fx, fy)
        time.sleep(STEP_DELAY)
        st = get_state()
        if learn:
            learn(st, (fx, fy))
        if matches(st):
            return (fx, fy)
        ui = st.get("potion_ui") or {}
        seen.append(f"({fx:.3f},{fy:.3f}) enemy={st.get('hovered')} slot={ui.get('hovered_slot')} "
                    f"use={ui.get('use_hovered')} popup={ui.get('popup_open')}")
    debug(what, seen, game, get_state())
    return None


def debug(what, seen, game, st):
    """Append a failed search to autopilot_debug.log so the sweep grid can be fixed for that case."""
    enemies = [(e["name"], e.get("combat_id")) for e in st.get("enemies", [])]
    with open(DEBUG_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} couldn't find {what}\n"
                f"window (hwnd, left, top, w, h) = {game}\nenemies = {enemies}\n"
                f"potions = {st.get('potions')}\n" + "\n".join(seen) + "\n")


def changed(s, before):
    return s.get("status") != "combat" or ([c["name"] for c in s["hand"]], s["energy"]) != before


def wait_until(cond, timeout, cancel):
    end = time.time() + timeout
    while time.time() < end and not cancel.is_set():
        if cond():
            return True
        time.sleep(0.05)
    return False
