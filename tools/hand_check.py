"""Check the "next hand" prediction against recorded fights (advisor/recordings/*.jsonl, newest file by default):
for each turn, the last recorded state's predicted next hand (solver.next_hand, nothing more played) vs the hand the
next turn actually started with. Short-pile turns use the reshuffle replay when the recording has shuffle_rng.
    python tools/hand_check.py [file.jsonl]
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "advisor"))
from solver import Plan, keeps_hand, keywords, next_draw, next_hand  # noqa: E402

path = sys.argv[1] if len(sys.argv) > 1 else max(glob.glob(os.path.join(os.path.dirname(__file__), "..", "advisor",
                                                                         "recordings", "*.jsonl")), key=os.path.getmtime)
turns = {}
for line in open(path, encoding="utf-8"):
    s = json.loads(line)
    if s.get("draw_ordered"):
        turns.setdefault((s.get("encounter"), s["turn"]), []).append(s)
names = lambda cs: sorted(c["name"] + "+" * bool(c.get("upgraded")) for c in cs)
tally = {}
for (enc, t), rows in turns.items():
    if (enc, t + 1) not in turns:
        continue
    last, first = rows[-1], turns[(enc, t + 1)][0]
    short = len(last["draw"]) < next_draw(last)
    kind = ("reshuffle" if last.get("shuffle_rng") else "short (no rng)") if short else "draw pile"
    hand, missing, _ = next_hand(last, Plan([], 0, 0, 0, 0, False, 0.0))
    kept = [c for c in last["hand"] if keeps_hand(last) or "Retain" in keywords(c)]  # retained: not part of the draw
    ok = not missing and names(hand + kept) == names(first["hand"])
    tally.setdefault(kind, [0, 0])[0 if ok else 1] += 1
    if not ok and not missing:
        print(f"{enc} t{t} {kind}: predicted {names(hand)} actual {names(first['hand'])}")
for kind, (ok, bad) in tally.items():
    print(f"{kind}: {ok} match, {bad} miss")
