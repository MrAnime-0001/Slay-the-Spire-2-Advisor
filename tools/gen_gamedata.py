"""Extract card upgrades, potion effects, relic numbers and rarities from the game's code into
advisor/gamedata.json. These live in code (OnUpgrade(), CanonicalVars, TargetType overrides),
not in memory, so the advisor needs this table. Re-run after each game patch:

    python tools/gen_gamedata.py "D:\\SteamLibrary\\steamapps\\common\\Slay the Spire 2"

Needs ilspycmd (dotnet tool install -g ilspycmd). Pass an already-decompiled folder as a second
argument to skip the decompile step.
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
HOOK = re.compile(r'public override (?:async )?[\w<>?]+ ((?:Modify|After|Before)\w+)\(')
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
                       "rarity": rarity(source)}
    return table


def parse_cards(src_dir):
    """{Card: {pool, cost, type, rarity, target, vars, keywords}} for every card in a character/colorless pool."""
    pool_of = {}
    for pool, source in classes(src_dir, "MegaCrit.Sts2.Core.Models.CardPools"):
        for card in re.findall(r'Card<(\w+)>', source):
            pool_of.setdefault(card, pool.replace("CardPool", ""))
    ctor = re.compile(r':\s*base\((-?\d+), CardType\.(\w+), CardRarity\.(\w+), TargetType\.(\w+)')
    table = {}
    for name, source in classes(src_dir, "MegaCrit.Sts2.Core.Models.Cards"):
        if (m := ctor.search(source)) and name in pool_of:
            table[name] = {"pool": pool_of[name], "cost": int(m[1]), "type": m[2], "rarity": m[3], "target": m[4],
                           "vars": canonical_vars(source),
                           "keywords": sorted(set(re.findall(r'CardKeyword\.(\w+)', kw[1]))) if (
                               kw := re.search(r'CanonicalKeywords =>([^;]+);', source)) else []}
    return table


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
    out += [{"adds_card": m[1]} for m in re.finditer(r'CardPileCmd\.\w+<(\w+)>', body)]
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


def parse_text(game_dir, tables=("events", "ancients")):
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
                if ".options." in k or k.endswith(".title"):
                    out[f"{table}:{k}"] = re.sub(r"\[/?\w+[^\]]*\]", "", v).strip()
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = sys.argv[2] if len(sys.argv) > 2 else None
    if not src:
        src = tempfile.mkdtemp(prefix="sts2src_")
        dll = os.path.join(sys.argv[1], "data_sts2_windows_x86_64", "sts2.dll")
        subprocess.run(["ilspycmd", "-p", "-o", src, dll], check=True)
    acts = parse_acts(src)
    data = {"upgrades": parse_upgrades(src), "potions": parse_potions(src), "relics": parse_relics(src),
            "acts": acts, "encounters": parse_encounters(src, acts), "monsters": parse_monsters(src),
            "cards": parse_cards(src), "text": parse_text(sys.argv[1])}
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "advisor", "gamedata.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    unknown = sum(1 for u in data["upgrades"].values() if "other" in u)
    print(f"wrote {os.path.normpath(out)}: {len(data['upgrades'])} upgrades ({unknown} unparsed), "
          f"{len(data['potions'])} potions, {len(data['relics'])} relics, {len(data['encounters'])} encounters")


if __name__ == "__main__":
    main()
