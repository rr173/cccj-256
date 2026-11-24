"""Differential fuzz: symbolic engine vs exhaustive reference enumeration.

On small finite input domains we can enumerate every legal input row and
compare the two step orders with the reference interpreter.  The engine's
verdict must agree: EQUIVALENT iff no row differs, COUNTEREXAMPLE iff some
row differs, EMPTY_CONTRACT iff no legal row exists.

This validates the SMT encoding (including null semantics, copy type errors,
first-reason selection, and deliver comparison) against the source of truth.

    python3 tests/fuzz.py [N] [--seed S]
"""
import itertools
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from eqcheck.engine import Limits, review  # noqa: E402
from eqcheck.model import RecipeError, eval_cond, parse_recipe  # noqa: E402

OPS = ["eq", "ne", "lt", "le", "gt", "ge"]


def rand_cond(rng, cols, depth=0):
    choices = ["cmp", "is_null"]
    if depth < 2:
        choices += ["all", "any", "not", "const"]
    k = rng.choice(choices)
    if k == "const":
        return {"const": rng.choice([True, False])}
    if k == "is_null":
        return {"is_null": rng.choice(list(cols))}
    if k in ("all", "any", "not"):
        if k == "not":
            return {"not": rand_cond(rng, cols, depth + 1)}
        n = rng.randint(0, 3)
        return {k: [rand_cond(rng, cols, depth + 1) for _ in range(n)]}
    name = rng.choice(list(cols))
    c = cols[name]
    if c.type == "int":
        op = rng.choice(OPS)
        v = rng.randint(c.min - 2, c.max + 2)
        return {"column": name, "op": op, "value": v}
    op = rng.choice(["eq", "ne"])
    return {"column": name, "op": op, "value": rng.choice(c.values)}


def rand_recipe(rng):
    n = rng.randint(2, 3)
    columns = []
    for i in range(n):
        nullable = rng.random() < 0.5
        if rng.random() < 0.6:
            lo = rng.choice([0, -1, 1])
            hi = lo + rng.randint(1, 3)
            columns.append({"name": f"i{i}", "type": "int", "min": lo, "max": hi,
                            "nullable": nullable})
        else:
            vals = rng.sample(["a", "b", "c"], rng.randint(2, 3))
            columns.append({"name": f"e{i}", "type": "enum", "values": vals,
                            "nullable": nullable})
    cols = parse_recipe_cols(columns)
    contract = rand_cond(rng, cols)
    steps = []
    for s in range(rng.randint(2, 3)):
        kind = rng.choice(["set", "set_null", "copy", "isolate"])
        when = rand_cond(rng, cols)
        sid = f"s{s}"
        target = rng.choice(list(cols))
        if kind == "set_null" and not cols[target].nullable:
            kind = "set"
        if kind == "copy":
            srcs = [c for c in cols if cols[c].type == cols[target].type]
            src = rng.choice(srcs)
            if cols[src].type == "enum" and cols[src].values != cols[target].values:
                kind = "set"
            else:
                steps.append({"kind": "copy", "id": sid, "when": when,
                              "source": src, "target": target})
                continue
        if kind == "isolate":
            steps.append({"kind": "isolate", "id": sid, "when": when,
                          "reason": f"r{s}"})
        elif kind == "set_null":
            steps.append({"kind": "set_null", "id": sid, "when": when,
                          "target": target})
        else:
            t = cols[target]
            if t.type == "int":
                v = rng.randint(t.min, t.max)
            else:
                v = rng.choice(t.values)
            steps.append({"kind": "set", "id": sid, "when": when,
                          "target": target, "value": v})
    deliver = rng.sample(list(cols), rng.randint(1, len(cols)))
    return {
        "format": "supply-clean-recipe", "version": 1,
        "columns": columns, "input_contract": contract,
        "steps": steps, "deliver": deliver,
    }


def parse_recipe_cols(columns):
    # small helper: reuse the model parser by wrapping in a trivial recipe
    tmp = {"format": "supply-clean-recipe", "version": 1, "columns": columns,
           "input_contract": {"const": True},
           "steps": [{"kind": "isolate", "id": "x", "when": {"const": False},
                      "reason": "x"},
                     {"kind": "set", "id": "y", "when": {"const": False},
                      "target": columns[0]["name"],
                      "value": (columns[0]["min"] if columns[0]["type"] == "int"
                                else columns[0]["values"][0])}],
           "deliver": [columns[0]["name"]]}
    try:
        return parse_recipe(tmp).cols
    except RecipeError:
        return None


def domain(cols):
    doms = {}
    for name, c in cols.items():
        if c.type == "int":
            vals = [v for v in range(c.min, c.max + 1)]
        else:
            vals = list(c.values)
        if c.nullable:
            vals = vals + [None]
        doms[name] = vals
    return doms


def all_rows(cols):
    names = list(cols)
    d = domain(cols)
    return (dict(zip(names, combo)) for combo in itertools.product(*(d[n] for n in names)))


def brute(recipe, i):
    legal = 0
    diff = None
    swapped = recipe.with_swapped(i)
    for row in all_rows(recipe.cols):
        if not eval_cond(recipe.contract, row):
            continue
        legal += 1
        a = recipe.run(row)
        b = swapped.run(row)
        if a != b and diff is None:
            diff = (row, a, b)
    return legal, diff


def main(argv):
    n = int(argv[0]) if argv and argv[0].isdigit() else 300
    seed = 1234
    if "--seed" in argv:
        seed = int(argv[argv.index("--seed") + 1])
    rng = random.Random(seed)
    limit = Limits(timeout_ms=10_000)
    bad = 0
    for t in range(n):
        obj = rand_recipe(rng)
        try:
            recipe = parse_recipe(obj)
        except RecipeError as e:
            print(f"[{t}] generator produced invalid recipe: {e}")
            bad += 1
            continue
        for i in range(len(recipe.steps) - 1):
            legal, diff = brute(recipe, i)
            res = review(obj, i, limits=limit)
            if legal == 0:
                expected = "EMPTY_CONTRACT"
            elif diff is not None:
                expected = "COUNTEREXAMPLE"
            else:
                expected = "EQUIVALENT"
            ok = res.status == expected
            if expected == "COUNTEREXAMPLE" and res.status == "COUNTEREXAMPLE":
                row = res.extra["row"]
                # reported row must be legal and actually differ
                if not eval_cond(recipe.contract, row):
                    ok = False
                elif recipe.run(row) == recipe.with_swapped(i).run(row):
                    ok = False
            if not ok:
                bad += 1
                print(f"MISMATCH test {t} swap {i}: engine={res.status} brute={expected}")
                print(json.dumps(obj, indent=1))
                if diff:
                    print("brute diff row:", diff)
    print(f"checked {n} random recipes; mismatches={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
