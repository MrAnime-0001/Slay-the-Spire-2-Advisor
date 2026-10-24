"""Icons for the overlay, read from your own game install (SlayTheSpire2.pck), else the StS2 wiki.

The pack holds Godot textures (.ctex, "GST2"): WebP/PNG inside, or raw BC7 (BPTC). Most icons are regions of an
atlas (relic_atlas, potion_atlas, power_atlas, ui_atlas), described by a small .tres file. Icons that can't be read
from the pack are fetched once from the wiki (StS2_<ClassName>.png) in the background and cached in icon_cache/.
The cache holds the game's art from your copy and the wiki: keep it local, never ship or share it.
`python advisor/icons.py` checks coverage."""
import datetime
import io
import json
import os
import re
import struct
import threading
import urllib.parse
import urllib.request

from relics import GAME_DIR

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon_cache")
WIKI = "https://slaythespire.wiki.gg"  # the only host fetched from
MAX_BYTES, TIMEOUT = 512 * 1024, 5
# Monster intents (the reader's kind, e.g. SingleAttackIntent) -> pack icon (images/packed/intents/<name>/ frames,
# or intent_<name>.png for death_blow / hidden)
INTENTS = {"SingleAttack": "attack", "MultiAttack": "attack", "Buff": "buff", "Debuff": "debuff",
           "CardDebuff": "card_debuff", "Defend": "defend", "Escape": "escape", "Heal": "heal", "Sleep": "sleep",
           "Status": "status", "Stun": "stun", "Summon": "summon", "DeathBlow": "death_blow", "Hidden": "hidden"}


def intent_icon(kind):
    return INTENTS.get((kind or "").removesuffix("Intent"), "unknown")


CHARACTERS = ("ironclad", "silent", "defect", "necrobinder", "regent")
BCN = {17: (1, 0.5), 18: (2, 1), 19: (3, 1), 22: (7, 1)}  # Godot image format -> (Pillow bcn type, bytes per pixel)
# Fixed UI icons: name -> pack path
UI = {"hp": "images/atlases/ui_atlas.sprites/top_bar/top_bar_heart.tres",
      "gold": "images/atlases/ui_atlas.sprites/top_bar/top_bar_gold.tres",
      "deck": "images/atlases/ui_atlas.sprites/top_bar/top_bar_deck.tres",
      "map": "images/atlases/ui_atlas.sprites/top_bar/top_bar_map.tres",
      "block": "images/ui/combat/block.png",
      "star": "images/packed/sprite_fonts/star_icon.png"}
# Map room types (pathing) -> map icon
ROOMS = {"Monster": "monster", "Elite": "elite", "Unknown": "unknown", "Shop": "shop", "Treasure": "chest",
         "RestSite": "rest", "Boss": "chest_boss", "Ancient": "unknown"}


def pck_files(path):
    """{path in pack: (offset, size)} from a Godot v3 pack (offsets relative to file_base when flag 2 is set)."""
    out = {}
    with open(path, "rb") as f:
        f.read(20)
        flags, base, diroff = struct.unpack("<IQQ", f.read(20))
        f.seek(diroff)
        for _ in range(struct.unpack("<I", f.read(4))[0]):
            name = f.read(struct.unpack("<I", f.read(4))[0]).rstrip(b"\0").decode()
            off, size = struct.unpack("<QQ", f.read(16))
            f.read(20)  # md5 + flags
            out[name] = (off + (base if flags & 2 else 0), size)
    return out


def snake(name):
    """BurningBlood -> burning_blood (the pack's file names)."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()  # InABottle -> in_a_bottle


class Pack:
    def __init__(self, path=os.path.join(GAME_DIR, "SlayTheSpire2.pck")):
        self.path = path
        self.files = pck_files(path) if os.path.exists(path) else {}
        self.textures = {}  # res path -> decoded atlas/texture (an atlas is decoded once)

    def read(self, name):
        off, size = self.files[name]
        with open(self.path, "rb") as f:
            f.seek(off)
            return f.read(size)

    def texture(self, png):
        """images/x.png -> RGBA image, through its .import file's .ctex."""
        from PIL import Image
        if png not in self.textures:
            imp = self.read(png + ".import").decode()
            b = self.read(re.search(r'path(?:\.\w+)?="res://([^"]+\.ctex)"', imp)[1])
            kind, w, h, _, fmt = struct.unpack("<IHHII", b[36:52])
            data = b[52:]
            if kind in (1, 2):  # PNG / WebP, size-prefixed
                img = Image.open(io.BytesIO(data[4:4 + struct.unpack("<I", data[:4])[0]])).convert("RGBA")
            elif fmt in BCN:  # GPU block compression (DXT1/3/5, BPTC = BC7); first mip only
                n, per_px = BCN[fmt]
                img = Image.frombytes("RGBA", (w, h), data[:int(w * h * per_px)], "bcn", n)
            else:
                raise ValueError(f"{png}: texture format {fmt} not supported")
            self.textures[png] = img
        return self.textures[png]

    def image(self, name):
        """A .png or an atlas sprite .tres from the pack -> RGBA image, or None. A name ending in "*" takes the
        first file with that prefix (Yummy Cookie has one sprite per character; intents are animation frames)."""
        if name.endswith("*"):
            name = next((k.removesuffix(".import") for k in sorted(self.files) if k.startswith(name[:-1])
                         and k.endswith((".tres", ".png.import"))), name)
        if name not in self.files and name + ".import" not in self.files:
            return None
        if name.endswith(".tres"):
            tres = self.read(name).decode()
            atlas = re.search(r'path="res://([^"]+)"', tres)[1]
            x, y, w, h = (int(float(v)) for v in re.search(r"region = Rect2\(([^)]+)\)", tres)[1].split(","))
            return self.texture(atlas).crop((x, y, x + w, y + h))
        return self.texture(name)


def pack_paths(kind, name):
    """Where an icon may live in the pack, best first (kind: relic, potion, power, energy, character, orb, intent,
    room, ui). Beta-only relics sit outside the atlas (images/relics/beta/)."""
    s = snake(name)
    if kind == "relic":
        return [f"images/atlases/relic_atlas.sprites/{s}.tres", f"images/relics/{s}.png", f"images/relics/beta/{s}.png",
                f"images/atlases/relic_atlas.sprites/{s}_*"]
    if kind == "potion":
        return [f"images/atlases/potion_atlas.sprites/{s}.tres", f"images/potions/{s}.png"]
    if kind == "power":  # beta-only powers (Underworld) sit outside the atlas
        return [pack_path(kind, name), f"images/powers/{s}.png", f"images/powers/beta/{s}.png"]
    if kind == "intent":  # attack, buff, debuff, defend, ...: the first animation frame
        return [f"images/packed/intents/{name}/*", f"images/packed/intents/intent_{name}.png"]
    return [p] if (p := pack_path(kind, name)) else []


def pack_path(kind, name):
    if kind == "power":
        return f"images/atlases/power_atlas.sprites/{snake(name)}.tres"
    if kind == "energy":
        return f"images/packed/sprite_fonts/{name.lower()}_energy_icon.png"
    if kind == "character":
        return f"images/ui/top_panel/character_icon_{name.lower()}.png"
    if kind == "orb":
        return f"images/orbs/{snake(name.removesuffix('Orb'))}_orb.png"
    if kind == "room":
        return f"images/atlases/ui_atlas.sprites/map/icons/map_{ROOMS.get(name, 'unknown')}.tres"
    return UI.get(name)


_pack, _lock, _mem, _fetching = None, threading.Lock(), {}, set()


def _missing():
    try:
        with open(os.path.join(CACHE, "missing.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _web(kind, name):
    """Fetch StS2_<Name>.png from the wiki once a day at most; store it in the cache. Runs in a background thread."""
    from PIL import Image
    key, today = f"{kind}/{name}", datetime.date.today().isoformat()
    try:
        api = (f"{WIKI}/api.php?action=query&format=json&prop=imageinfo&iiprop=url&titles="
               + urllib.parse.quote(f"File:StS2_{name}.png"))
        with urllib.request.urlopen(api, timeout=TIMEOUT) as r:
            pages = json.loads(r.read(MAX_BYTES))["query"]["pages"]
        url = next((p["imageinfo"][0]["url"] for p in pages.values() if p.get("imageinfo")), None)
        if not url or urllib.parse.urlparse(url).scheme != "https" or not (
                urllib.parse.urlparse(url).hostname or "").endswith("wiki.gg"):
            raise LookupError("no image")
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            data = r.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("too big")
        img = Image.open(io.BytesIO(data))
        if img.format not in ("PNG", "WEBP"):
            raise ValueError(img.format)
        os.makedirs(os.path.join(CACHE, kind), exist_ok=True)
        img.convert("RGBA").save(os.path.join(CACHE, kind, name + ".png"))
    except Exception:  # offline, not on the wiki, bad file: try again tomorrow
        with _lock:
            miss = _missing()
            miss[key] = today
            os.makedirs(CACHE, exist_ok=True)
            with open(os.path.join(CACHE, "missing.json"), "w", encoding="utf-8") as f:
                json.dump(miss, f)
    finally:
        _fetching.discard(key)


def get(kind, name, online=True):
    """RGBA image for an icon, or None (yet). Pack first; then the cached wiki copy; else starts a wiki fetch
    (relic/potion/power only) and returns None, so the caller draws a stand-in until it lands."""
    global _pack
    from PIL import Image
    key = f"{kind}/{name}"
    if key in _mem:
        return _mem[key]
    if _pack is None:
        _pack = Pack()
    img = None
    for p in pack_paths(kind, name):
        try:
            img = _pack.image(p)
        except Exception:
            img = None
        if img is not None:
            break
    cached = os.path.join(CACHE, kind, name + ".png")
    if img is None and os.path.exists(cached):
        img = Image.open(cached).convert("RGBA")
    if img is None and online and kind in ("relic", "potion", "power") and key not in _fetching \
            and _missing().get(key) != datetime.date.today().isoformat():
        _fetching.add(key)
        threading.Thread(target=_web, args=(kind, name), daemon=True).start()
        return None  # not memoised: picked up from the cache on a later call
    if img is not None or key not in _fetching:
        _mem[key] = img
    return img


def fit(img, size):
    """Scale an icon to fit size x size (keeps aspect), on a transparent square."""
    from PIL import Image
    img = img.copy()
    img.thumbnail((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size))
    out.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
    return out


if __name__ == "__main__":  # coverage self-check (pack only, no downloads)
    from relics import GAMEDATA
    for kind, names in (("relic", GAMEDATA["relics"]), ("potion", GAMEDATA["potions"]),
                        ("power", GAMEDATA.get("powers", {}))):
        miss = [n for n in names if get(kind, n, online=False) is None and not n.startswith("Deprecated")
                and n != "VakuuCardSelector"]  # placeholder classes, not real items
        print(f"{kind}: {len(names) - len(miss)}/{len(names)} from the pack; missing: {', '.join(miss[:12])}"
              + (" ..." if len(miss) > 12 else ""))
    fixed = [(k, n) for k, ns in (("ui", UI), ("energy", CHARACTERS), ("character", CHARACTERS), ("room", ROOMS),
                                  ("intent", sorted(set(INTENTS.values()) | {"unknown"})),
                                  ("orb", ("Lightning", "Frost", "Dark", "Plasma", "Glass"))) for n in ns]
    bad = [f"{k}/{n}" for k, n in fixed if get(k, n, online=False) is None]
    print(f"ui/energy/character/room/intent/orb: {len(fixed) - len(bad)}/{len(fixed)}; missing: {', '.join(bad)}")
    assert get("relic", "BurningBlood", online=False) is not None, "BC7 atlas decode"
    assert get("potion", "FirePotion", online=False) is not None, "WebP decode"
