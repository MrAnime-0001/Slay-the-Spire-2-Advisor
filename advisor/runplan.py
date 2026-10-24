"""Run plan (v19 Phase B / wiki W8): the build the deck is going for, how committed it is, what it lacks, and how a
card fits it. Shared by picker (card picks, skip bar) and the overlay (reasons).

User's rules (2026-10-21): Act 1 stays open (frontload, AoE, Block; lean to what the cards point at). Commit when 2-3
core cards or a key relic land, at the latest in Act 2. Once committed, off-build cards are penalised by how
committed the deck is ("balanced"), unless they fill a real gap (AoE, Block, draw, scaling). The wiki's
Synergies / Anti-Synergies notes (advisor/wiki.json, tools/import_wiki.py) add pair bonuses with cards in the deck.
"""
import json
import os

from archetypes import ARCHETYPES, CORE_WEIGHT, builds_of, commitment
from relics import GAMEDATA

COMMIT_AT, LATE_COMMIT = 0.4, 0.2  # committed at 40% (2-3 core cards), or 20% from Act 2 on
CORE_FIT, BUILD_FIT, OFF_BUILD, GAP_VALUE = 6, 3, 4, 3
WIKI_PAIR, WIKI_PAIR_CAP, WIKI_ANTI = 1, 3, 2
RELIC_FIT = 6  # a key relic of the target build (Snecko Skull in Poison), half while Act 1 is open
SCALING_BY_ACT = (0.3, 0.8, 1.0)
CHARS = {c.lower(): c for c in ARCHETYPES}

try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki.json"), encoding="utf-8") as _f:
        WIKI = json.load(_f)
except (OSError, ValueError):
    WIKI = {}
WIKI_CARDS = WIKI.get("cards", {})


def character(state):
    """Character name ("Silent"): the reader's field, else the card pool most of the deck comes from."""
    if (state.get("character") or "").lower() in CHARS:
        return CHARS[state["character"].lower()]
    pools = [GAMEDATA["cards"].get(c["name"], {}).get("pool", "") for c in state.get("deck", [])]
    pools = [p for p in pools if p in ARCHETYPES]
    return max(set(pools), key=pools.count) if pools else None


def roles(card):
    """Gaps a card can fill: aoe, block, draw, scaling."""
    v, out = card.get("vars", {}), set()
    if card.get("type") == "Attack" and card.get("target") in ("AllEnemies", "RandomEnemy"):
        out.add("aoe")
    if "Block" in v:
        out.add("block")
    if GAMEDATA["cards"].get(card["name"], {}).get("draws"):  # real draw (Blade Dance's Cards = Shivs made)
        out.add("draw")
    if card.get("type") == "Power":
        out.add("scaling")
    return out


def plan(state):
    """{character, target ("Silent:Poison" or None), level 0..1, committed, act, gaps {gap: 0..1}}."""
    deck, relics = state.get("deck", []), state.get("relics", [])
    char, act = character(state), min(state.get("act", 0), 2)
    mine = {b: v for b, v in commitment(deck, relics).items() if char and b.startswith(char + ":")}
    target, level = max(mine.items(), key=lambda kv: kv[1], default=(None, 0.0))
    held = {r["name"] if isinstance(r, dict) else r for r in relics}
    key_relic = bool(target) and bool(held & set(ARCHETYPES[char][target.split(":")[1]][2]))
    committed = bool(target) and (level >= COMMIT_AT or key_relic or (act >= 1 and level >= LATE_COMMIT))
    have = [roles(c) for c in deck]
    blocks = sum("block" in r for r in have) / max(len(deck), 1)
    gaps = {"aoe": 0.0 if any("aoe" in r for r in have) else 1.0,
            "block": max(0.0, 0.3 - blocks) / 0.3,
            "draw": 0.0 if any("draw" in r for r in have) else 0.5,
            "scaling": 0.0 if any("scaling" in r for r in have) else SCALING_BY_ACT[act]}
    return {"character": char, "target": target, "level": level, "committed": committed, "act": act,
            "gaps": {g: round(x, 2) for g, x in gaps.items() if x > 0}}


def wiki_pairs(name, deck, kind="cards"):
    """(synergy count, anti count): wiki notes linking this card (or relic) and the deck's cards, either direction."""
    names = {c["name"] for c in deck} - {name}
    syn = anti = 0
    for sec, sign in (("synergies", 1), ("anti", -1)):
        mine = {r for _, refs in WIKI.get(kind, {}).get(name, {}).get(sec, []) for r in refs}
        theirs = {n for n in names if any(name in refs for _, refs in WIKI_CARDS.get(n, {}).get(sec, []))}
        k = len((mine & names) | theirs)
        syn, anti = (syn + k, anti) if sign > 0 else (syn, anti + k)
    return syn, anti


def card_fit(card, p, deck):
    """(score delta, short reason) for adding `card` under plan `p`."""
    delta, why = 0.0, []
    target = p["target"]
    build = target.split(":")[1] if target else None
    weight = builds_of(card["name"]).get(target, 0) if target else 0
    scale = 1.0 if p["committed"] else 0.5  # open phase: lean, don't lock in
    if weight == CORE_WEIGHT:
        delta += CORE_FIT * scale
        why.append(f"core: {build}")
    elif weight:
        delta += BUILD_FIT * scale
        why.append(f"fits {build}")
    fills = [g for g in roles(card) if g in p["gaps"]]
    if fills:
        delta += GAP_VALUE * max(p["gaps"][g] for g in fills)
        why.append("fills " + "/".join(sorted(fills)))
    delta += pair_fit(card["name"], deck, why)
    if p["committed"] and not weight and not fills and card["type"] != "Curse":
        delta -= OFF_BUILD * p["level"]
        why.append(f"off-build: dilutes {build}")
    return round(delta, 1), ", ".join(why)


def pair_fit(name, deck, why, kind="cards"):
    """Score from wiki Synergies / Anti-Synergies with the deck's cards; reasons appended to `why`."""
    syn, anti = wiki_pairs(name, deck, kind)
    if syn:
        why.append(f"synergy x{syn}")
    if anti:
        why.append(f"anti-synergy x{anti}")
    return min(WIKI_PAIR_CAP, WIKI_PAIR * syn) - WIKI_ANTI * anti


def relic_fit(name, p, deck):
    """(score delta, short reason) for a relic under plan `p`: key relic of the target build, wiki pairs."""
    delta, why = 0.0, []
    if p["target"] and name in ARCHETYPES[p["character"]][p["target"].split(":")[1]][2]:
        delta += RELIC_FIT * (1.0 if p["committed"] else 0.5)
        why.append(f"key relic: {p['target'].split(':')[1]}")
    delta += pair_fit(name, deck, why, "relics")
    return round(delta, 1), ", ".join(why)


def describe(p):
    """Overlay line: 'Plan: Poison · committed 60% · needs: AoE, draw' or 'Act 1: open · leaning Poison'."""
    needs = ", ".join(g for g, x in sorted(p["gaps"].items(), key=lambda kv: -kv[1]) if x >= 0.3)
    lean = p["target"].split(":")[1] if p["target"] else "none"
    head = (f"Plan: {lean} · committed {p['level']:.0%}" if p["committed"]
            else f"Act {p['act'] + 1}: open · leaning {lean}")
    return head + (f" · needs: {needs}" if needs else "")
