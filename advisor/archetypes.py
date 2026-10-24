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
    # Core cards and relics added in v15 come from the user's Research Notes guides and Spire-Forge BUILD_DATA,
    # kept only where the card exists in StS2 and its game text fits the build (tools/import_research.py notes).
    "Ironclad": {
        "Strength": (r"Strength", ["DemonForm", "Inflame", "SwordBoomerang", "TwinStrike", "Whirlwind", "FightMe",
                                   "Offering"], ["Anchor", "HornCleat", "RuinedHelmet", "Vajra", "PenNib"]),
        "Vulnerable": (r"Vulnerable", ["Bash", "Uppercut"], []),
        "Exhaust": (r"Exhaust", ["FeelNoPain", "DarkEmbrace", "SecondWind", "TrueGrit", "BurningPact", "Corruption",
                                 "FiendFire", "Brand"], []),
        "Block": (r"Block", ["BodySlam", "ShrugItOff", "Impervious", "Unmovable", "CrimsonMantle", "FlameBarrier",
                             "BloodWall", "Armaments", "Barricade"], ["Sai", "Anchor"]),
        "SelfDamage": (r"[Ll]ose \{?\w*\}? ?HP", ["Bloodletting", "Rupture", "Offering", "Inferno", "TearAsunder", "Brand"],
                       ["SelfFormingClay", "DemonTongue", "CentennialPuzzle"]),
        "Strikes": (r"Strike", ["PerfectedStrike", "Hellraiser", "TwinStrike", "PommelStrike"], ["StrikeDummy", "PenNib"]),
    },
    "Silent": {
        "Poison": (r"Poison", ["NoxiousFumes", "DeadlyPoison", "PoisonedStab", "BouncingFlask", "Accelerant",
                               "Envenom", "CorrosiveWave", "BubbleBubble", "Haze", "Mirage", "Burst"], ["SneckoSkull"]),
        "Shiv": (r"Shiv", ["BladeDance", "CloakAndDagger", "Accuracy", "InfiniteBlades", "FanOfKnives", "Finisher"],
                 ["Shuriken", "Kunai", "OrnamentalFan"]),
        "Sly": (r"Sly|[Dd]iscard", ["Acrobatics", "Prepared", "HiddenDaggers", "Survivor", "Tactician", "Reflex",
                                    "HandTrick", "ToolsOfTheTrade", "Untouchable", "CalculatedGamble", "GrandFinale"],
                ["Tingsha", "BagOfPreparation", "CentennialPuzzle", "LetterOpener", "ToughBandages"]),
    },
    "Necrobinder": {
        "Osty": (r"Osty|Summon", ["Bodyguard", "Unleash", "SicEm", "Dirge", "Reanimate", "Invoke", "Rattle"],
                 ["BoneFlute", "BoundPhylactery"]),
        "Souls": (r"Soul", ["CaptureSpirit", "GraveWarden", "Dirge", "BorrowedTime", "Haunt", "SoulStorm", "DeathMarch"],
                  ["FuneraryMask"]),
        "Doom": (r"Doom", ["ReaperForm", "DeathMarch", "TheScythe", "Reave", "Countdown"], []),
    },
    "Regent": {
        "Stars": (r"singleStarIcon|Star", ["Venerate", "Glow", "ShiningStrike", "Devastate", "Reflect", "FallingStar",
                                           "ChildOfTheStars", "SevenStars", "Stardust", "BlackHole", "BigBang",
                                           "Convergence"], []),
        "Forge": (r"Forge|Sovereign Blade", ["SummonForth", "Conqueror", "TheSmith", "Parry", "SwordSage", "SeekingEdge",
                                             "BigBang"], []),
    },
    "Defect": {
        "Orbs": (r"Channel|Evoke|Focus|[Oo]rb", ["BallLightning", "ColdSnap", "LightningRod", "FocusedStrike",
                                                 "Glacier", "Chill", "Hailstorm", "Hotfix", "Capacitor", "Defragment",
                                                 "Refract", "ShadowShield", "Tempest", "Darkness", "MultiCast",
                                                 "Coolheaded", "Storm", "Loop", "BiasedCognition"],
                 ["CrackedCore", "DataDisk", "GoldPlatedCables"]),
        "Claw": (r"Claw", ["Claw", "Hologram", "Scrape", "Skim", "AllForOne", "Feral"], []),
    },
}
CORE_WEIGHT = 2
COMMIT_FULL = 8  # commitment points at which a build counts as fully committed
# ponytail: builds are keyword + named-core based; deeper card-to-card combos are left to the tier blend.


def snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()


@lru_cache(maxsize=4096)
def card_text(name):
    return GAMEDATA.get("text", {}).get(f"cards:{snake(name)}.description", "")


def expand_power(card):
    """Some debuffs sit under a generic "Power" var (Uppercut: "Apply {Power} Weak. Apply {Power} Vulnerable.",
    Shockwave, Expose, Putrefy): name them from the card text so the solver and deck sim apply them."""
    v = card.get("vars", {})
    if "Power" not in v or card.get("target") not in ("AnyEnemy", "AllEnemies"):
        return card
    text = card_text(card["name"])
    named = {key: v["Power"] for word, key in (("Weak", "WeakPower"), ("Vulnerable", "VulnerablePower"))
             if re.search(r"\{Power[^}]*\}[^.]*\b" + word, text)}
    if not named:
        return card
    return {**card, "vars": {**{k: x for k, x in v.items() if k != "Power"}, **named}}


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
