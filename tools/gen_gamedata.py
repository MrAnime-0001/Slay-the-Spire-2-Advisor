"""Extract card upgrades, potion effects, relic numbers and rarities from the game's code into
advisor/gamedata.json. These live in code (OnUpgrade(), CanonicalVars, TargetType overrides),
not in memory, so the advisor needs this table. Re-run after each game patch:

    python tools/gen_gamedata.py "D:\\SteamLibrary\\steamapps\\common\\Slay the Spire 2"

Needs ilspycmd (dotnet tool install -g ilspycmd). Pass an already-decompiled folder as a second
argument to skip the decompile step (kept in "sts2-advisor Dev\\decomp\\sts2src-<version>").

Refresh only the code tables of an existing build file (keeps its text, so the installed game can be another build):

    python tools/gen_gamedata.py advisor/gamedata/v0.111.0.json "..\\decomp\\sts2src-0.111.0"
"""
import json
import os
import re
import struct
import subprocess
import sys
import tempfile

VAR_PROP = re.compile(r'public \S+ (\w+) => \(\S+\)_vars\["(\w+)"\];')
VAR_BY_PROP = re.compile(r'(?:base\.)?DynamicVars\.(\w+)\.UpgradeValueBy\((-?[\d.]+)m\)')
VAR_BY_KEY = re.compile(r'(?:base\.)?DynamicVars\["(\w+)"\]\.UpgradeValueBy\((-?[\d.]+)m\)')
COST = re.compile(r'(?:base\.)?EnergyCost\.UpgradeBy\((-?\d+)\)')
KEYWORD = re.compile(r'(Add|Remove)Keyword\(CardKeyword\.(\w+)\)')
NEW_VAR = re.compile(r'new (\w*?)Var(?:<(\w+)>)?\(\s*(?:"(\w+)",\s*)?(-?[\d.]+)m?')
TARGET = re.compile(r'TargetType TargetType => TargetType\.(\w+)')
HOOK = re.compile(r'public override (?:async )?[\w<>?]+ ((?:Modify|After|Before|Should|TryModify)\w+)\(')
RARITY = re.compile(r'Rarity Rarity => (?:Relic|Potion)Rarity\.(\w+)')


def rarity(source):
    m = RARITY.search(source)
    return m[1] if m else None


def upgrade_body(source):
    """Statements inside `void OnUpgrade() { ... }`, or None if the card has no upgrade."""
    m = re.search(r'void OnUpgrade\(\)\s*\{', source)
    if not m:
        return None
    depth, i = 1, m.end()
    while depth and i < len(source):
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        i += 1
    return [s.strip() for s in source[m.end():i - 1].split(";") if s.strip().strip("{}").strip()]


def canonical_vars(source):
    """{key: value} from the CanonicalVars member (ILSpy separates members with blank lines)."""
    i = source.find("CanonicalVars")
    if i < 0:
        return {}
    region = source[i:].split("\n\n", 1)[0]
    out = {}
    for prefix, generic, quoted, value in NEW_VAR.findall(region):
        out[generic or quoted or prefix] = float(value)
    return out


def classes(src_dir, folder):
    path = os.path.join(src_dir, folder)
    for name in sorted(os.listdir(path)):
        if name.endswith(".cs"):
            with open(os.path.join(path, name), encoding="utf-8") as f:
                yield name[:-3], f.read()


def parse_upgrades(src_dir):
    with open(os.path.join(src_dir, "MegaCrit.Sts2.Core.Localization.DynamicVars", "DynamicVarSet.cs"), encoding="utf-8") as f:
        prop_to_key = dict(VAR_PROP.findall(f.read()))
    table = {}
    for name, source in classes(src_dir, "MegaCrit.Sts2.Core.Models.Cards"):
        body = upgrade_body(source)
        if body is None:
            continue
        up = {"vars": {}, "cost": 0, "keywords": [], "other": []}
        for stmt in body:
            if m := VAR_BY_PROP.search(stmt):
                key = prop_to_key.get(m[1], m[1])
                up["vars"][key] = up["vars"].get(key, 0) + float(m[2])
            elif m := VAR_BY_KEY.search(stmt):
                up["vars"][m[1]] = up["vars"].get(m[1], 0) + float(m[2])
            elif m := COST.search(stmt):
                up["cost"] += int(m[1])
            elif m := KEYWORD.search(stmt):
                up["keywords"].append(("+" if m[1] == "Add" else "-") + m[2])
            elif stmt != "base.OnUpgrade()":
                up["other"].append(stmt)
        table[name] = {k: v for k, v in up.items() if v}
    return table


def parse_potions(src_dir):
    table = {}
    for name, source in classes(src_dir, "MegaCrit.Sts2.Core.Models.Potions"):
        target = TARGET.search(source)
        if not target:
            continue  # abstract/base classes
        t = target[1]
        table[name] = {"target": "Self" if t == "AnyPlayer" else t, "vars": canonical_vars(source),
                       "rarity": rarity(source), **pile_moves(source)}
    return table


def pile_moves(source):
    """{draws: count var, fetch: {from, to, top, count, filter}} for cards/potions that draw or move cards between
    piles (research/piles.md). count = a number or a DynamicVars name; filter = card type the pick is limited to.
    Only player picks that end in hand / on the draw pile count as fetches (transform/exhaust picks don't)."""
    out = {}
    if m := re.search(r'CardPileCmd\.Draw\(\w+, DynamicVars(?:\.(\w+)|\["(\w+)"\])\.(?:BaseValue|IntValue)', source):
        out["draws"] = m[1] or m[2]
    src = re.search(r'CardSelectCmd\.FromCombatPile\([^;]*?PileType\.(\w+)\.GetPile', source)
    frm = src[1] if src else "Hand" if "CardSelectCmd.FromHand(" in source else None
    dest = re.search(r'CardPileCmd\.Add\([^;]*?PileType\.(Hand|Draw|Discard)(, CardPilePosition\.Top)?\)', source)
    if frm and dest and "TransformSelectionPrompt" not in source and "ExhaustSelectionPrompt" not in source:
        n = re.search(r'new CardSelectorPrefs\((?:SelectionScreenPrompt|\w+), (?:0, )?(\w+|DynamicVars(?:\.(\w+)|\["(\w+)"\])\.IntValue)\)', source)
        count = 1
        if n:
            count = int(n[1]) if n[1].isdigit() else n[2] or n[3] or "Cards"  # Dredge: num = Min(Cards, room in hand)
        flt = re.search(r'filter: \(CardModel c\) => c\.Type == CardType\.(\w+)', source)
        out["fetch"] = {"from": frm, "to": dest[1], "top": bool(dest[2]), "count": count, "filter": flt[1] if flt else None}
    return out


def created(source):
    """[card, count var] for cards a card puts in your hand (Shivs, Debris, Fuel...), else None.
    ponytail: Storm of Steel (one Shiv per card discarded) and discard-pile adds aren't listed."""
    if m := re.search(r'Shiv\.CreateInHand\(Owner, DynamicVars(?:\.(\w+)|\["(\w+)"\])\.IntValue', source):
        return ["Shiv", m[1] or m[2]]
    if "Shiv.CreateInHand(Owner, CombatState)" in source:
        loop = re.search(r'i < DynamicVars\.(\w+)\.IntValue', source)
        return ["Shiv", loop[1] if loop else None]
    if (m := re.search(r'CreateCard<(\w+)>\(Owner\)', source)) and ("PileType.Hand" in source or "PileType." not in source):
        return [m[1], None]
    return None


# --- card effects: OnPlay() read as an ordered list of ops ---
# Each game command becomes one op; amounts are DynamicVars keys (read live from the card), numbers, or "-Key":
#   damage {amt, hits, tgt: one|all|random, by?: osty}   block {amt}   power {power, amt, tgt: self|one|all}
#   draw / energy / stars / summon / forge / heal / hp_loss / max_hp_loss / orb_slots {amt}   channel {orb}
#   evoke / orb_passive / kill_osty   create {card: name|random|choice, count, to}
#   select {from, to, count, mode}: we pick cards (to = Discard / Exhaust / Upgrade / Play / DrawTop / Hand...)
#   exhaust / discard / upgrade / transform / autoplay / move {which: random|hand|self|generated|pile:<Pile>}
#   repeat {n, ops}   cond {if, ops}   unknown {stmt}
# A count can be "X" (X-cost), "hand" (cards in hand), "calc:<Key>" (computed by the game) or "?" (unknown).
NOISE = re.compile(r'Sfx|Vfx|VfxCmd|SfxCmd|Cmd\.Wait|TriggerAnim|ArgumentNullException|CustomScaledWait|\bFlash\(|CardCmd\.Preview')
# conditions that hold in a normal play: a pick was made, a count is positive, Osty is there, not a test mock
KNOWN_IF = re.compile(r'^\s*!?\s*\w+\s*!=\s*null\s*$|^\s*\w+\s*>\s*0\s*$|^\s*\w+\.Any\(\)\s*$|^!Osty\.CheckMissingWithAnim|'
                      r'^Owner\.IsOstyAlive$|^_mock\w+ == null$|^!CombatManager\.Instance\.IsOverOrEnding$')


def onplay_body(source):
    m = re.search(r'Task OnPlay\([^)]*\)\s*\{', source)
    return enclosed(source, m.end() - 1) if m else None


def blocks(text):
    """C# statements -> [("stmt", s) | ("if", cond, then, else) | ("loop", kind, header, body)] (lambdas stay inside
    their statement)."""
    out, i, n = [], 0, len(text)

    def block_at(j):  # a { } block or one statement starting at j -> (items, end)
        while j < n and text[j].isspace():
            j += 1
        if j < n and text[j] == "{":
            inner = enclosed(text, j)
            return blocks(inner), j + len(inner) + 2
        k = statement_end(j)
        return blocks(text[j:k]), k

    def statement_end(j):
        depth = 0
        while j < n:
            c = text[j]
            depth += (c in "([{") - (c in ")]}")
            if c == ";" and depth == 0:
                return j + 1
            j += 1
        return n

    while i < n:
        if text[i].isspace():
            i += 1
            continue
        m = re.match(r'(if|foreach|for|while|switch|using|lock)\s*\(', text[i:])
        if m:
            p = i + m.end() - 1
            header = enclosed(text, p)
            body, j = block_at(p + len(header) + 2)
            if m[1] == "if":
                rest = re.match(r'\s*else\b', text[j:])
                other, j = block_at(j + rest.end()) if rest else ([], j)
                out.append(("if", header, body, other))
            else:
                out.append(("loop", m[1], header, body))
            i = j
            continue
        if text[i] == "{":
            inner = enclosed(text, i)
            out += blocks(inner)
            i += len(inner) + 2
            continue
        k = statement_end(i)
        out.append(("stmt", text[i:k].strip()))
        i = k
    return out


def parse_ops(source, prop_to_key):
    """(ops, ops when upgraded or None if the same) from a card's OnPlay()."""
    body = onplay_body(source)
    if body is None:
        return None, None
    tree = blocks(body)
    base = Walker(prop_to_key).walk(tree, upgraded=False)
    up = Walker(prop_to_key).walk(tree, upgraded=True)
    return base, (up if up != base else None)


class Walker:
    """Turns the statement tree into ops, following one upgrade state."""

    def __init__(self, prop_to_key):
        self.keys = prop_to_key
        self.local = {}      # local variable -> amount ("Power", 3, "X", "hand") or a group ("allies", "enemies")
        self.picked = {}     # local holding a player pick -> its select op
        self.made = {}       # local holding generated card(s) -> {"card": name|"random"|"choice", "count": n}
        self.rand = set()    # locals holding a random hand card

    def amount(self, expr):
        expr = expr.strip()
        if m := re.fullmatch(r'\(?(-?\d+(?:\.\d+)?)[mMfF]?\)?', expr):
            v = float(m[1])
            return int(v) if v.is_integer() else v
        if m := re.match(r'(?:base\.)?DynamicVars(?:\.(\w+)|\["(\w+)"\])(?:\.(?:BaseValue|IntValue))?$', expr):
            return self.keys.get(m[1], m[1]) if m[1] else m[2]
        if m := re.fullmatch(r'\(\w+\)\s*(.+)', expr, re.S):  # cast: (int)x, (BlockVar)DynamicVars["K"]
            return self.amount(m[1])
        if expr.startswith("(") and enclosed(expr, 0) == expr[1:-1]:
            return self.amount(expr[1:-1])
        if expr.startswith("-"):
            v = self.amount(expr[1:])
            return -v if isinstance(v, (int, float)) else v if v == "?" else "-" + v
        if m := re.search(r'DynamicVars(?:\.(\w+)|\["(\w+)"\])\)*\.Calculate\(', expr):  # CalculatedVar: game-computed
            return "calc:" + (self.keys.get(m[1], m[1]) if m[1] else m[2])
        if expr.startswith("Hook.Modify"):  # Hook.ModifyBlock(state, creature, amount, ...): the base amount
            return next((v for a in split_args(enclosed(expr, expr.index("("))) if (v := self.amount(a)) != "?"), "?")
        if "ResolveEnergyXValue" in expr:
            return "X"
        if m := re.fullmatch(r'(\w+)\.(?:BaseValue|IntValue)', expr):
            return self.local.get(m[1], "?")
        return self.local.get(expr, "?")

    def walk(self, items, upgraded):
        ops = []
        for it in items:
            if it[0] == "stmt":
                if self.stmt(it[1], ops) == "return":
                    return ops
            elif it[0] == "if":
                _, cond, then, other = it
                c = cond.strip()
                if "DamageCmd.Attack(" in c:  # if ((await DamageCmd.Attack(...)).Results.Any(r => r.WasKilled))
                    self.stmt(c, ops)
                    c = "attack result: " + c[c.rfind(".Results"):]
                if re.fullmatch(r'!?\s*(base\.)?IsUpgraded', c):
                    take = upgraded != c.startswith("!")
                    branch = then if take else other
                    ops += self.walk(branch, upgraded)
                    if take and any(x[0] == "stmt" and x[1].startswith("return") for x in branch):
                        return ops
                elif KNOWN_IF.match(c):  # "picked card != null", "num > 0", "Osty is there": the normal case
                    ops += self.walk(then, upgraded)
                else:
                    inner = self.walk(then, upgraded) + self.walk(other, upgraded)
                    if inner:
                        ops.append({"op": "cond", "if": c[:80], "ops": inner})
            else:
                ops += self.loop(it[1], it[2], it[3], upgraded)
        return ops

    def loop(self, kind, header, body, upgraded):
        pre = []
        if kind == "foreach" and (m := re.match(r'\s*[\w<>?]+\s+(\w+)\s+in\s+(.+)$', header, re.S)):
            var, coll = m[1], m[2].strip()
            if re.search(r'\w+Cmd\.|CreateInHand', coll):  # foreach (var x in await Shiv.CreateInHand(...))
                self.stmt(coll, pre)
            group = ("enemies" if re.search(r'Enemies|Opponents', coll) else
                     "allies" if re.search(r'Teammates|PlayerCreatures', coll) else
                     "hand" if re.search(r'PileType\.Hand', coll) else self.local.get(coll))
            if coll in self.picked:
                self.picked[var] = self.picked[coll]
            if coll in self.made:
                self.made[var] = self.made[coll]
            if group:
                self.local[var] = group
            inner = self.walk(body, upgraded)
            if group == "enemies":
                return pre + [{**o, "tgt": "all"} if o.get("tgt") in ("one", "all") else o for o in inner]
            if group == "allies":  # single player: our own creature
                return pre + [{**o, "tgt": "self"} if o.get("tgt") == "all" else o for o in inner]
            if group == "hand" or str(group).startswith("pile:") or coll in self.picked or coll in self.made or pre:
                return pre + inner  # once per card: the op itself already covers the whole group
            return pre + ([{"op": "cond", "if": f"foreach ({header[:60]})", "ops": inner}] if inner else [])
        inner = self.walk(body, upgraded)
        if not inner:
            return []
        if kind == "for" and (m := re.search(r'<\s*([^;]+);', header)):
            return [{"op": "repeat", "n": self.amount(m[1]), "ops": inner}]
        return [{"op": "cond", "if": f"{kind} ({header[:60]})", "ops": inner}]

    def which(self, var):
        """What a card variable refers to: its select op, or a kind (random / hand / self / generated)."""
        var = var.strip()
        if var in self.picked:
            return self.picked[var]
        if var == "this":
            return "self"
        if var in self.rand:
            return "random"
        if self.local.get(var) == "hand" or str(self.local.get(var)).startswith("pile:"):
            return self.local[var]
        return "generated" if var in self.made else "random"

    def stmt(self, s, ops):
        if s.startswith("return"):
            return "return"
        if NOISE.search(s) and "DamageCmd" not in s and "AddGenerated" not in s:
            return None
        # locals: int amount = DynamicVars["Power"].IntValue; picks; generated cards; groups of creatures
        if m := re.match(r'(?:[\w<>?.,]+\s+)?(\w+)\s*=(?![=>])\s*(.+?);?$', s, re.S):
            name, rhs = m[1], m[2]
            if "CardSelectCmd." in rhs:
                op = self.select(rhs)
                if "FromChooseACardScreen" in rhs:  # pick 1 of N generated cards: becomes the generated card
                    self.made[name] = {"card": "choice", "count": 1}
                else:
                    ops.append(op)
                    self.picked[name] = op
                return None
            if (c := re.search(r'CreateCard<(\w+)>', rhs)) and "Cmd." not in rhs:
                self.made[name] = {"card": c[1], "count": 1}
                return None
            if "CardFactory." in rhs:
                n = re.search(r',\s*([^,]+),\s*Owner\.RunState\.Rng\.CombatCardGeneration', rhs)
                count = 1 if "FirstOrDefault" in rhs or not n else self.amount(n[1])
                self.made[name] = {"card": "random", "count": count}
                return None
            if "CombatCardSelection.NextItem" in rhs or "Rng." in rhs and "Hand" in rhs:
                self.rand.add(name)
                return None
            if re.search(r'Teammates|PlayerCreatures', rhs):
                self.local[name] = "allies"
                return None
            if ".CreateInHand(" in rhs:  # var shivs = await Shiv.CreateInHand(...)
                self.stmt(rhs, ops)
                self.made[name] = {"card": ops[-1]["card"], "count": ops[-1]["count"]}
                return None
            if p := re.search(r'PileType\.(\w+)\.GetPile\(Owner\)\.Cards|PlayerCombatState\.(\w+)Pile\.Cards', rhs):
                pile = p[1] or p[2]  # a filtered pile ("pile:Discard") or the whole hand
                self.local[name] = "hand" if pile == "Hand" and ".Where" not in rhs else "pile:" + pile
                return None
            if re.fullmatch(r'\w+\.Count(\(\))?', rhs.strip()) and self.local.get(rhs.split(".")[0]) == "hand":
                self.local[name] = "hand"
                return None
            if "Cmd." not in rhs:
                self.local[name] = self.amount(rhs)
                return None
        if "DamageCmd.Attack(" in s:
            a = split_args(enclosed(s, s.index("DamageCmd.Attack(") + len("DamageCmd.Attack")))
            hits = s.find(".WithHitCount(")
            tgt = "all" if "TargetingAllOpponents" in s else "random" if "TargetingRandomOpponents" in s else "one"
            op = {"op": "damage", "amt": self.amount(a[0]) if a else "?",
                  "hits": self.amount(enclosed(s, hits + len(".WithHitCount"))) if hits >= 0 else 1, "tgt": tgt}
            if ".FromOsty(" in s:
                op["by"] = "osty"
            ops.append(op)
            return None
        if m := re.search(r'PowerCmd\.Apply<(\w+)>\(', s):
            a = split_args(enclosed(s, m.end() - 1))
            who = a[1] if len(a) > 1 else ""
            tgt = ("self" if re.search(r'Owner\.Creature|Owner\b', who) else "one" if "cardPlay.Target" in who else
                   "self" if self.local.get(who.strip()) == "allies" else "all")
            ops.append({"op": "power", "power": m[1], "amt": self.amount(a[2]) if len(a) > 2 else "?", "tgt": tgt})
            return None
        simple = (("CreatureCmd.GainBlock(", "block", 1), ("CardPileCmd.Draw(", "draw", 1), ("PlayerCmd.GainEnergy(", "energy", 0),
                  ("PlayerCmd.GainStars(", "stars", 0), ("OstyCmd.Summon(", "summon", 2), ("ForgeCmd.Forge(", "forge", 0),
                  ("CreatureCmd.Heal(", "heal", 1), ("OrbCmd.AddSlots(", "orb_slots", 1), ("CreatureCmd.LoseMaxHp(", "max_hp_loss", 2))
        for call, op, arg in simple:
            if call in s:
                a = split_args(enclosed(s, s.index(call) + len(call) - 1))
                pick = re.sub(r'^\w+:\s*', '', a[arg] if len(a) > arg else "")
                ops.append({"op": op, "amt": self.amount(pick)})
                return None
        if "OrbCmd.RemoveSlots(" in s:
            ops.append({"op": "orb_slots", "amt": -1})
            return None
        if "CreatureCmd.Damage(" in s and "Owner.Creature" in s:
            a = split_args(enclosed(s, s.index("CreatureCmd.Damage(") + len("CreatureCmd.Damage")))
            amt = next((self.amount(x) for x in a if "DynamicVars" in x or re.fullmatch(r'\d+m?', x.strip())), "?")
            ops.append({"op": "hp_loss", "amt": amt})
            return None
        if "CreatureCmd.Kill(Owner.Osty)" in s:
            ops.append({"op": "kill_osty"})
            return None
        if m := re.search(r'OrbCmd\.Channel<(\w+)Orb>', s):
            ops.append({"op": "channel", "orb": m[1]})
            return None
        if "OrbCmd.EvokeNext(" in s:
            ops.append({"op": "evoke"})
            return None
        if "OrbCmd.Passive(" in s:
            ops.append({"op": "orb_passive"})
            return None
        if m := re.search(r'(\w+)\.CreateInHand\(([^;]*)', s):
            a = split_args(enclosed(s, s.index(".CreateInHand(") + len(".CreateInHand")))
            count = self.amount(a[1]) if len(a) > 2 and "CombatState" not in a[1] else 1
            ops.append({"op": "create", "card": m[1], "count": count, "to": "Hand"})
            return None
        if m := re.search(r'AddGeneratedCardsToCombat\((\w+), PileType\.(\w+)|AddGeneratedCardToCombat\((.+?), PileType\.(\w+)', s):
            expr, to = (m[1], m[2]) if m[1] else (m[3], m[4])
            made = ({"card": c[1], "count": 1} if (c := re.search(r'CreateCard<(\w+)>', expr)) else
                    self.made.get(expr.strip(), {"card": "random", "count": 1}))
            ops.append({"op": "create", **made, "to": to})
            return None
        if m := re.search(r'CardCmd\.(Exhaust|Discard|Upgrade|Transform)\((?:\w+, )?(\w+)', s):
            act, var = m[1].lower(), m[2]
            w = self.which(var)
            if isinstance(w, dict):
                w["to"] = m[1]
            elif not (act == "upgrade" and w == "generated"):  # upgraded generated cards: the create op covers it
                ops.append({"op": act, "which": w})
            return None
        if m := re.search(r'CardCmd\.AutoPlay\(\w+, (\w+)', s):
            w = self.which(m[1])
            if isinstance(w, dict):
                w["to"] = "Play"
            else:
                ops.append({"op": "autoplay", "which": w})
            return None
        if m := re.search(r'CardPileCmd\.Add\((\w+), PileType\.(\w+)(, CardPilePosition\.Top)?', s):
            w, to = self.which(m[1]), m[2] + ("Top" if m[3] else "")
            if isinstance(w, dict):
                w["to"] = to
            else:
                ops.append({"op": "move", "which": w, "to": to})
            return None
        if re.search(r'\w+Cmd\.\w+', s):
            ops.append({"op": "unknown", "stmt": re.sub(r'\s+', ' ', s)[:120]})
        return None

    def select(self, rhs):
        frm = re.search(r'PileType\.(\w+)\.GetPile', rhs)
        mode = re.search(r'CardSelectorPrefs\.(\w+?)SelectionPrompt|FromHandFor(\w+)', rhs)
        n = re.search(r'new CardSelectorPrefs\([^,]+,\s*(?:\d+,\s*)?([^,)]+)', rhs)
        return {"op": "select", "from": frm[1] if frm else "Hand" if "FromHand" in rhs else "Screen",
                "to": None, "count": self.amount(n[1]) if n else 1,
                "mode": (mode[1] or mode[2]).lower() if mode else "pick"}


def parse_cards(src_dir):
    """{Card: {pool, cost, type, rarity, target, vars, keywords}} for every card in a character/colorless pool."""
    pool_of = {}
    for pool, source in classes(src_dir, "MegaCrit.Sts2.Core.Models.CardPools"):
        for card in re.findall(r'Card<(\w+)>', source):
            pool_of.setdefault(card, pool.replace("CardPool", ""))
    ctor = re.compile(r':\s*base\((-?\d+), CardType\.(\w+), CardRarity\.(\w+), TargetType\.(\w+)')
    with open(os.path.join(src_dir, "MegaCrit.Sts2.Core.Localization.DynamicVars", "DynamicVarSet.cs"), encoding="utf-8") as f:
        prop_to_key = dict(VAR_PROP.findall(f.read()))
    table = {}
    for name, source in classes(src_dir, "MegaCrit.Sts2.Core.Models.Cards"):
        if (m := ctor.search(source)) and name in pool_of:
            table[name] = {"pool": pool_of[name], "cost": int(m[1]), "type": m[2], "rarity": m[3], "target": m[4],
                           "vars": canonical_vars(source),
                           "keywords": sorted(set(re.findall(r'CardKeyword\.(\w+)', kw[1]))) if (
                               kw := re.search(r'CanonicalKeywords =>([^;]+);', source)) else []}
            if sc := re.search(r'CanonicalStarCost => (\d+)', source):
                table[name]["star_cost"] = int(sc[1])  # Regent: costs stars on top of energy
            if orbs := re.findall(r'OrbCmd\.Channel<(\w+)Orb>', source):
                table[name]["channel"] = orbs  # Defect: orbs channeled on play (ponytail: loops count once)
            table[name].update(pile_moves(source))
            if made := created(source):
                table[name]["creates"] = made  # [card, count var or None (= 1)]: cards put into your hand on play
            if "HasTurnEndInHandEffect => true" in source:
                table[name]["turn_end"] = True  # Burn, Beckon, Regret...: acts at our turn end while in hand
            ops, ops_up = parse_ops(source, prop_to_key)
            if ops is not None:
                table[name]["ops"] = ops  # what OnPlay does, in order (solver.play reads these first)
            if ops_up is not None:
                table[name]["ops_upgraded"] = ops_up
    return table


def ops_complete(ops):
    """No unknown/conditional op and every amount known ("calc:" amounts are computed by the game at play time)."""
    return all(o["op"] not in ("unknown", "cond") and ops_complete(o.get("ops", [])) and not any(
        v == "?" or str(v).startswith("calc:") for v in (o.get("amt"), o.get("hits"), o.get("n"), o.get("count")))
        for o in ops)


def ops_coverage(cards):
    """(fully parsed, total, Counter of unknown statements) over cards with an OnPlay."""
    from collections import Counter
    unknown, done, total = Counter(), 0, 0
    for c in cards.values():
        if "ops" not in c:
            continue
        total += 1
        both = c["ops"] + c.get("ops_upgraded", [])
        done += ops_complete(both)

        def scan(ops):
            for o in ops:
                if o["op"] == "unknown":
                    unknown[re.sub(r'\(.*', '(', o["stmt"].replace("await ", ""))] += 1
                elif o["op"] == "cond":
                    unknown["cond: " + o["if"][:50]] += 1
                scan(o.get("ops", []))
        scan(both)
    return done, total, unknown


def parse_acts(src_dir):
    """{Act: {index, encounters, bosses}} from each act's GenerateAllEncounters / BossDiscoveryOrder."""
    acts = {}
    for name, source in classes(src_dir, "MegaCrit.Sts2.Core.Models.Acts"):
        gen = re.search(r'GenerateAllEncounters\(\)\s*\{', source)
        boss = re.search(r'BossDiscoveryOrder =>([^;]+);', source)
        index = re.search(r'int Index => (\d+);', source)
        if gen and boss:
            acts[name] = {"index": int(index[1]) if index else None,
                          "encounters": re.findall(r'Encounter<(\w+)>', enclosed(source, gen.end() - 1)),
                          "bosses": re.findall(r'Encounter<(\w+)>', boss[1])}
    return acts


def parse_encounters(src_dir, acts):
    """{EncounterClass: {class: Monster|Elite|Boss, monsters, acts}} (RoomType is overridden in code)."""
    rx = re.compile(r'RoomType RoomType => RoomType\.(\w+)')
    return {name: {"class": m[1], "monsters": sorted(set(re.findall(r'Monster<(\w+)>', source))),
                   "acts": [a for a, info in acts.items() if name in info["encounters"]]}
            for name, source in classes(src_dir, "MegaCrit.Sts2.Core.Models.Encounters")
            if (m := rx.search(source))}


def parse_relics(src_dir):
    return {name: {"vars": canonical_vars(source), "hooks": sorted(set(HOOK.findall(source))),
                   "rarity": rarity(source)}
            for name, source in classes(src_dir, "MegaCrit.Sts2.Core.Models.Relics")}


def parse_powers(src_dir):
    """{Power: {hooks, type: Buff|Debuff, stack: Counter|Single...}}: the hooks say when a power acts
    (BeforeSideTurnEndEarly = Plating/Regen at our turn end, ModifyHpLostAfterOstyLate = Buffer...)."""
    out = {}
    for name, source in classes(src_dir, "MegaCrit.Sts2.Core.Models.Powers"):
        kind = re.search(r'PowerType Type => PowerType\.(\w+)', source)
        stack = re.search(r'PowerStackType StackType => PowerStackType\.(\w+)', source)
        if kind:
            out[name] = {"hooks": sorted(set(HOOK.findall(source))), "type": kind[1], "stack": stack[1] if stack else None}
    return out


# --- monsters: AI state machines, move numbers, effects ---
ASC = re.compile(r'AscensionHelper\.GetValueIfAscension\(AscensionLevel\.(\w+),\s*(-?\d+),\s*(-?\d+)\)')
INT_PROP = re.compile(r'(?:private|public|protected)(?: override)? (?:static )?(?:int|decimal|float) (\w+) => ([^;]+);')


def enclosed(s, i):
    """Text inside the bracket pair that opens at s[i]."""
    close = {"(": ")", "{": "}"}[s[i]]
    depth, j = 1, i + 1
    while depth and j < len(s):
        depth += (s[j] == s[i]) - (s[j] == close)
        j += 1
    return s[i + 1:j - 1]


def split_args(text):
    """Split on top-level commas."""
    out, depth, cur = [], 0, ""
    for ch in text:
        depth += (ch in "([{") - (ch in ")]}")
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return out + ([cur.strip()] if cur.strip() else [])


def number(expr, props, levels, depth=0):
    """int, {"base", "asc", "at": ascension level} for ascension-scaled numbers, or the raw text."""
    expr = re.sub(r'^\(\)\s*=>\s*', '', expr.strip())  # weight lambdas: () => 0.4f
    if m := re.fullmatch(r'(-?\d+(?:\.\d+)?)[mfMF]?', expr):
        return int(m[1]) if "." not in m[1] else float(m[1])
    if m := ASC.fullmatch(expr):
        return {"base": int(m[3]), "asc": int(m[2]), "at": levels.index(m[1])}
    if expr in props and depth < 3:
        return number(props[expr], props, levels, depth + 1)
    return expr


def method_body(s, name):
    m = re.search(r'(?:Task|void) ' + re.escape(name) + r'\([^)]*\)\s*\{', s)
    return enclosed(s, m.end() - 1) if m else ""


def effects(body, props, levels):
    """Powers applied, block, heals, summons and cards added by one move (or by AfterAddedToRoom)."""
    out = []
    for m in re.finditer(r'PowerCmd\.Apply<(\w+)>\(', body):
        a = split_args(enclosed(body, m.end() - 1))
        if len(a) >= 3:
            out.append({"power": m[1], "on": "self" if a[1] == "Creature" else a[1],
                        "amt": number(a[2], props, levels)})
    for m in re.finditer(r'CreatureCmd\.GainBlock\(', body):
        a = split_args(enclosed(body, m.end() - 1))
        out.append({"block": number(a[1], props, levels) if len(a) > 1 else "?"})
    for m in re.finditer(r'CreatureCmd\.Heal\(', body):
        a = split_args(enclosed(body, m.end() - 1))
        out.append({"heal": number(a[1], props, levels) if len(a) > 1 else "?"})
    out += [{"summon": m[1]} for m in re.finditer(r'CreatureCmd\.Add<(\w+)>', body)]
    for m in re.finditer(r'CardPileCmd\.(\w+)<(\w+)>\(', body):
        a = split_args(enclosed(body, m.end() - 1))  # AddToCombatAndPreview<Dazed>(targets, pile, count, ...)
        out.append({"adds_card": m[2], **({"count": number(a[2], props, levels)}
                                          if m[1].startswith("AddToCombat") and len(a) >= 3 else {})})
    return out


def parse_monsters(src_dir):
    with open(os.path.join(src_dir, "MegaCrit.Sts2.Core.Entities.Ascension", "AscensionLevel.cs"), encoding="utf-8") as f:
        levels = re.findall(r'^\s*(\w+),?\s*$', f.read().split("{", 1)[1], re.M)
    table = {}
    for name, s in classes(src_dir, "MegaCrit.Sts2.Core.Models.Monsters"):
        m = re.search(r'GenerateMoveStateMachine\(\)\s*\{', s)
        if not m:
            continue
        props = dict(INT_PROP.findall(s))
        body = enclosed(s, m.end() - 1)
        var, moves, branches, nxt = {}, {}, {}, {}
        for mm in re.finditer(r'new (MoveState|RandomBranchState|ConditionalBranchState)\(', body):
            inner = enclosed(body, mm.end() - 1)
            a = split_args(inner)
            if not a or not a[0].startswith('"'):
                continue
            sid = a[0].strip('"')
            # Assigned to a variable, or created inline as someone's FollowUpState.
            # Every assignment in the statement gets this state, e.g.
            # x = (Cast)(a.FollowUpState = (b.FollowUpState = new RandomBranchState("RAND")))
            stmt = re.split(r'[;{}]', body[:mm.start()])[-1]
            for lhs in re.finditer(r'(\w+)(\.FollowUpState)?\s*=(?![=>])', stmt):
                if lhs[2]:
                    nxt[var.get(lhs[1], lhs[1])] = sid
                else:
                    var[lhs[1]] = sid
            if init := re.match(r'\s*\{[^}]*FollowUpState = (\w+)', body[mm.end() + len(inner) + 1:]):
                nxt[sid] = init[1]  # resolved to an ID below
            if mm[1] != "MoveState":
                branches[sid] = {"kind": "random" if mm[1][0] == "R" else "conditional", "to": []}
                continue
            fn = a[1] if len(a) > 1 and re.fullmatch(r'\w+', a[1]) else None
            intents = []
            for x in a[2:]:
                if im := re.match(r'new (\w+)Intent\((.*)\)$', x, re.S):
                    intents.append({"kind": im[1], "args": [number(v, props, levels) for v in split_args(im[2])]})
            moves[sid] = {"intents": intents, "effects": effects(method_body(s, fn), props, levels) if fn else []}
        nxt.update({var.get(a, a): b for a, b in re.findall(r'(\w+)\.FollowUpState = (\w+);', body)})
        nxt = {k: var.get(v, v) for k, v in nxt.items()}
        for mm in re.finditer(r'(\w+)\.(AddBranch|AddState)\(', body):
            a = split_args(enclosed(body, mm.end() - 1))
            if var.get(mm[1]) in branches and a:
                rest = a[1:]
                branches[var[mm[1]]]["to"].append(
                    {"to": var.get(a[0], a[0]),
                     "repeat": next((r.split(".")[1] for r in rest if r.startswith("MoveRepeatType.")), None),
                     "args": [number(r, props, levels) for r in rest if not r.startswith("MoveRepeatType.")]})
        init = re.search(r'new MonsterMoveStateMachine\(\w+, (.*)\);', body, re.S)
        start = init[1].strip() if init else ""
        if alias := re.search(re.escape(start) + r' = ([^;]+);', body) if start not in var else None:
            start = alias[1]  # e.g. initialState = (_screamFirst ? a : b)
        hp = [number(props.get(k, "?"), props, levels) for k in ("MinInitialHp", "MaxInitialHp")]
        table[name] = {"hp": hp, "moves": moves, "next": nxt, "branches": branches,
                       "start": var.get(start) or sorted({var[v] for v in re.findall(r'\w+', start) if v in var}),
                       "start_effects": effects(method_body(s, "AfterAddedToRoom"), props, levels)}
    return table


def parse_text(game_dir, tables=("events", "ancients", "cards", "card_keywords", "powers", "relics", "potions", "orbs",
                                   "afflictions", "enchantments")):
    """English option text from SlayTheSpire2.pck (Godot pack v3: header, then a file directory at dir_offset;
    offsets are relative to file_base when flag 2 is set). Keys are "table:key" like the reader's option desc;
    BBCode markup is stripped, {Var} placeholders kept."""
    want = {f"localization/eng/{t}.json": t for t in tables}
    out = {}
    with open(os.path.join(game_dir, "SlayTheSpire2.pck"), "rb") as f:
        f.read(20)
        flags, base, diroff = struct.unpack("<IQQ", f.read(20))
        f.seek(diroff)
        found = {}
        for _ in range(struct.unpack("<I", f.read(4))[0]):
            path = f.read(struct.unpack("<I", f.read(4))[0]).rstrip(b"\0").decode()
            off, size = struct.unpack("<QQ", f.read(16))
            f.read(20)  # md5 + flags
            if path in want:
                found[want[path]] = (off + (base if flags & 2 else 0), size)
        for table, (off, size) in found.items():
            f.seek(off)
            for k, v in json.loads(f.read(size)).items():
                if ".options." in k or k.endswith(".title") or (k.endswith(".description") and table != "events"):
                    out[f"{table}:{k}"] = re.sub(r"\[/?\w+[^\]]*\]", "", v).strip()
    return out


def code_tables(src):
    """Everything that comes from the decompiled code (not the .pck text)."""
    acts = parse_acts(src)
    return {"upgrades": parse_upgrades(src), "potions": parse_potions(src), "relics": parse_relics(src),
            "acts": acts, "encounters": parse_encounters(src, acts), "monsters": parse_monsters(src),
            "cards": parse_cards(src), "powers": parse_powers(src)}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if sys.argv[1].endswith(".json"):  # refresh: rebuild the code tables of an existing file, keep its text/version
        with open(sys.argv[1], encoding="utf-8") as f:
            data = json.load(f)
        data.update(code_tables(sys.argv[2]))
        with open(sys.argv[1], "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        print(f"refreshed {os.path.normpath(sys.argv[1])} (game {data.get('version')}) from {sys.argv[2]}: "
              f"{len(data['cards'])} cards, {len(data['powers'])} powers")
        print_ops_coverage(data["cards"])
        return
    src = sys.argv[2] if len(sys.argv) > 2 else None
    if not src:
        src = tempfile.mkdtemp(prefix="sts2src_")
        dll = os.path.join(sys.argv[1], "data_sts2_windows_x86_64", "sts2.dll")
        subprocess.run(["ilspycmd", "-p", "-o", src, dll], check=True)
    data = {**code_tables(src), "text": parse_text(sys.argv[1])}
    try:  # one file per game build (advisor/gamedata/<version>.json); gamedata.json = latest generated
        with open(os.path.join(sys.argv[1], "release_info.json"), encoding="utf-8") as f:
            data["version"] = json.load(f)["version"]
    except (OSError, ValueError, KeyError):
        data["version"] = None
    advisor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "advisor")
    outs = [os.path.join(advisor, "gamedata.json")]
    if data["version"]:
        os.makedirs(os.path.join(advisor, "gamedata"), exist_ok=True)
        outs.append(os.path.join(advisor, "gamedata", f"{data['version']}.json"))
    for out in outs:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
    unknown = sum(1 for u in data["upgrades"].values() if "other" in u)
    print(f"wrote {', '.join(map(os.path.normpath, outs))} (game {data['version']}): {len(data['upgrades'])} upgrades ({unknown} unparsed), "
          f"{len(data['potions'])} potions, {len(data['relics'])} relics, {len(data['encounters'])} encounters")
    print_ops_coverage(data["cards"])


def print_ops_coverage(cards):
    done, total, unknown = ops_coverage(cards)
    print(f"card effects read from OnPlay: {done}/{total} fully ({100 * done / total:.1f}%); most common gaps:")
    for stmt, n in unknown.most_common(8):
        print(f"  {n:3} {stmt[:100]}")


if __name__ == "__main__":
    main()
