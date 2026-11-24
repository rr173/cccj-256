# eqcheck — offline adjacent-step swap reviewer

An offline checker for supply-data cleaning recipes.  Given a recipe and one
pair of adjacent steps, it asks: **for every row the input contract allows,
are the delivered values of good rows and the first isolation reason of bad
rows unchanged if those two steps are swapped?**  The answer is one of
`EQUIVALENT`, `COUNTEREXAMPLE`, or `UNDETERMINED` — never "looked the same on
the samples".

Reasoning is *symbolic* over the declared integer intervals (Z3, QF_LIA).
No value enumeration is performed, so an interval of width 10¹⁸ or 10³⁰ costs
the same as a width of 3.

## Quick start

```bash
# Review one adjacent pair (0-based index of the first step)
python3 -I eqcheck/run.py check samples/recipe_swap_bug.json --swap 0
# -> COUNTEREXAMPLE, writes cex.<recipe>.<idx>.json and prints a replay command

# Review every adjacent pair
python3 -I eqcheck/run.py check samples/recipe_commuting.json

# Re-run a saved counterexample; works with no solver / no review state
python3 -I eqcheck/run.py replay /abs/path/cex.recipe_swap_bug.0.json
# or the standalone entry point:
python3 -I eqcheck/replay.py /abs/path/cex.recipe_swap_bug.0.json
```

Exit codes for `check`: `0` all reviewed pairs equivalent, `1` at least one
counterexample, `2` undetermined / empty contract / other non-verdict,
`3` recipe format error.

## Recipe format

A recipe is a JSON object.  Full normative description is the docstring of
`eqcheck/model.py`; the shape is:

```json
{
  "format": "supply-clean-recipe",
  "version": 1,
  "columns": [
    {"name": "id",     "type": "int",  "min": 0, "max": 1000000000000000000, "nullable": false},
    {"name": "age",    "type": "int",  "min": 0, "max": 130, "nullable": true},
    {"name": "region", "type": "enum", "values": ["EU", "US", "APAC"], "nullable": true}
  ],
  "input_contract": {"all": [
    {"column": "id", "op": "gt", "value": 0},
    {"any": [{"is_null": "age"}, {"column": "age", "op": "ge", "value": 18}]}
  ]},
  "steps": [
    {"kind": "isolate", "id": "s0", "when": {"column": "region", "op": "eq", "value": "EU"},
     "reason": "eu_not_allowed"},
    {"kind": "set",     "id": "s1", "when": {"column": "age", "op": "lt", "value": 18},
     "target": "region", "value": "APAC"},
    {"kind": "set_null","id": "s2", "when": {"is_null": "age"}, "target": "region"},
    {"kind": "copy",    "id": "s3", "when": {"const": true}, "source": "age", "target": "age2"}
  ],
  "deliver": ["id", "age", "region"]
}
```

Key points:

- **Column types** are exactly `int` or an explicit `enum` set of strings;
  every column declares `nullable`.  Every `int` column declares a finite
  inclusive `[min, max]`; intervals may be arbitrarily wide.
- **Conditions** are arbitrary boolean combinations (`all`, `any`, `not`,
  `const`) of null tests and comparisons.  Comparisons are *same-type*:
  int↔int (including int↔int constant), enum↔enum (only with identical
  declared value sets) or enum↔declared string.  Ordering ops (`lt/le/gt/ge`)
  are restricted to int columns and are meaningful only when both sides are
  non-null.  **Outside an explicit null test, any comparison with a null side
  is false** (`ne` included).
- **Steps** operate on one column and run only when their `when` condition
  holds: `set` (constant, typed and in-range), `set_null` (target must be
  nullable), `copy` (same type; source/target enum sets must match; may write
  null into a nullable target only), or `isolate` with a named reason.
- **Copy type errors.**  A copy whose target interval is narrower than the
  source's, or whose nullable source may supply null, can *actually* produce
  an invalid value for some rows.  Such a row is isolated with the step's own
  fixed reason `type:<step-id>`.  (Statically impossible copies — enum set
  mismatch, `set_null` into a non-nullable column — are rejected at load time
  with a clear format error instead of being silently ignored.)
- **Isolation stops the row.**  No later step runs.  An isolated row's final
  result is *only* its first reason; its transient column contents are never
  compared.  A good row's result is the tuple of `deliver` columns.
- No aggregation or cross-row operators exist; unknown keys/kinds are format
  errors, so the language cannot silently grow.

### What is compared

Both runs use the **same input row**.  Prefix steps (before the pair) and
suffix steps (after it) run normally in both orders.  The difference checked
is exactly:

- both good → `deliver` tuples equal;
- both isolated → first reason strings equal;
- exactly one isolated → different by definition.

### Input-stage validation (shared, not permuted)

Every raw row must match the declared types (int in its interval, enum in its
set) and satisfy `input_contract`; this single input stage runs before the
steps in both pipelines and is not one of the permutable steps.  Violations
get `input:contract` / `input:type:<col>`.  Because that stage is identical in
both orders, it can never itself be the source of a reported difference.

## Outcomes

- `EQUIVALENT` — solver proved no legal row differs.  Only reported after a
  separate satisfiability check confirmed the contract admits at least one
  legal row.
- `COUNTEREXAMPLE` — one concrete legal row is reported with both final
  results, and validated by the reference interpreter before being emitted.
- `EMPTY_CONTRACT` — the contract plus type constraints are unsatisfiable.
  Reported separately and never as a pass.
- `UNDETERMINED` — the solver budget ran out (or an input-size cap was hit).
  Never reported as equivalent.
- `ERROR` — malformed recipe, unknown keys, out-of-range swap index, or an
  internal inconsistency (solver candidate rejected by the reference
  interpreter / engine-format mismatch).

## Counterexample replay

Each counterexample file is self-contained: it embeds the full recipe object,
the swap index, and the raw input row.  `replay` re-executes both orders with
the pure-Python reference interpreter (`eqcheck/model.py`) and reports whether
the difference reproduces, with no solver and no review artifact needed:

```bash
python3 -I eqcheck/run.py replay cex.json      # exit 1 reproduced, 0 no longer reproduces, 2 error
```

If the recipe file and row are also available, `replay.py --eval RECIPE ROW
[--swap N]` evaluates a row directly.

## Resource limits (chosen, and reported in every result)

| limit | default | meaning |
|---|---|---|
| `solver_timeout_ms` | 30 000 | wall-clock budget per Z3 check |
| `solver_rlimit` | 40 000 000 | Z3 deterministic work budget |
| `max_steps` | 64 | recipe steps |
| `max_columns` | 64 | declared columns |
| `max_condition_nodes` | 4 000 | total condition AST nodes |

All are recorded in the output under `"limits"`.  Any budget exhaustion yields
`UNDETERMINED`; a run that exhausts its budget never claims equivalence.
Tune with `--timeout-ms`, `--rlimit`.

## Solver dependency

Z3 5.4.1 is vendored in `.tools/` (runtime files only) and imported lazily.
`replay` does not need it.  If `.tools/` is absent and no system `z3` is
installed, `check` returns `ERROR` ("cannot load solver") rather than guessing.

## Tests

```bash
python3 -m unittest discover -s tests -v   # unit + CLI + replay tests
python3 tests/fuzz.py 400                   # differential fuzz vs exhaustive
                                            # enumeration on small domains
```

The fuzz harness generates random recipes over tiny finite domains, computes
the ground truth by enumerating every legal row with the reference
interpreter, and asserts the solver's verdict and its reported counterexample
row both agree.

## Layout

```
eqcheck/model.py    recipe parse/validate + reference interpreter (no deps)
eqcheck/engine.py   symbolic encoding, vacuity check, counterexample extraction
eqcheck/run.py       CLI: check / replay
eqcheck/replay.py    solver-free standalone replay entry point
samples/             example recipes (commuting, counterexample, empty, ...)
tests/               unit tests and differential fuzzer
```
