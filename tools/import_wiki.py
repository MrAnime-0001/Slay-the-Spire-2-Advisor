"""Import the scraped StS2 wiki (data.json) into advisor/wiki.json and write .planning/research/wiki-audit.md.

Gamedata (from the game's code) stays the source of numbers. The wiki adds what it lacks: interaction notes
(Synergies / Anti-Synergies / Order), enemy counter-play (Useful Cards), event options, Ancient relic pools.
Every name is resolved to a game class name (both builds); unresolved names are listed in the audit.
wiki.json holds community text: keep it local, like icon_cache/.

  python tools/import_wiki.py [path to data.json]
"""
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ADVISOR = os.path.join(HERE, "..", "advisor")
DUMP = r"C:\Users\Yutti Chylong\Downloads\Slay the Spire 2 Research\data.json"
OUT = os.path.join(ADVISOR, "wiki.json")
AUDIT = os.path.join(HERE, "..", ".planning", "research", "wiki-audit.md")
SECTIONS = {"Synergies": "synergies", "Anti-Synergies": "anti", "Order": "order"}
sys.path.insert(0, ADVISOR)
from icons import snake  # noqa: E402


def gamedata():
    """Merged entries of every build's gamedata: {kind: {class: entry}} and the text table."""
    out, text = {}, {}
    for f in sorted(glob.glob(os.path.join(ADVISOR, "gamedata", "*.json"))):
        with open(f, encoding="utf-8") as fh:
            g = json.load(fh)
        for kind in ("cards", "relics", "potions", "powers", "monsters"):
            out.setdefault(kind, {}).update(g.get(kind, {}))
        text.update(g["text"])
    return out, text


def titles(gd, text):
    """{kind: {english title lowercased: [class names]}} from the game's own text."""
    out = {}
    for kind in ("cards", "relics", "potions", "powers"):
        m = out[kind] = {}
        for cls in gd[kind]:
            t = text.get(f"{kind}:{snake(cls).upper()}.title")
            if t:
                m.setdefault(t.lower(), []).append(cls)
    out["monsters"] = {re.sub(r"[^a-z0-9]", "", c.lower()): [c] for c in gd["monsters"]}
    # the wiki's "Internal name" row (STRIKE_IRONCLAD) is the game's id: exact, beats the title
    out["internal"] = {k: {snake(c).upper(): c for c in gd[k]} for k in ("cards", "relics", "potions", "monsters")}
    for kind in ("events", "ancients"):
        out[kind] = {v.lower(): [k.split(":")[1].removesuffix(".title")] for k, v in text.items()
                     if k.startswith(kind + ":") and k.count(".") == 1 and k.endswith(".title")}
    return out


def section(md, head, level=2):
    """Text under a markdown heading (until the next heading of the same or higher level)."""
    m = re.search(rf"^{'#' * level} {re.escape(head)}\s*$(.*?)(?=^#{{1,{level}}} |\Z)", md, re.M | re.S)
    return m.group(1) if m else ""


def bullets(block):
    """Nested '- ' bullets flattened: 'Vulnerable applied by this card: Increases damage dealt by Bully.'"""
    out, stack = [], []
    for line in block.splitlines():
        m = re.match(r"^(\s*)- (.+)", line)
        if not m:
            continue
        depth, txt = len(m.group(1)) // 2, m.group(2).strip()
        stack = stack[:depth]
        if txt.endswith(":"):
            stack.append(txt[:-1])
        else:
            out.append(": ".join(stack + [txt]))
    return out


def main(path=DUMP):
    with open(path, encoding="utf-8") as f:
        pages = json.load(f)
    gd, text = gamedata()
    names = titles(gd, text)
    known = sorted({t for k in ("cards", "relics", "potions") for t in names[k]}, key=len, reverse=True)
    ref_re = re.compile(r"\b(" + "|".join(map(re.escape, known)) + r")\b", re.I)

    def refs(s):
        return sorted({c for t in ref_re.findall(s) for k in ("cards", "relics", "potions")
                       for c in names[k].get(t.lower(), [])})

    def interactions(md):
        inter = section(md, "Interactions")
        out = {}
        for head, key in SECTIONS.items():
            items = bullets(section(inter, head, 3))
            if items:
                out[key] = [[s, refs(s)] for s in items]
        return out

    wiki, unmapped, audit = {k: {} for k in ("cards", "relics", "potions", "monsters", "events", "ancients")}, {}, []
    folder_kind = {"Cards": "cards", "Relics": "relics", "Potions": "potions", "Events": "events", "Ancients": "ancients"}
    for p in pages:
        kind = "monsters" if p["folder"].startswith("Enemies") else folder_kind.get(p["folder"])
        if not kind:
            continue
        key = re.sub(r"[^a-z0-9]", "", p["name"].lower()) if kind == "monsters" else p["name"].lower()
        m = re.search(r"\| Internal name \| (\w+)", p["text"])
        internal = m.group(1) if m else ""
        cls = names["internal"].get(kind, {}).get(internal)
        char = re.match(r"(.+) \((\w+)\)$", p["name"])  # "Strike (Ironclad)" -> StrikeIronclad
        if not cls and char:
            cls = next((c for c in names[kind].get(char.group(1).lower(), []) if c.endswith(char.group(2))), None)
        classes = [cls] if cls else names[kind].get(key) or (
            [internal] if kind in ("events", "ancients") and f"{kind}:{internal}.title" in text else None)
        if not classes:
            unmapped.setdefault(kind, []).append(p["name"])
            continue
        md, box = p["text"], (p["infoboxes"][0]["fields"] if p["infoboxes"] else {})
        if kind in ("cards", "relics", "potions"):
            entry = interactions(md)
            notes = bullets(section(md, "Notes"))
            if notes:
                entry["notes"] = notes
            spawn = bullets(section(md, "Spawn Restrictions", 3)) or bullets(section(md, "Spawn Restrictions"))
            if spawn:
                entry["spawn"] = spawn
            if kind == "cards" and box.get("EnergyCost"):  # audit: wiki base cost vs the game's
                m = re.match(r"(?:Base: )?(-?\d+|X)", box["EnergyCost"])
                for c in classes:
                    g = gd["cards"][c].get("cost")
                    if m and m.group(1) not in ("X", str(g)) and not (g == -1 and m.group(1) == "X"):
                        audit.append(f"card cost: {c} game {g}, wiki {box['EnergyCost']}")
        elif kind == "monsters":
            entry = {k: v for k, v in (("useful", bullets(section(md, "Useful Cards"))),
                                       ("notes", bullets(section(md, "Notes")))) if v}
            hp = re.match(r"(\d+)(?:-(\d+))?", box.get("HP", ""))
            g = [h.get("base") if isinstance(h, dict) else h for h in gd["monsters"][classes[0]].get("hp") or []]
            if hp and g and str(hp.group(1)) != str(g[0]):  # base min HP (ascension rows aside)
                audit.append(f"monster HP: {classes[0]} game {g}, wiki {box['HP']}")
            # Pattern tables: base "Deals N damage" values vs the game AI's attack intents (first arg, base value)
            wiki_dmg = {int(m) for t in p["tables"] if t["section"] == "Pattern" for r in t["rows"]
                        if "Ascension" not in str(r.get("Effect", "")) for m in re.findall(r"Deals (\d+)", str(r.get("Effect", "")))}
            game_dmg = {a[0].get("base") if isinstance(a[0], dict) else a[0]
                        for mv in gd["monsters"][classes[0]].get("moves", {}).values() for it in mv.get("intents", [])
                        if "Attack" in it.get("kind", "") and (a := it.get("args"))}
            if wiki_dmg and game_dmg and not wiki_dmg <= game_dmg:
                audit.append(f"monster damage: {classes[0]} game {sorted(map(str, game_dmg))}, wiki {sorted(wiki_dmg)}")
        elif kind == "events":
            opts = re.findall(r"^\*\*\[(.+?)\]\*\*\s*(.+)$", section(md, "Options"), re.M)
            entry = {"options": [[o, t.strip()] for o, t in opts]}
            cond = bullets(section(md, "Conditions")) or section(md, "Conditions").strip()
            if cond:
                entry["conditions"] = cond
        else:  # ancients: relic pools by table section
            entry = {}
            for t in p["tables"]:
                pool = [c for r in t["rows"] for c in names["relics"].get(str(r.get("Relic", "")).strip("* ").lower(), [])]
                if pool:
                    entry[t["section"]] = pool
        if entry:
            for c in classes:
                wiki[kind][c] = entry
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"source": path, **wiki}, f, ensure_ascii=False, separators=(",", ":"))
    write_audit(wiki, unmapped, list(dict.fromkeys(audit)), gd, path)  # one wiki page can list several monsters
    print(", ".join(f"{k} {len(v)}" for k, v in wiki.items()), "| unmapped",
          ", ".join(f"{k} {len(v)}" for k, v in unmapped.items()), f"| {len(audit)} mismatches")
    return wiki, unmapped


def write_audit(wiki, unmapped, audit, gd, path):
    sys.path.insert(0, ADVISOR)
    from solver import ENEMY_POWERS, ENGINES, PLAYER_POWERS
    seen = {}
    for f in glob.glob(os.path.join(ADVISOR, "recordings", "*.jsonl")):  # how often each power shows up in fights
        for line in open(f, encoding="utf-8"):
            s = json.loads(line)
            for c in [s.get("player", {})] + [e.get("creature", {}) for e in s.get("enemies", [])]:
                for pw in c.get("powers", []):
                    seen[pw["name"]] = seen.get(pw["name"], 0) + 1
    missing = sorted((n for n in gd["powers"] if n not in PLAYER_POWERS | ENEMY_POWERS | set(ENGINES)), key=lambda n: -seen.get(n, 0))
    lines = ["# Wiki audit (generated by tools/import_wiki.py)", "", f"Source: `{path}`. Gamedata wins on numbers.", "",
             "## Imported", ""] + [f"- {k}: {len(v)}" for k, v in wiki.items()] + [
             "", "## Wiki vs gamedata mismatches", ""] + [f"- {a}" for a in audit or ["none"]] + [
             "", "## Wiki pages not matched to a game name", ""] + [f"- {k}: {', '.join(sorted(v))}" for k, v in unmapped.items()] + [
             "", "## Powers the solver doesn't model (most seen in your recorded fights first)", ""] + [
             f"- {n} ({seen.get(n, 0)} states)" for n in missing]
    with open(AUDIT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:2])
