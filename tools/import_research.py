"""Import card tiers, combos, anti-synergies and boss tips into advisor/knowledge.json from:
  - Spire-Forge (the user's web advisor): data/*.js -> slaythetierlist tiers (cards[X].stl), SYNERGY_PAIRS (pairs),
    ARCHETYPE_ANTI_SYNERGY (anti), BOSS_TIPS (boss_tips). Read with node's vm module (data files only).
  - Research Notes: sts2-<char>-guide.md "Card Tier Overview" tables -> cards[X].guide.
Names that aren't StS2 cards (StS1 leftovers like Heavy Blade, Catalyst) are dropped and listed.

  python tools/import_research.py [--forge DIR] [--notes DIR]
"""
import argparse
import glob
import json
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ADVISOR = os.path.join(HERE, "..", "advisor")
KNOWLEDGE = os.path.join(ADVISOR, "knowledge.json")
FORGE = r"C:\Users\Yutti Chylong\Documents\GitHub\Spire-Forge"
NOTES = r"C:\Users\Yutti Chylong\OneDrive\Files\sts2-build-advisor EXTRAS\Research Notes"
CHARS = ("Ironclad", "Silent", "Defect", "Necrobinder", "Regent")
GUIDE_TIER = {"Must Pick": "S", "Great": "A", "Situational": "C", "Skip": "D"}
ANTI_BUILD = {"exhaust": "Ironclad:Exhaust", "block": "Ironclad:Block", "osty": "Necrobinder:Osty",
              "doom": "Necrobinder:Doom", "sly": "Silent:Sly", "claw": "Defect:Claw"}
FORGE_FILES = ["cards-ironclad", "cards-silent", "cards-defect", "cards-necrobinder", "cards-regent",
               "cards-colorless", "cards", "builds", "constants", "bossTips"]
DUMP = """const fs = require('fs'), vm = require('vm'), ctx = {};
vm.createContext(ctx);
for (const f of process.argv.slice(1)) vm.runInContext(fs.readFileSync(f, 'utf8').replace(/^(const|let) /gm, 'var '), ctx);
process.stdout.write(JSON.stringify({cards: ctx.ALL_CARDS, pairs: ctx.SYNERGY_PAIRS,
  anti: ctx.ARCHETYPE_ANTI_SYNERGY, tips: ctx.BOSS_TIPS}));"""


def card_ids():
    ids = set()
    for f in glob.glob(os.path.join(ADVISOR, "gamedata", "*.json")) + [os.path.join(ADVISOR, "gamedata.json")]:
        with open(f, encoding="utf-8") as fh:
            ids |= set(json.load(fh)["cards"])
    return {i.lower(): i for i in ids}


def resolver(dropped):
    low = card_ids()

    def find(name, char=None):
        """Card id for a display name; starter cards carry the character suffix (Defend -> DefendNecrobinder)."""
        key = re.sub(r"[^A-Za-z0-9]", "", re.sub(r"\(.*?\)", "", name.rstrip("+"))).lower()
        hit = low.get(key) or next((low[key + c.lower()] for c in ([char] if char else CHARS)
                                    if key + c.lower() in low), None)
        if not hit:
            dropped.add(name.strip())
        return hit
    return find


def forge(path, knowledge, find):
    files = [os.path.join(path, "data", f + ".js") for f in FORGE_FILES]
    data = json.loads(subprocess.run(["node", "-e", DUMP, *files], capture_output=True, text=True,
                                     encoding="utf-8", check=True).stdout)
    n = 0
    for char, cards in data["cards"].items():
        for c in cards:
            if c.get("tier") and not c.get("isUpgraded") and (cid := find(c["name"], char)):
                knowledge["cards"].setdefault(cid, {})["stl"] = c["tier"]
                n += 1
    pairs = [[a, b, p.get("bonus", 10)] for p in data["pairs"] or [] if (a := find(p["a"])) and (b := find(p["b"]))]
    knowledge["pairs"] = pairs
    knowledge["anti"] = {ANTI_BUILD[k]: [cid for name in v if (cid := find(name, ANTI_BUILD[k].split(":")[0]))]
                         for k, v in (data["anti"] or {}).items() if k in ANTI_BUILD}
    with open(os.path.join(ADVISOR, "gamedata.json"), encoding="utf-8") as fh:
        bosses = [e for e, v in json.load(fh)["encounters"].items() if v.get("class") == "Boss"]
    tips = {}
    for name, tip in (data["tips"] or {}).items():
        key = re.sub(r"[^A-Za-z]", "", name.split("#")[0])
        enc = next((e for e in bosses if e.startswith(key) or e.startswith(key.removeprefix("The"))), None)
        if enc:
            tips[enc] = tip
    knowledge["boss_tips"] = tips
    return f"Spire-Forge: {n} tiers, {len(pairs)} pairs, {len(knowledge['anti'])} anti lists, {len(tips)} boss tips"


def notes(path, knowledge, find):
    n = 0
    for f in glob.glob(os.path.join(path, "sts2-*-guide.md")):
        char = os.path.basename(f).split("-")[1].capitalize()
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        if "## Card Tier Overview" not in text:
            continue
        tier = None
        for line in text.split("## Card Tier Overview")[1].split("\n## ")[0].splitlines():
            if m := re.match(r"### (.+)", line):
                tier = GUIDE_TIER.get(m[1].strip())
            elif tier and (m := re.match(r"\|\s*([^|]+?)\s*\|", line)) and m[1] != "Card" and not m[1].startswith("-"):
                if cid := find(m[1], char):
                    knowledge["cards"].setdefault(cid, {})["guide"] = tier
                    n += 1
    return f"Research Notes: {n} guide tiers"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forge", default=FORGE)
    ap.add_argument("--notes", default=NOTES)
    args = ap.parse_args()
    with open(KNOWLEDGE, encoding="utf-8") as fh:
        knowledge = json.load(fh)
    dropped = set()
    find = resolver(dropped)
    for path, step in ((args.forge, forge), (args.notes, notes)):
        print(step(path, knowledge, find) if os.path.isdir(path) else f"skipped (not found): {path}")
    with open(KNOWLEDGE, "w", encoding="utf-8") as fh:
        json.dump(knowledge, fh, indent=1, sort_keys=True)
    print("not StS2 cards (dropped):", ", ".join(sorted(dropped)) or "none")


if __name__ == "__main__":
    main()
