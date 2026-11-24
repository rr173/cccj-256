"""Symbolic equivalence engine for one adjacent step swap.

All reasoning is exact, over the *whole* (possibly enormous) integer domain:
each input column becomes a pair (is_null, value) of SMT variables and every
step is lifted to quantifier-free linear-integer/boolean formulas.  We never
enumerate values, so a declared interval such as [0, 10**18] costs no more
than [0, 10].

Decision procedure: Z3 (QF_LIA + Bool + ITE).  A counterexample query is:

    legal_input  AND  ( result_original != result_swapped )

where legal_input = input contract AND raw-type validity and "result" is
"first isolation reason" for isolated rows, or the deliver tuple for
successful rows (transient contents of isolated rows are ignored).

Outcomes: EQUIVALENT, COUNTEREXAMPLE, UNDETERMINED (solver budget exhausted),
EMPTY_CONTRACT (no legal input row exists -- never reported as equivalent).
"""

import hashlib
import json
import sys
from typing import Any, Dict, List, Optional

from .model import Column, Cond, Recipe, RecipeError, Step, parse_recipe

# Z3 is vendored under .tools/z3; import it only when the engine is used.
_Z3_PATH = None


def _import_z3():
    global _Z3_PATH
    try:
        import z3  # type: ignore
        return z3
    except (ImportError, AttributeError):
        import os
        import sys as _sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Accept either layout: .tools/z3/__init__.py (package) or
        # .tools/z3/z3/__init__.py.  Insert the directory that *contains*
        # the z3 package so its relative imports resolve.
        for cand in (os.path.join(root, ".tools"),
                     os.path.join(root, ".tools", "z3")):
            if os.path.isfile(os.path.join(cand, "z3", "__init__.py")):
                if cand not in _sys.path:
                    _sys.path.insert(0, cand)
                _Z3_PATH = cand
                break
        import z3  # type: ignore
        return z3


# ---------------------------------------------------------------------------
# Resource limits (documented in README)
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_RLIMIT = 40_000_000
MAX_STEPS = 64
MAX_COLUMNS = 64
MAX_COND_NODES = 4000


class Limits:
    def __init__(self, timeout_ms=DEFAULT_TIMEOUT_MS, rlimit=DEFAULT_RLIMIT,
                 max_steps=MAX_STEPS, max_columns=MAX_COLUMNS,
                 max_cond_nodes=MAX_COND_NODES):
        self.timeout_ms = timeout_ms
        self.rlimit = rlimit
        self.max_steps = max_steps
        self.max_columns = max_columns
        self.max_cond_nodes = max_cond_nodes

    def as_dict(self):
        return {
            "solver_timeout_ms": self.timeout_ms,
            "solver_rlimit": self.rlimit,
            "max_steps": self.max_steps,
            "max_columns": self.max_columns,
            "max_condition_nodes": self.max_cond_nodes,
        }


class ResourceExhausted(Exception):
    pass


def _count_cond(node: Cond) -> int:
    n = 1
    for ch in node.children:
        n += _count_cond(ch)
    return n


def precheck_limits(recipe: Recipe, limits: Limits) -> None:
    if len(recipe.steps) > limits.max_steps:
        raise ResourceExhausted(
            f"recipe has {len(recipe.steps)} steps > max_steps={limits.max_steps}")
    if len(recipe.cols) > limits.max_columns:
        raise ResourceExhausted(
            f"recipe has {len(recipe.cols)} columns > max_columns={limits.max_columns}")
    total = _count_cond(recipe.contract)
    for s in recipe.steps:
        total += _count_cond(s.when)
    if total > limits.max_cond_nodes:
        raise ResourceExhausted(
            f"conditions contain {total} nodes > max_condition_nodes="
            f"{limits.max_cond_nodes}")


# ---------------------------------------------------------------------------
# Outcome types
# ---------------------------------------------------------------------------

class ReviewResult:
    def __init__(self, status: str, detail: str, **kw):
        self.status = status          # EQUIVALENT|COUNTEREXAMPLE|UNDETERMINED|EMPTY_CONTRACT|ERROR
        self.detail = detail
        self.extra = kw

    def as_dict(self):
        d = {"status": self.status, "detail": self.detail}
        d.update(self.extra)
        return d


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------

class _Sym:
    """Symbolic column value: (is_null BoolRef, value IntRef).

    For enums `value` is the index into the declared value tuple.
    """

    __slots__ = ("nul", "val")

    def __init__(self, nul, val):
        self.nul = nul
        self.val = val


class Encoder:
    def __init__(self, recipe: Recipe, limits: Limits):
        self.z3 = _import_z3()
        self.r = recipe
        self.limits = limits
        self.reason_codes: Dict[str, int] = {}
        self.int_input_vars: Dict[str, Any] = {}
        self.enum_index = {name: {v: i for i, v in enumerate(c.values)}
                           for name, c in recipe.cols.items() if c.type == "enum"}
        self._make_inputs()

    # -- reasons ----------------------------------------------------------
    def code(self, reason: str) -> int:
        if reason not in self.reason_codes:
            self.reason_codes[reason] = len(self.reason_codes) + 1
        return self.reason_codes[reason]

    # -- inputs -----------------------------------------------------------
    def _make_inputs(self):
        z3 = self.z3
        self.input_state: Dict[str, _Sym] = {}
        for name, col in self.r.cols.items():
            nul = z3.Bool(f"in_null_{name}")
            val = z3.Int(f"in_val_{name}")
            self.input_state[name] = _Sym(nul, val)

    def legal_input_formula(self):
        """contract AND raw input type validity."""
        z3 = self.z3
        conj = [self.cond_formula(self.r.contract, self.input_state)]
        for name, col in self.r.cols.items():
            s = self.input_state[name]
            if not col.nullable:
                conj.append(z3.Not(s.nul))
            if col.type == "int":
                in_ty = z3.And(z3.IntVal(col.min) <= s.val,
                               s.val <= z3.IntVal(col.max))
            else:
                in_ty = z3.And(z3.IntVal(0) <= s.val,
                               s.val < z3.IntVal(len(col.values)))
            conj.append(z3.Or(s.nul, in_ty))
        return z3.And(*conj)

    # -- conditions -------------------------------------------------------
    def cond_formula(self, node: Cond, state: Dict[str, _Sym]):
        z3 = self.z3
        k = node.kind
        if k == "const":
            return z3.BoolVal(node.const_val)
        if k == "all":
            if not node.children:
                return z3.BoolVal(True)
            return z3.And(*[self.cond_formula(c, state) for c in node.children])
        if k == "any":
            if not node.children:
                return z3.BoolVal(False)
            return z3.Or(*[self.cond_formula(c, state) for c in node.children])
        if k == "not":
            return z3.Not(self.cond_formula(node.children[0], state))
        if k == "is_null":
            return state[node.col].nul
        # cmp: every comparison is false if either side is null
        a = state[node.col]
        col = self.r.cols[node.col]
        if node.operand is not None:
            b = state[node.operand]
            nonnull = z3.And(z3.Not(a.nul), z3.Not(b.nul))
            return z3.And(nonnull, self._cmp(a.val, b.val, node.op))
        if col.type == "int":
            cval = z3.IntVal(node.const_val)
        else:
            cval = z3.IntVal(self.enum_index[node.col][node.const_val])
        nonnull = z3.Not(a.nul)
        return z3.And(nonnull, self._cmp(a.val, cval, node.op))

    def _cmp(self, a, b, op):
        z3 = self.z3
        if op == "eq":
            return a == b
        if op == "ne":
            return z3.Not(a == b)
        if op == "lt":
            return a < b
        if op == "le":
            return a <= b
        if op == "gt":
            return a > b
        if op == "ge":
            return a >= b
        raise AssertionError(op)

    # -- pipeline ---------------------------------------------------------
    def pipeline(self, steps: List[Step]):
        """Return (iso BoolRef, reason IntRef, out_state) for the given order.

        Formula construction is nested ITE over all input variables: cost is
        linear in recipe size, independent of the integer interval width.
        """
        z3 = self.z3
        state = dict(self.input_state)
        iso = z3.BoolVal(False)
        reason = z3.IntVal(0)
        for step in steps:
            cond = self.cond_formula(step.when, state)
            active = z3.And(z3.Not(iso), cond)
            if step.kind == "isolate":
                iso = z3.Or(iso, active)
                reason = z3.If(active, z3.IntVal(self.code(step.reason)), reason)
                continue
            if step.kind == "set":
                col = self.r.cols[step.target]
                if col.type == "int":
                    cval = z3.IntVal(step.value)
                else:
                    cval = z3.IntVal(self.enum_index[step.target][step.value])
                cur = state[step.target]
                state[step.target] = _Sym(
                    z3.If(active, z3.BoolVal(False), cur.nul),
                    z3.If(active, cval, cur.val),
                )
                continue
            if step.kind == "set_null":
                cur = state[step.target]
                state[step.target] = _Sym(
                    z3.If(active, z3.BoolVal(True), cur.nul),
                    cur.val,
                )
                continue
            # copy
            src = state[step.source]
            tgt_col = self.r.cols[step.target]
            cur = state[step.target]
            bad = self._copy_badness(src, tgt_col)
            bad_here = z3.And(active, bad)
            iso = z3.Or(iso, bad_here)
            reason = z3.If(bad_here, z3.IntVal(self.code(step.fixed_reason)), reason)
            write = z3.And(active, z3.Not(bad))
            state[step.target] = _Sym(
                z3.If(write, src.nul, cur.nul),
                z3.If(write, src.val, cur.val),
            )
        return iso, reason, state

    def _copy_badness(self, src: _Sym, tgt_col: Column):
        z3 = self.z3
        parts = []
        if not tgt_col.nullable:
            parts.append(src.nul)
        if tgt_col.type == "int":
            parts.append(z3.And(
                z3.Not(src.nul),
                z3.Not(z3.And(z3.IntVal(tgt_col.min) <= src.val,
                              src.val <= z3.IntVal(tgt_col.max))),
            ))
        # enum copy compatibility is guaranteed at parse time
        if not parts:
            return z3.BoolVal(False)
        return z3.Or(*parts)

    # -- results ----------------------------------------------------------
    def results_differ(self, pipe_a, pipe_b):
        z3 = self.z3
        iso_a, reason_a, out_a = pipe_a
        iso_b, reason_b, out_b = pipe_b
        # exactly one pipeline isolates: the outcomes cannot match
        cases = [z3.Xor(iso_a, iso_b)]
        # both isolated: first reason only
        cases.append(z3.And(iso_a, iso_b, z3.Not(reason_a == reason_b)))
        # both successful: deliver tuple
        comps = []
        for d in self.r.deliver:
            a = out_a[d]
            b = out_b[d]
            same = z3.And(
                z3.Not(z3.Xor(a.nul, b.nul)),
                z3.Or(a.nul, a.val == b.val),
            )
            comps.append(z3.Not(same))
        cases.append(z3.And(z3.Not(iso_a), z3.Not(iso_b), z3.Or(*comps)))
        return z3.Or(*cases)


# ---------------------------------------------------------------------------
# Counterexample extraction
# ---------------------------------------------------------------------------

def _model_row(enc: Encoder, model) -> Dict[str, Any]:
    z3 = enc.z3
    row: Dict[str, Any] = {}
    for name, col in enc.r.cols.items():
        s = enc.input_state[name]
        nul = model.eval(s.nul, model_completion=True)
        if z3.is_true(nul):
            row[name] = None
        else:
            v = model.eval(s.val, model_completion=True)
            try:
                iv = v.as_long()
            except AttributeError:
                iv = int(str(v))
            if col.type == "int":
                row[name] = iv
            else:
                row[name] = col.values[iv]
    return row


def _run_to_json(result):
    status, payload = result
    if status == "isolated":
        return {"outcome": "isolated", "reason": payload}
    return {"outcome": "ok", "deliver": list(payload)}


def _fingerprint(recipe_obj) -> str:
    canon = json.dumps(recipe_obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def review(recipe_obj, swap_index: int, limits: Optional[Limits] = None) -> ReviewResult:
    limits = limits or Limits()
    try:
        recipe = parse_recipe(recipe_obj)
    except RecipeError as e:
        return ReviewResult("ERROR", f"invalid recipe format: {e}")
    if not (0 <= swap_index < len(recipe.steps) - 1):
        return ReviewResult(
            "ERROR",
            f"swap index {swap_index} out of range; recipe has {len(recipe.steps)} "
            f"steps, valid indices are 0..{len(recipe.steps) - 2}",
            limits=limits.as_dict())
    try:
        precheck_limits(recipe, limits)
    except ResourceExhausted as e:
        return ReviewResult(
            "UNDETERMINED",
            f"resource limit exhausted before solving: {e}. "
            f"A resource-limited run cannot establish equivalence.",
            limits=limits.as_dict())
    try:
        enc = Encoder(recipe, limits)
    except Exception as e:  # pragma: no cover - import failure
        return ReviewResult("ERROR", f"cannot load solver: {e}")

    z3 = enc.z3
    legal = enc.legal_input_formula()

    swap = {"index": swap_index,
            "original_order": [recipe.steps[swap_index].id,
                               recipe.steps[swap_index + 1].id],
            "swapped_order": [recipe.steps[swap_index + 1].id,
                              recipe.steps[swap_index].id]}

    # Vacuity check first: never claim a pass over an empty input domain.
    vac = z3.Solver()
    vac.set(timeout=limits.timeout_ms)
    vac.set(rlimit=limits.rlimit)
    vac.add(legal)
    chk = vac.check()
    if chk == z3.unknown:
        return ReviewResult(
            "UNDETERMINED",
            "solver budget exhausted while checking whether the input contract "
            "admits any legal row; no verdict can be given.",
            limits=limits.as_dict(), swap=swap)
    if chk == z3.unsat:
        return ReviewResult(
            "EMPTY_CONTRACT",
            "the input contract admits no legal input row at all (contract AND "
            "column type constraints are unsatisfiable); equivalence is NOT "
            "claimed, this is a vacuous specification.",
            limits=limits.as_dict(), swap=swap)

    steps_a = recipe.steps
    steps_b = recipe.with_swapped(swap_index).steps
    pipe_a = enc.pipeline(steps_a)
    pipe_b = enc.pipeline(steps_b)

    bad = z3.Solver()
    bad.set(timeout=limits.timeout_ms)
    bad.set(rlimit=limits.rlimit)
    bad.add(legal)
    bad.add(enc.results_differ(pipe_a, pipe_b))
    chk = bad.check()
    if chk == z3.unknown:
        return ReviewResult(
            "UNDETERMINED",
            "solver budget exhausted while searching for a counterexample; "
            "absence of a counterexample within the budget is NOT equivalence.",
            limits=limits.as_dict(), swap=swap)
    if chk == z3.unsat:
        return ReviewResult(
            "EQUIVALENT",
            "for every legal input row the first isolation reason and the "
            "delivered values are identical in both step orders.",
            limits=limits.as_dict(), swap=swap)

    # SAT: extract one legal row and confirm with the reference interpreter.
    model = bad.model()
    row = _model_row(enc, model)
    swapped = recipe.with_swapped(swap_index)
    try:
        ref_a = recipe.run(row)
        ref_b = swapped.run(row)
    except RecipeError as e:
        return ReviewResult(
            "ERROR",
            f"solver produced a candidate row rejected by the reference "
            f"interpreter ({e}); this indicates an engine/format mismatch.",
            candidate_row=row)
    if ref_a == ref_b:
        return ReviewResult(
            "ERROR",
            "internal inconsistency: the solver reported a difference but the "
            "reference interpreter produced identical results for the candidate "
            "row. No verdict is given.",
            candidate_row=row,
            original=_run_to_json(ref_a), swapped=_run_to_json(ref_b))
    # The row must be a legal original input (contract + raw types).
    verified = recipe.run(row)
    if verified[0] == "isolated" and verified[1].startswith("input:"):
        return ReviewResult(
            "ERROR",
            "internal inconsistency: candidate row is not a legal contract input "
            f"(reference says {verified[1]}).",
            candidate_row=row)

    a = _run_to_json(ref_a)
    b = _run_to_json(ref_b)
    cex = {
        "tool": "eqcheck/1",
        "recipe_fingerprint": _fingerprint(recipe_obj),
        "swap": swap,
        "recipe": recipe_obj,
        "input_row": row,
        "original": a,
        "swapped": b,
    }
    return ReviewResult(
        "COUNTEREXAMPLE",
        "found one legal input row whose final result differs between the two "
        "adjacent step orders.",
        limits=limits.as_dict(),
        swap=swap,
        row=row,
        original=a,
        swapped=b,
        counterexample=cex,
    )


def replay_cex(cex_obj) -> ReviewResult:
    """Re-run a saved counterexample with the reference interpreter only.

    Self-contained: the recipe object and the input row are embedded in the
    counterexample file, so no review/solver process is needed.
    """
    if not isinstance(cex_obj, dict):
        return ReviewResult("ERROR", "counterexample file must be a JSON object")
    for k in ("recipe", "swap", "input_row", "original", "swapped"):
        if k not in cex_obj:
            return ReviewResult("ERROR", f"counterexample file missing key {k!r}")
    try:
        recipe = parse_recipe(cex_obj["recipe"])
    except RecipeError as e:
        return ReviewResult("ERROR", f"embedded recipe is invalid: {e}")
    sw = cex_obj["swap"]
    idx = sw.get("index") if isinstance(sw, dict) else None
    if not isinstance(idx, int) or not (0 <= idx < len(recipe.steps) - 1):
        return ReviewResult("ERROR", f"invalid swap index in counterexample: {idx!r}")
    row = cex_obj["input_row"]
    swapped = recipe.with_swapped(idx)
    try:
        a = recipe.run(row)
        b = swapped.run(row)
    except RecipeError as e:
        return ReviewResult("ERROR", f"input row does not fit the recipe: {e}")
    a_json = _run_to_json(a)
    b_json = _run_to_json(b)
    differs = a_json != b_json
    # The row must still be a legal original input (contract + raw types).
    legal = not (a[0] == "isolated" and a[1].startswith("input:"))
    if differs and legal:
        status = "COUNTEREXAMPLE"
        detail = "replay reproduced the difference"
    elif differs:
        status = "ERROR"
        detail = ("replay produced a difference, but the row is NOT a legal "
                  "contract input, so this is not a valid counterexample")
    else:
        status = "ERROR"
        detail = ("replay did NOT reproduce a difference with the embedded "
                  "recipe and row, so the saved counterexample is stale or was "
                  "modified")
    return ReviewResult(
        status,
        detail,
        swap=sw,
        row=row,
        original=a_json,
        swapped=b_json,
        legal_input=legal,
    )
