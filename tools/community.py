"""Community stats from Spire Codex (spire-codex.com public API, ~2M submitted runs), kept local.
    python -I tools/community.py        -> advisor/community.json (+ a short report)
Per encounter: death rate, average HP lost and turns, overall and per character. Per character, per card / relic:
the API's `lift` (win rate minus those players' own expected win rate, floor-adjusted, percentage points; null
under 20 seats). The picker adds it to your own run history's lift (picker.learned), capped like that.
The download is untrusted input: parsed as JSON only, ids mapped to gamedata class names, numbers type-checked.
Rate limit: 300 requests/minute per endpoint without a key; this makes ~12. Refresh after a game patch.
"""
import glob
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script dir; learn.py sits next to us
from learn import class_names  # noqa: E402

API = "https://spire-codex.com/api"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "advisor", "community.json")
CHARACTERS = ("IRONCLAD", "SILENT", "DEFECT", "NECROBINDER", "REGENT")


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "sts2-advisor (local, personal use)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def fight(row):
    total, fatal = num(row.get("total")), num(row.get("fatal"))
    if not total or fatal is None:
        return None
    return {"n": int(total), "death": round(fatal / total, 4), "dmg": num(row.get("avg_damage")),
            "turns": num(row.get("avg_turns"))}


def encounters(names):
    out, page = {}, 1
    while True:
        d = get(f"/runs/encounter-stats?limit=200&page={page}")
        for row in d.get("encounters", []):
            name = names.get(str(row.get("encounter_id", "")))
            f = fight(row)
            if not name or not f:
                continue
            f["by_char"] = {str(c.get("character", "")).title(): fc for c in row.get("characters", [])
                            if (fc := fight(c))}
            out[name] = f
        if not d.get("has_next") or page > 10:
            return out
        page += 1


def lifts(kind, names, character):
    d = get(f"/runs/metrics/{kind}?character={character}")
    rows = d.get("rows") if isinstance(d.get("rows"), list) else next(
        (v for v in d.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [])
    out = {}
    for row in rows:
        name, lift = names.get(str(row.get("id", ""))), num(row.get("lift"))
        if name and lift is not None and not row.get("upgraded"):
            out[name] = round(lift / 100, 4)  # percentage points -> fraction, like learn.py's lift
    return out


def main():
    names = class_names()  # GAME_ID -> ClassName over every gamedata build
    enc_names = {}
    for f in glob.glob(os.path.join(os.path.dirname(OUT), "gamedata", "*.json")):
        for n in json.load(open(f, encoding="utf-8")).get("encounters", {}):
            enc_names[re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", n).upper()] = n
    data = {"fetched": time.strftime("%Y-%m-%d"), "source": API, "encounters": encounters(enc_names), "chars": {}}
    for ch in CHARACTERS:
        data["chars"][ch.title()] = {"cards": lifts("cards", names["cards"], ch),
                                     "relics": lifts("relics", names["relics"], ch)}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=0)
    e = data["encounters"]
    print(f"{len(e)} encounters, " + ", ".join(f"{ch}: {len(v['cards'])} cards / {len(v['relics'])} relics"
                                           for ch, v in data["chars"].items()) + f" -> {OUT}")
    for n, f in sorted(e.items(), key=lambda kv: -kv[1]["death"])[:5]:
        print(f"  {n}: {f['death']:.1%} deaths, {f['dmg']} HP, {f['turns']} turns")


if __name__ == "__main__":
    main()
