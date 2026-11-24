"""Standalone replay of a saved counterexample (no solver required).

A counterexample file embeds the recipe and the legal input row, so replaying
needs neither the review run, its output, nor Z3:

    python3 -I eqcheck/replay.py cex.recipe.json  # works even with .tools removed

Also provides a raw evaluation helper:
    python3 -I eqcheck/replay.py --eval RECIPE.json ROW.json [--swap N]

Note: importing eqcheck.engine is done lazily inside cmd_replay so that a
missing solver only affects review, never replay.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from eqcheck.engine import replay_cex, _run_to_json  # reference interpreter only


def _dump(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False))


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def cmd_replay(args):
    if not args:
        print("usage: python3 -I eqcheck/replay.py <counterexample.json>", file=sys.stderr)
        return 2
    cex = _load(args[0])
    res = replay_cex(cex)
    _dump(res.as_dict())
    return 1 if res.status == "COUNTEREXAMPLE" else 2


def cmd_recipe_eval(args):
    """Evaluate a plain input row against a recipe (original or swapped).

    usage: python3 -I eqcheck/replay.py --eval RECIPE.json ROW.json [--swap N]
    """
    import argparse
    ap = argparse.ArgumentParser(prog="eqcheck/replay.py --eval")
    ap.add_argument("recipe")
    ap.add_argument("row")
    ap.add_argument("--swap", type=int, default=None)
    ns = ap.parse_args(args)
    from eqcheck.model import load_recipe_file
    r = load_recipe_file(ns.recipe)
    row = _load(ns.row)
    rr = r.with_swapped(ns.swap) if ns.swap is not None else r
    try:
        out = rr.run(row)
    except Exception as e:  # noqa: BLE001
        _dump({"error": str(e)})
        return 2
    _dump({"result": _run_to_json(out)})
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--eval":
        raise SystemExit(cmd_recipe_eval(argv[1:]))
    raise SystemExit(cmd_replay(argv))
