"""Replay recorded fights (advisor/recordings/*.jsonl, written by the overlay) through the current solver.

For each turn's first decision state: the solver's best plan and its predicted HP loss, next to the HP
actually lost by the start of the next turn. Run after changing the solver to see if predictions improve:
    python tools/replay.py [file.jsonl ...]
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "advisor"))
from solver import solve  # noqa: E402

files = sys.argv[1:] or sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "advisor", "recordings", "*.jsonl")))
firsts = []  # first state of each (fight, turn), in order
for path in files:
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = json.loads(line)
            if not firsts or (firsts[-1]["encounter"], firsts[-1]["turn"]) != (s.get("encounter"), s["turn"]):
                firsts.append(s)

errors = []
for s, nxt in zip(firsts, firsts[1:] + [None]):
    plan = solve(s)[0]
    same_fight = nxt and nxt.get("encounter") == s.get("encounter") and nxt["turn"] == s["turn"] + 1
    actual = s["player"]["hp"] - nxt["player"]["hp"] if same_fight else None
    if actual is not None:
        errors.append(abs(plan.hp_loss - actual))
    steps = ", ".join(st[0] for st in plan.steps) or "End turn"
    print(f"{s.get('encounter') or '?':<28} t{s['turn']:<2} hp {s['player']['hp']:<3} predicted -{plan.hp_loss:<3} "
          f"actual {'-' + str(actual) if actual is not None else '?':<4} {steps}")
if errors:
    print(f"\n{len(errors)} turns, mean |predicted - actual| HP loss: {sum(errors) / len(errors):.1f}")
