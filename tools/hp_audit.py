"""HP discipline audit over recorded fights (advisor/recordings): how often the chosen plan takes avoidable damage.

    python tools/hp_audit.py [max states]

Avoidable = another card-only plan takes >= 3 less HP while the chosen one gains <= 2 damage and no kill.
Left out: potion plans (the solver saves potions in hallway fights on purpose) and plans that draw and keep energy
(the autopilot replans after the draw). Also prints mean predicted HP
loss per turn by fight type and how many turns end in LETHAL, so a fix can be checked for side effects."""
import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "advisor"))
from solver import fight_type, solve  # noqa: E402


def potion(p):
    return any(s[5] == "potion" for s in p.steps)


def main(limit=None):
    rec = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "advisor", "recordings", "*.jsonl")
    n, avoidable, lethal, examples = 0, 0, 0, []
    loss = collections.defaultdict(list)
    for path in sorted(glob.glob(rec)):
        for line in open(path, encoding="utf-8"):
            if limit and n >= limit:
                break
            s = json.loads(line)
            try:
                plans = solve(s, top=60)
            except Exception:
                continue
            if not plans:
                continue
            n += 1
            b = plans[0]
            lethal += b.win
            loss[fight_type(s)].append(b.hp_loss)
            if potion(b) or (b.energy_left and any(x[3].get("vars", {}).get("Cards") for x in b.steps)):
                continue  # potions saved on purpose; a draw that keeps energy is replanned after the draw
            better = [p for p in plans if not potion(p) and p.hp_loss <= b.hp_loss - 3]
            if better:
                a = max(better, key=lambda p: p.score)
                if b.damage - a.damage <= 2 and b.kills <= a.kills and not b.win:
                    avoidable += 1
                    if len(examples) < 8:
                        examples.append(f"  {s.get('encounter')}: {[x[0] for x in b.steps]} -{b.hp_loss:g} HP "
                                        f"vs {[x[0] for x in a.steps]} -{a.hp_loss:g} HP")
    print(f"{n} turns · avoidable damage taken in {avoidable} · lethal {lethal}")
    for t, xs in sorted(loss.items()):
        print(f"  {t:8} mean predicted HP loss/turn {sum(xs) / len(xs):.2f} ({len(xs)} turns)")
    print("\n".join(examples))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
