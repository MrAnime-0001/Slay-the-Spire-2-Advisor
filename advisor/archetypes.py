"""Character builds (archetypes) and how committed a deck is to each.

A card belongs to an archetype when it's one of the build's named core cards (web guides, checked against the
v0.107.1 card list) or its game text matches the build's keyword. A deck's commitment to a build is how many
of its cards belong to it; picks get a synergy bonus for builds the deck already leans into, so a half-built
Poison deck takes the next poison card over a generic good card. Research: .planning/research/builds.md.
"""
import re
from functools import lru_cache

from relics import GAMEDATA

# {character: {build: (text regex, core cards, key relics)}}. Core cards count double for commitment.
ARCHETYPES = {
    "Ironclad": {
        "Strength": (r"Strength", ["DemonForm", "Inflame", "SwordBoomerang", "TwinStrike", "Whirlwind", "FightMe"],
                     ["Anchor", "HornCleat", "RuinedHelmet"]),
        "Vulnerable": (r"Vulnerable", ["Bash", "Uppercut"], []),
        "Exhaust": (r"Exhaust", ["FeelNoPain", "DarkEmbrace", "SecondWind", "TrueGrit", "BurningPact", "Corruption"], []),
        "Block": (r"Block", ["BodySlam", "ShrugItOff", "Impervious", "Unmovable", "CrimsonMantle", "FlameBarrier",
                             "BloodWall", "Armaments"], ["Sai"]),
        "SelfDamage": (r"[Ll]ose \{?\w*\}? ?HP", ["Bloodletting", "Rupture"], []),
    },
    "Silent": {
        "Poison": (r"Poison", ["NoxiousFumes", "DeadlyPoison", "PoisonedStab", "BouncingFlask", "Accelerant",
                               "Envenom", "CorrosiveWave", "BubbleBubble", "Haze", "Mirage"], []),
        "Shiv": (r"Shiv", ["BladeDance", "CloakAndDagger", "Accuracy", "InfiniteBlades"],
                 ["Shuriken", "Kunai", "OrnamentalFan"]),
        "Sly": (r"Sly|[Dd]iscard", ["Acrobatics", "Prepared", "HiddenDaggers", "Survivor", "Tactician", "Reflex",
                                    "HandTrick"], ["Tingsha", "BagOfPreparation", "CentennialPuzzle", "LetterOpener"]),
    },
    "Necrobinder": {
        "Osty": (r"Osty|Summon", ["Bodyguard", "Unleash", "SicEm", "Dirge"], ["BoneFlute", "BoundPhylactery"]),
        "Souls": (r"Soul", ["CaptureSpirit", "GraveWarden", "Dirge", "BorrowedTime"], []),
        "Doom": (r"Doom", ["ReaperForm", "DeathMarch", "TheScythe", "Reave"], []),
    },
    "Regent": {
        "Stars": (r"singleStarIcon|Star", ["Venerate", "Glow", "ShiningStrike", "Devastate", "Reflect", "FallingStar",
                                           "ChildOfTheStars"], []),
        "Forge": (r"Forge|Sovereign Blade", ["SummonForth", "Conqueror", "TheSmith", "Parry", "SwordSage"], []),
    },
    "Defect": {
        "Orbs": (r"Channel|Evoke|Focus|[Oo]rb", ["BallLightning", "ColdSnap", "LightningRod", "FocusedStrike",
                                                 "Glacier", "Chill", "Hailstorm", "Hotfix", "Capacitor", "Defragment",
                                                 "Refract", "ShadowShield", "Tempest"], ["CrackedCore"]),
        "Claw": (r"Claw", ["Claw", "Hologram", "Scrape", "Skim"], []),
    },
}
CORE_WEIGHT = 2
COMMIT_FULL = 8  # commitment points at which a build counts as fully committed
# ponytail: builds are keyword + named-core based; deeper card-to-card combos are left to the tier blend.


def snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()


@lru_cache(maxsize=4096)
def builds_of(name):
    """{build: weight} this card contributes to (core = 2, text match = 1), across its character's builds."""
    card = GAMEDATA["cards"].get(name, {})
    text = GAMEDATA.get("text", {}).get(f"cards:{snake(name)}.description", "")
    out = {}
    for char, builds in ARCHETYPES.items():
        if card.get("pool") not in (char, "Colorless", None):
            continue
        for build, (regex, core, _) in builds.items():
            if name in core:
                out[f"{char}:{build}"] = CORE_WEIGHT
            elif text and re.search(regex, text) and card.get("rarity") not in ("Basic", "Status", "Curse"):
                out[f"{char}:{build}"] = 1
    return out


def commitment(deck, relics=()):
    """{build: 0..1} how far the deck is into each build (cards + key relics)."""
    score = {}
    for c in deck:
        for b, w in builds_of(c["name"]).items():
            score[b] = score.get(b, 0) + w
    names = {r["name"] if isinstance(r, dict) else r for r in relics}
    for char, builds in ARCHETYPES.items():
        for build, (_, _, key) in builds.items():
            if names & set(key):
                score[f"{char}:{build}"] = score.get(f"{char}:{build}", 0) + CORE_WEIGHT
    return {b: min(1.0, s / COMMIT_FULL) for b, s in score.items()}


def synergy(card, deck, relics=()):
    """0..2: how well a card feeds the builds this deck is already committed to (core cards count double)."""
    mine = commitment(deck, relics)
    return max((w * mine.get(b, 0) for b, w in builds_of(card["name"]).items()), default=0.0)


def describe(deck, relics=()):
    """Overlay text: the deck's top builds, e.g. 'Poison 60% · Sly 25%'."""
    top = sorted(commitment(deck, relics).items(), key=lambda kv: -kv[1])[:2]
    return " · ".join(f"{b.split(':')[1]} {v:.0%}" for b, v in top if v >= 0.2)
