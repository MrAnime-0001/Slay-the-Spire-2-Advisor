"""Enemy foresight from the game's own AI tables (gamedata["monsters"], see tools/gen_gamedata.py).
predict() walks an enemy's move state machine from the move it shows now and returns the expected
damage of its next turns, so the solver can see Enlarge / Vigor / big hits coming."""
import re

from relics import GAMEDATA

MONSTERS = GAMEDATA.get("monsters", {})
ATTACKS = ("SingleAttack", "DeathBlow")
# ponytail: unknown branch conditions (other than slot checks) split evenly; enemy Weak and
# Strength-down on enemies are not projected past this turn.


# Attack numbers the code computes at runtime (args kept as source text by gen_gamedata), from the code.
RUNTIME = {
    "DreadDamage": lambda asc, pw: (15 if asc >= 9 else 13) + pw.get("DexterityPower", 0),  # The Forgotten
    "SteamEruptionDamage": lambda asc, pw: pw.get("SteamEruptionPower", 0),  # Waterfall Giant's explode
    "CurrentPressureGunDamage": lambda asc, pw: 23 if asc >= 9 else 20,  # ponytail: +5 per use not tracked
    "BaseMultiClawCount + ExtraMultiClawCount": lambda asc, pw: 3,  # Test Subject (extra claws not tracked)
}


def num(v, asc, powers=None):
    """A gamedata number at this ascension (None when the code computes it at runtime and it's unknown)."""
    if isinstance(v, dict):
        return v["asc"] if asc >= v["at"] else v["base"]
    if isinstance(v, str) and v in RUNTIME:
        return RUNTIME[v](asc, powers or {})
    return v if isinstance(v, (int, float)) else None


def attacks(move, asc, powers=None):
    """[(damage per hit, hits)] for each attack intent of a move."""
    out = []
    for it in move["intents"]:
        a = [num(x, asc, powers) for x in it["args"]]
        if it["kind"] in ATTACKS and a and a[0] is not None:
            out.append((a[0], 1))
        elif it["kind"] == "MultiAttack" and len(a) >= 2 and None not in a[:2]:
            out.append((a[0], a[1]))
    return out


TEAM = "CombatState.GetTeammatesOf(Creature)"  # The Obscura's Sail: its whole side, itself included


def self_power(move, asc, power="StrengthPower"):
    return sum(num(e["amt"], asc) or 0 for e in move["effects"]
               if e.get("power") == power and e.get("on") in ("self", TEAM))


def summon_dmg(name, moves, asc):
    """Expected damage per turn of what a forecast turn's moves {ID: p} summon (Fogmog's Eye With Teeth, Living
    Fog's Gas Bomb, Ovicopter's eggs...): the summon's mean attack over its moves. ponytail: no Strength, no cycle."""
    ai, out = MONSTERS.get(name), 0
    for mv, p in moves.items():
        for e in (ai["moves"][mv]["effects"] if ai and mv in ai["moves"] else ()):
            sm = MONSTERS.get(e.get("summon"))
            if sm:
                per = [sum(d * h for d, h in attacks(m, asc)) for m in sm["moves"].values()]
                out += p * sum(per) / max(len(per), 1)
    return out


def ally_strength(name, moves, asc):
    """Expected Strength a forecast turn's moves {ID: p} give the enemy's teammates (not itself).
    ponytail: the Queen's BURN_BRIGHT_FOR_ME amount (a runtime var) isn't known, counted 0."""
    ai = MONSTERS.get(name)
    return sum(p * sum(num(e["amt"], asc) or 0 for e in ai["moves"][mv]["effects"]
                       if e.get("power") == "StrengthPower" and e.get("on") == TEAM)
               for mv, p in moves.items() if ai and mv in ai["moves"])


def follow(ai, state, prev, used, slot, depth=0):
    """{move ID: probability} for what comes after `state` (a move or branch ID)."""
    nxt = ai["next"].get(state, state if depth else None)
    if nxt is None:
        return {}
    if nxt in ai["moves"]:
        return {nxt: 1.0}
    branch = ai["branches"].get(nxt)
    if not branch or depth > 4:
        return {}
    options = []
    for b in branch["to"]:
        if branch["kind"] == "random":
            if (b["repeat"] == "CannotRepeat" and b["to"] == prev) or (b["repeat"] == "UseOnlyOnce" and b["to"] in used):
                continue
            weight = next((x for x in b["args"] if isinstance(x, (int, float))), 1)
        else:  # conditional: only slot checks are knowable from outside
            cond = " ".join(str(x) for x in b["args"])
            m = re.search(r'SlotName == "(\w+)"', cond)
            if m and slot and m[1] != slot:
                continue
            weight = 1
        options.append((b["to"], weight))
    total = sum(w for _, w in options) or 1
    out = {}
    for to, w in options:
        for mv, p in ({to: 1.0} if to in ai["moves"] else follow(ai, to, prev, used, slot, depth + 1)).items():
            out[mv] = out.get(mv, 0) + p * w / total
    return out


# TemporaryStrengthPower subclasses (both builds): they end at the end of the owner's turn, so the moves after the
# shown one hit with the Strength back. Down = Strength lost (Enfeebling Touch...), up = gained.
TEMP_STRENGTH_DOWN = {"CrushUnderPower", "DarkShacklesPower", "DyingStarPower", "EnfeeblingTouchPower", "ManglePower",
                      "MonarchsGazeStrengthDownPower", "PiercingWailPower", "ShacklingPotionPower"}
TEMP_STRENGTH_UP = {"CoordinatePower", "FeedingFrenzyPower", "FlexPotionPower", "ReptileTrinketPower", "SetupStrikePower"}


def predict(enemy, asc=0, turns=2):
    """Expected damage to you on each of the next `turns` enemy turns AFTER the one shown now:
    [{"dmg": float, "moves": {ID: p}}]. Empty when the AI table or current move is unknown."""
    ai, move = MONSTERS.get(enemy.get("name")), enemy.get("move")
    if not ai or move not in ai["moves"]:
        return []
    powers = {p["name"]: p["amount"] for p in enemy["creature"].get("powers", [])}
    shown = ai["moves"][move]
    # Byrdonis Territorial, Cultist Ritual: +Strength at the end of each of its turns
    # Zapbot's High Voltage (game text: end of its turn, the wiki says start) works the same way
    territorial = powers.get("TerritorialPower", 0) + powers.get("RitualPower", 0) + powers.get("HighVoltagePower", 0)
    strength = powers.get("StrengthPower", 0) + self_power(shown, asc) + territorial
    strength += sum(n for p, n in powers.items() if p in TEMP_STRENGTH_DOWN)
    strength -= sum(n for p, n in powers.items() if p in TEMP_STRENGTH_UP)
    # Vigor boosts the next attack's hits; the shown move uses up existing Vigor if it attacks.
    vigor = (0 if attacks(shown, asc) else powers.get("VigorPower", 0)) + self_power(shown, asc, "VigorPower")
    used = set(enemy.get("history", [])) | {move}
    dist, out = {move: 1.0}, []
    ritual = self_power(shown, asc, "RitualPower")  # applied by the shown move: skips that turn's trigger
    for k in range(turns):
        if k == 1:  # its first trigger: end of the next turn
            territorial += ritual
            strength += ritual
        nxt = {}
        for mv, p in dist.items():
            for to, q in follow(ai, mv, mv, used, enemy.get("slot")).items():
                nxt[to] = nxt.get(to, 0) + p * q
        dmg = sum(p * sum(max(0, d + strength + vigor) * h for d, h in attacks(ai["moves"][mv], asc, powers))
                  for mv, p in nxt.items())
        # its biggest single hit (Buffer stops one HP loss, not the whole turn)
        big = sum(p * max((max(0, d + strength + vigor) for d, _ in attacks(ai["moves"][mv], asc, powers)), default=0)
                  for mv, p in nxt.items())
        if any(attacks(ai["moves"][mv], asc) for mv in nxt):
            vigor = 0  # ponytail: Vigor dropped once any likely move attacks
        strength += sum(p * self_power(ai["moves"][mv], asc) for mv, p in nxt.items()) + territorial
        vigor += sum(p * self_power(ai["moves"][mv], asc, "VigorPower") for mv, p in nxt.items())
        used |= set(nxt)
        out.append({"dmg": round(dmg, 1), "hit": round(big, 1), "moves": {k: round(v, 2) for k, v in nxt.items()}})
        dist = nxt
    return out
