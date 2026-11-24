#!/usr/bin/env python3
"""Self-contained CLI for the offline adjacent-step swap reviewer.

Usage:
    python3 eqcheck/run.py check RECIPE.json --swap N [options]
    python3 eqcheck/run.py check RECIPE.json          # review every adjacent pair
    python3 eqcheck/run.py replay CEX.json            # replay a counterexample

`replay` uses only the standard library plus the reference interpreter in
eqcheck/model.py: the counterexample file embeds both the recipe and the
legal input row, so it can be re-run without the solver or any review state.

Exit codes: 0 = every reviewed pair EQUIVALENT (at least one reviewed),
1 = at least one COUNTEREXAMPLE, 2 = UNDETERMINED / EMPTY_CONTRACT / error,
3 = format error.  UNDETERMINED is never reported as equivalent.
"""
import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from eqcheck.engine import Limits, replay_cex, review  # noqa: E402
from eqcheck.model import RecipeError, load_recipe_file  # noqa: E402


def _emit(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _safe(path):
    base = path.rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", base.rsplit(".", 1)[0])


def _check_file(path, swap, limits):
    try:
        load_recipe_file(path)
        with open(path, "r", encoding="utf-8") as f:
            recipe_obj = json.load(f)
    except RecipeError as e:
        return None, {"status": "ERROR", "detail": str(e)}
    except (OSError, json.JSONDecodeError) as e:
        return None, {"status": "ERROR", "detail": f"cannot read recipe: {e}"}

    recipe = load_recipe_file(path)
    n = len(recipe.steps)
    if n < 2:
        return None, {"status": "ERROR",
                      "detail": "recipe must contain at least two steps to have an adjacent pair"}
    indices = [swap] if swap is not None else list(range(n - 1))
    for i in indices:
        if not (0 <= i < n - 1):
            return None, {"status": "ERROR",
                          "detail": f"swap index {i} out of range 0..{n - 2}"}
    results = []
    for i in indices:
        res = review(recipe_obj, i, limits=limits)
        entry = res.as_dict()
        results.append(entry)
        if res.status == "COUNTEREXAMPLE":
            # Make the artifact path absolute so the replay command printed
            # here works from any working directory.
            cex_path = os.path.abspath(f"cex.{_safe(path)}.{i}.json")
            with open(cex_path, "w", encoding="utf-8") as f:
                json.dump(res.extra["counterexample"], f, ensure_ascii=False, indent=2)
                f.write("\n")
            entry["counterexample_file"] = cex_path
            entry["replay_command"] = (
                f"python3 -I {os.path.join(_HERE, 'run.py')} replay {cex_path}")
    return recipe, results


def cmd_check(argv):
    ap = argparse.ArgumentParser(prog="eqcheck check")
    ap.add_argument("recipe")
    ap.add_argument("--swap", type=int, default=None,
                    help="0-based index of the first of the two adjacent steps")
    ap.add_argument("--timeout-ms", type=int, default=30_000)
    ap.add_argument("--rlimit", type=int, default=40_000_000)
    ns = ap.parse_args(argv)
    limits = Limits(timeout_ms=ns.timeout_ms, rlimit=ns.rlimit)
    recipe, results = _check_file(ns.recipe, ns.swap, limits)
    if recipe is None:
        _emit(results)
        return 3
    _emit({"recipe": ns.recipe, "limits": limits.as_dict(), "results": results})
    # Any non-verdict must never look like success.
    if any(r["status"] == "ERROR" for r in results):
        return 3
    if "COUNTEREXAMPLE" in [r["status"] for r in results]:
        return 1
    if any(r["status"] in ("UNDETERMINED", "EMPTY_CONTRACT") for r in results):
        return 2
    return 0


def cmd_replay(argv):
    ap = argparse.ArgumentParser(prog="eqcheck replay")
    ap.add_argument("counterexample")
    ns = ap.parse_args(argv)
    try:
        with open(ns.counterexample, "r", encoding="utf-8") as f:
            cex = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _emit({"status": "ERROR", "detail": f"cannot read counterexample: {e}"})
        return 3
    res = replay_cex(cex)
    _emit(res.as_dict())
    # 1 = difference reproduced; 2 = stale/inconsistent; 3 = malformed input.
    if res.status == "COUNTEREXAMPLE":
        return 1
    return 2


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "check":
        return cmd_check(rest)
    if cmd == "replay":
        return cmd_replay(rest)
    print(f"unknown command {cmd!r}; use 'check' or 'replay'", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
