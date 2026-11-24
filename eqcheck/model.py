"""Recipe format: parsing, strict validation, and reference semantics.

A recipe is a JSON object:

  {
    "format": "supply-clean-recipe",
    "version": 1,
    "columns": [
      {"name": "id",       "type": "int",   "min": 0, "max": 10**18, "nullable": false},
      {"name": "age",      "type": "int",   "min": 0, "max": 130,    "nullable": true},
      {"name": "region",   "type": "enum",  "values": ["EU","US","APAC"], "nullable": true},
      {"name": "note",     "type": "enum",  "values": ["ok","bad",""], "nullable": true}
    ],
    "input_contract": {"all": [ {"column": "id", "op": "gt", "value": 0},
                                {"is_null": "age"} ]},
    "steps": [ ... see below ... ],
    "deliver": ["id", "region"]
  }

Column declarations
-------------------
- Type is exactly one of "int" (unbounded integer semantics, but every int
  column declares a finite inclusive interval [min, max] and every value
  produced for that column must lie inside it) or "enum" (explicit set of
  string values, declared once, non-empty).
- "nullable": bool, default false.  A non-nullable column must never hold
  null; the raw input may only contain null for a nullable column and no
  step may write null into a non-nullable column (see below).
- Column names are unique.  Declarations are immutable for a run.

Conditions (used by input_contract, step "when", and set/isolate)
-----------------------------------------------------------------
A condition is a boolean combination:
  {"all": [c, ...]} | {"any": [c, ...]} | {"not": c} |
  {"is_null": "<column>"} |
  {"column": "<col>", "op": "eq"|"ne"|"lt"|"le"|"gt"|"ge", "value": <json>} |
  {"column": "<col>", "op": "eq"|"ne"|"lt"|"le"|"gt"|"ge", "other_column": "<col2>"} |
  {"const": true|false}

Semantics:
- "column" comparisons require value (or other_column) to have the *same
  declared column type*: an int column compares to a JSON integer; an enum
  column compares to a string in its declared set (and to another enum
  column only if both enumerate the same value set).  A literal integer
  outside the column's interval is allowed; it can never equal an
  in-contract value, and the solver decides the comparison exactly (no value
  enumeration).
- Ordering ops lt/le/gt/ge are only legal on int columns and int columns
  equal to each other.  They are only meaningful when both sides non-null.
- Any comparison (eq/ne/lt/le/gt/ge/cmp), including "ne", evaluates to
  FALSE when either side is null.  Only an explicit {"is_null": col}
  observes null.  ("not" of an is_null is therefore the only way to say
  non-null, and a bare comparison is never true for a null side.)
- {"all": []} is true, {"any": []} is false.
- Every literal constant is type-checked against the declared column.

Steps
-----
Every step has a unique "id" and a "when" condition (default {"const": true}).

  {"kind": "set", "id": ..., "when": cond, "target": col, "value": <const>}
      Assign a constant.  For int targets the constant must be a JSON
      integer inside the declared interval; for enum targets a declared
      value.  Never null (use {"kind": "set_null"}).
  {"kind": "set_null", "id": ..., "when": cond, "target": col}
      Assign null; target must be declared nullable.  (This is a fixed
      recipe, so "may write null into non-nullable" is rejected at load
      time with a clear error rather than silently ignored.)
  {"kind": "copy", "id": ..., "when": cond, "source": col, "target": col}
      Copy one column into another.  The types must be compatible: int to
      int, or enum to enum with identical declared value sets.  The target's
      interval may be narrower than the source's; if it is, then at solve
      time the copy may actually write an out-of-range integer, and the
      isolation reason is the step's own FIXED typed reason "type:<step-id>".
      If the source can hold null and the target is non-nullable, a null
      copy is likewise a fixed typed error "type:<step-id>".
      The written value must be a member of the target's type: for int a
      value in [min,max], for enum a value in the declared set.
  {"kind": "isolate", "id": ..., "when": cond, "reason": "<non-empty name>"}
      If cond holds, the row is isolated with that named reason and no
      later step runs.

Isolation semantics
-------------------
- Contract/first-stage checks: every input row must satisfy input_contract
  and every raw non-null input value must be within its column declaration
  (int in interval, enum in set).  A row violating that is isolated with
  reason "input:contract" (contract) or "input:type:<col>" (type).  This
  is the single input-validation stage shared by both pipelines; it runs
  before any step and is identical in both orders.  (Do not express input
  validation with steps: steps are the permutable part.)
- Steps run in order; the first step whose "when" holds either isolates
  (kind isolate, or a copy that produces an invalid value / null into a
  non-nullable column => "type:<step-id>") or mutates columns.
- Once isolated, no later step runs.  An isolated row's result is the
  reason string of its first isolation only; its transient column contents
  are not compared.
- A successful row's result is the tuple of its deliver columns.
- The contract input stage is part of both pipelines and is not permuted;
  the two pipelines share the same stage.  Only two adjacent steps in
  "steps" are swapped.

The recipe never has aggregation or cross-row operators; validation
rejects unknown kinds/keys so a format extension cannot silently pass.
"""

import json
from typing import Any, Dict, List

INT_MIN_KEY = "min"
INT_MAX_KEY = "max"


class RecipeError(ValueError):
    pass


def _fail(msg: str) -> None:
    raise RecipeError(msg)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# --------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------

class Column:
    __slots__ = ("name", "type", "nullable", "min", "max", "values")

    def __init__(self, name, type_, nullable, min_=None, max_=None, values=None):
        self.name = name
        self.type = type_
        self.nullable = nullable
        self.min = min_
        self.max = max_
        self.values = values  # tuple for enum

    def in_range(self, v: int) -> bool:
        return self.min <= v <= self.max

    def __repr__(self):
        return f"<col {self.name}:{self.type}>"


def parse_columns(raw) -> Dict[str, Column]:
    if not isinstance(raw, list) or not raw:
        _fail("recipe.columns must be a non-empty array")
    out: Dict[str, Column] = {}
    for i, c in enumerate(raw):
        where = f"recipe.columns[{i}]"
        if not isinstance(c, dict):
            _fail(f"{where}: must be an object")
        extra = set(c) - {"name", "type", "nullable", "min", "max", "values"}
        if extra:
            _fail(f"{where}: unknown key(s) {sorted(extra)}")
        name = c.get("name")
        if not isinstance(name, str) or not name:
            _fail(f"{where}.name must be a non-empty string")
        if name in out:
            _fail(f"{where}.name {name!r} duplicates an earlier column")
        typ = c.get("type")
        nullable = c.get("nullable", False)
        if not isinstance(nullable, bool):
            _fail(f"{where}.nullable must be a boolean")
        if typ == "int":
            extra = set(c) - {"name", "type", "nullable", "min", "max"}
            if extra:
                _fail(f"{where}: int column has unknown key(s) {sorted(extra)}")
            lo, hi = c.get("min"), c.get("max")
            if not _is_int(lo) or not _is_int(hi):
                _fail(f"{where}: int column {name!r} must declare integer min and max")
            if lo > hi:
                _fail(f"{where}: int column {name!r} has min > max")
            out[name] = Column(name, "int", nullable, lo, hi)
        elif typ == "enum":
            extra = set(c) - {"name", "type", "nullable", "values"}
            if extra:
                _fail(f"{where}: enum column has unknown key(s) {sorted(extra)}")
            vals = c.get("values")
            if not isinstance(vals, list) or not vals:
                _fail(f"{where}: enum column {name!r} needs a non-empty values array")
            seen = set()
            for v in vals:
                if not isinstance(v, str):
                    _fail(f"{where}: enum values must be strings")
                if v in seen:
                    _fail(f"{where}: enum column {name!r} repeats value {v!r}")
                seen.add(v)
            out[name] = Column(name, "enum", nullable, values=tuple(vals))
        else:
            _fail(f"{where}.type must be 'int' or 'enum', got {typ!r}")
    return out


# --------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------

class Cond:
    """Parsed condition AST.  Node kinds:
    all/any/not const is_null cmp
    cmp: (col, op, kind, operand) where kind in {'const','col'} and
         op in {'eq','ne','lt','le','gt','ge'}
    """
    __slots__ = ("kind", "children", "col", "op", "operand", "const_val")

    def __init__(self, kind, children=None, col=None, op=None, operand=None,
                 const_val=None):
        self.kind = kind
        self.children = children or []
        self.col = col
        self.op = op
        self.operand = operand
        self.const_val = const_val

    def __repr__(self):
        if self.kind in ("all", "any"):
            return f"({self.kind} {' '.join(map(repr, self.children))})"
        if self.kind == "not":
            return f"(not {self.children[0]!r})"
        if self.kind == "is_null":
            return f"(is_null {self.col})"
        if self.kind == "const":
            return str(self.const_val)
        return f"({self.col} {self.op} {self.operand!r})"

    def mentioned_columns(self):
        if self.kind in ("all", "any"):
            s = set()
            for ch in self.children:
                s |= ch.mentioned_columns()
            return s
        if self.kind == "not":
            return self.children[0].mentioned_columns()
        if self.kind == "is_null":
            return {self.col}
        if self.kind == "cmp":
            s = {self.col}
            if self.operand is not None:
                s.add(self.operand)
            return s
        return set()


def _parse_cmp(node: dict, cols: Dict[str, Column], where: str) -> Cond:
    col = node.get("column")
    if not isinstance(col, str) or col not in cols:
        _fail(f"{where}: 'column' must name a declared column, got {col!r}")
    op = node.get("op")
    if op not in ("eq", "ne", "lt", "le", "gt", "ge"):
        _fail(f"{where}: op must be eq/ne/lt/le/gt/ge, got {op!r}")
    has_value = "value" in node
    has_other = "other_column" in node
    if has_value == has_other:
        _fail(f"{where}: comparison needs exactly one of 'value' or 'other_column'")
    extra = set(node) - {"column", "op", "value", "other_column"}
    if extra:
        _fail(f"{where}: unknown key(s) {sorted(extra)}")
    c = cols[col]
    key = node.get("other_column") if has_other else None
    if has_other:
        if not isinstance(key, str) or key not in cols:
            _fail(f"{where}: 'other_column' must name a declared column, got {key!r}")
        k = cols[key]
        if c.type != k.type:
            _fail(f"{where}: cannot compare {c.type} column {col!r} with {k.type} column {key!r}")
        if c.type == "int":
            if op not in ("eq", "ne", "lt", "le", "gt", "ge"):
                _fail(f"{where}: bad op")
        else:
            if op not in ("eq", "ne"):
                _fail(f"{where}: enum columns only support eq/ne, got {op!r}")
            if c.values != k.values:
                _fail(f"{where}: enum columns {col!r} and {key!r} have different value sets; "
                      f"use an explicit value comparison")
        return Cond("cmp", col=col, op=op, operand=key)
    val = node["value"]
    if c.type == "int":
        if not _is_int(val):
            _fail(f"{where}: int column {col!r} must compare to a JSON integer, got {val!r}")
        # A literal outside the column interval is allowed; it simply can
        # never equal an in-contract value, and the SMT encoding decides the
        # comparison exactly without enumerating values.
        return Cond("cmp", col=col, op=op, operand=None, const_val=val)
    else:
        if not isinstance(val, str) or val not in c.values:
            _fail(f"{where}: enum column {col!r} must compare to a declared value, got {val!r}")
        if op not in ("eq", "ne"):
            _fail(f"{where}: enum columns only support eq/ne, got {op!r}")
        return Cond("cmp", col=col, op=op, operand=None, const_val=val)


def parse_cond(node, cols: Dict[str, Column], where: str) -> Cond:
    if not isinstance(node, dict):
        _fail(f"{where}: condition must be an object, got {type(node).__name__}")
    if "const" in node:
        extra = set(node) - {"const"}
        if extra:
            _fail(f"{where}: 'const' cannot be combined with {sorted(extra)}")
        if not isinstance(node["const"], bool):
            _fail(f"{where}: 'const' must be a boolean")
        return Cond("const", const_val=node["const"])
    if "all" in node or "any" in node:
        k = "all" if "all" in node else "any"
        extra = set(node) - {k}
        if extra:
            _fail(f"{where}: '{k}' cannot be combined with {sorted(extra)}")
        arr = node[k]
        if not isinstance(arr, list):
            _fail(f"{where}: '{k}' must be an array")
        return Cond(k, children=[parse_cond(ch, cols, f"{where}.{k}[{i}]")
                                 for i, ch in enumerate(arr)])
    if "not" in node:
        extra = set(node) - {"not"}
        if extra:
            _fail(f"{where}: 'not' cannot be combined with {sorted(extra)}")
        return Cond("not", children=[parse_cond(node["not"], cols, f"{where}.not")])
    if "is_null" in node:
        extra = set(node) - {"is_null"}
        if extra:
            _fail(f"{where}: 'is_null' cannot be combined with {sorted(extra)}")
        col = node["is_null"]
        if not isinstance(col, str) or col not in cols:
            _fail(f"{where}: is_null must name a declared column, got {col!r}")
        return Cond("is_null", col=col)
    if "column" in node:
        return _parse_cmp(node, cols, where)
    _fail(f"{where}: unrecognised condition keys {sorted(node)}")


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

class Step:
    __slots__ = ("id", "kind", "when", "target", "source", "value", "reason", "index")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def fixed_reason(self) -> str:
        return f"type:{self.id}"

    def __repr__(self):
        return f"<step {self.index}:{self.kind}:{self.id}>"


STEP_KEYS = {
    "set":       {"id", "kind", "when", "target", "value"},
    "set_null":  {"id", "kind", "when", "target"},
    "copy":      {"id", "kind", "when", "source", "target"},
    "isolate":   {"id", "kind", "when", "reason"},
}


def parse_steps(raw, cols: Dict[str, Column]) -> List[Step]:
    if not isinstance(raw, list) or not raw:
        _fail("recipe.steps must be a non-empty array")
    out = []
    ids = set()
    for i, s in enumerate(raw):
        where = f"recipe.steps[{i}]"
        if not isinstance(s, dict):
            _fail(f"{where}: must be an object")
        kind = s.get("kind")
        if kind not in STEP_KEYS:
            _fail(f"{where}: kind must be one of {sorted(STEP_KEYS)}, got {kind!r}")
        extra = set(s) - STEP_KEYS[kind]
        if extra:
            _fail(f"{where}: unknown key(s) {sorted(extra)} for kind {kind!r}")
        sid = s.get("id")
        if not isinstance(sid, str) or not sid:
            _fail(f"{where}.id must be a non-empty string")
        if sid in ids:
            _fail(f"{where}.id {sid!r} is duplicated")
        ids.add(sid)
        when = parse_cond(s.get("when", {"const": True}), cols, f"{where}.when")
        target = None
        if kind in ("set", "set_null", "copy"):
            target = s.get("target")
            if not isinstance(target, str) or target not in cols:
                _fail(f"{where}.target must name a declared column, got {target!r}")
        if kind == "set":
            t = cols[target]
            val = s["value"]
            if t.type == "int":
                if not _is_int(val):
                    _fail(f"{where}.value must be a JSON integer for int column {target!r}")
                if not t.in_range(val):
                    _fail(f"{where}.value {val} is outside declared interval "
                          f"[{t.min},{t.max}] of column {target!r}")
            else:
                if not isinstance(val, str) or val not in t.values:
                    _fail(f"{where}.value must be one of {list(t.values)} for enum column "
                          f"{target!r}, got {val!r}")
            out.append(Step(id=sid, kind=kind, when=when, target=target,
                            value=val, index=i))
        elif kind == "set_null":
            t = cols[target]
            if not t.nullable:
                _fail(f"{where}: column {target!r} is not nullable; set_null would write "
                      f"null into a non-nullable column")
            out.append(Step(id=sid, kind=kind, when=when, target=target, index=i))
        elif kind == "copy":
            src = s.get("source")
            if not isinstance(src, str) or src not in cols:
                _fail(f"{where}.source must name a declared column, got {src!r}")
            a, b = cols[src], cols[target]
            if a.type != b.type:
                _fail(f"{where}: cannot copy {a.type} column {src!r} into {b.type} "
                      f"column {target!r}")
            if a.type == "enum" and a.values != b.values:
                _fail(f"{where}: enum columns {src!r} and {target!r} have different "
                      f"declared value sets")
            if not b.nullable and (not a.nullable):
                pass  # statically safe against null
            out.append(Step(id=sid, kind=kind, when=when, target=target,
                            source=src, index=i))
        else:  # isolate
            reason = s.get("reason")
            if not isinstance(reason, str) or not reason:
                _fail(f"{where}.reason must be a non-empty string")
            if reason.startswith("input:") or reason.startswith("type:"):
                _fail(f"{where}.reason may not start with reserved prefixes "
                      f"'input:' or 'type:'")
            out.append(Step(id=sid, kind=kind, when=when, reason=reason, index=i))
    return out


# --------------------------------------------------------------------------
# Recipe
# --------------------------------------------------------------------------

class Recipe:
    def __init__(self, cols, contract, steps, deliver):
        self.cols = cols
        self.contract = contract
        self.steps = steps
        self.deliver = deliver

    # -- helpers ----------------------------------------------------------
    def column_names(self):
        return list(self.cols)

    def with_swapped(self, i: int) -> "Recipe":
        steps = list(self.steps)
        steps[i], steps[i + 1] = steps[i + 1], steps[i]
        return Recipe(self.cols, self.contract, steps, self.deliver)

    # -- reference execution ---------------------------------------------
    def run(self, row: Dict[str, Any], trace: bool = False):
        """Return (result, trace) where result is either
        ('ok', tuple(deliver values)) or ('isolated', reason)."""
        cols = self.cols
        events = []
        # Raw row shape: exactly the declared columns, nothing else.
        for name in row:
            if name not in cols:
                _fail(f"input row has undeclared column {name!r}")
        for name in cols:
            if name not in row:
                _fail(f"input row is missing declared column {name!r}")
        stage = "input"
        # Input validation (shared stage, not permuted).
        if not eval_cond(self.contract, row):
            events.append(("input", None, "input:contract"))
            return _result_with_trace(("isolated", "input:contract"), events, trace)
        for name, col in cols.items():
            v = row[name]
            if v is None:
                if not col.nullable:
                    r = f"input:type:{name}"
                    events.append(("input", None, r))
                    return _result_with_trace(("isolated", r), events, trace)
            elif col.type == "int":
                if not _is_int(v) or not col.in_range(v):
                    r = f"input:type:{name}"
                    events.append(("input", None, r))
                    return _result_with_trace(("isolated", r), events, trace)
            else:
                if v not in col.values:
                    r = f"input:type:{name}"
                    events.append(("input", None, r))
                    return _result_with_trace(("isolated", r), events, trace)
        state = dict(row)
        for step in self.steps:
            if not eval_cond(step.when, state):
                events.append((step.id, "skip", None))
                continue
            if step.kind == "isolate":
                events.append((step.id, "isolate", step.reason))
                return _result_with_trace(("isolated", step.reason), events, trace)
            if step.kind == "set":
                state[step.target] = step.value
                events.append((step.id, "set", None))
            elif step.kind == "set_null":
                state[step.target] = None
                events.append((step.id, "set_null", None))
            elif step.kind == "copy":
                v = state[step.source]
                tcol = cols[step.target]
                bad = False
                if v is None:
                    bad = not tcol.nullable
                elif tcol.type == "int":
                    bad = not _is_int(v) or not tcol.in_range(v)
                # enum compatibility is enforced at load time
                if bad:
                    events.append((step.id, "isolate", step.fixed_reason))
                    return _result_with_trace(("isolated", step.fixed_reason), events, trace)
                state[step.target] = v
                events.append((step.id, "copy", None))
            else:  # pragma: no cover
                _fail(f"unknown step kind {step.kind!r}")
        result = ("ok", tuple(state[d] for d in self.deliver))
        return _result_with_trace(result, events, trace)


def _result_with_trace(result, events, trace):
    return (result, events) if trace else result


def _cmp_int(a, b, op):
    """Ordered comparison on non-null ints; strict Python semantics."""
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    if op == "lt":
        return a < b
    if op == "le":
        return a <= b
    if op == "gt":
        return a > b
    return a >= b


def eval_cond(node: Cond, state: Dict[str, Any]) -> bool:
    k = node.kind
    if k == "const":
        return node.const_val
    if k == "all":
        return all(eval_cond(c, state) for c in node.children)
    if k == "any":
        return any(eval_cond(c, state) for c in node.children)
    if k == "not":
        return not eval_cond(node.children[0], state)
    if k == "is_null":
        return state[node.col] is None
    # cmp
    a = state[node.col]
    b = state[node.operand] if node.operand is not None else node.const_val
    if a is None or b is None:
        return False
    return _cmp_int(a, b, node.op)


# --------------------------------------------------------------------------
# Top-level parsing
# --------------------------------------------------------------------------

RECIPE_KEYS = {"format", "version", "columns", "input_contract", "steps", "deliver"}


def parse_recipe(obj) -> Recipe:
    if not isinstance(obj, dict):
        _fail("recipe must be a JSON object")
    extra = set(obj) - RECIPE_KEYS
    if extra:
        _fail(f"recipe has unknown key(s) {sorted(extra)}")
    expected = {"format", "version", "columns", "input_contract", "steps", "deliver"}
    missing = expected - set(obj)
    if missing:
        _fail(f"recipe is missing required key(s) {sorted(missing)}")
    if obj["format"] != "supply-clean-recipe":
        _fail(f"recipe.format must be 'supply-clean-recipe', got {obj['format']!r}")
    if obj["version"] != 1 or isinstance(obj["version"], bool):
        _fail(f"recipe.version must be 1, got {obj['version']!r}")
    cols = parse_columns(obj["columns"])
    contract = parse_cond(obj["input_contract"], cols, "recipe.input_contract")
    steps = parse_steps(obj["steps"], cols)
    deliver = obj["deliver"]
    if not isinstance(deliver, list) or not deliver:
        _fail("recipe.deliver must be a non-empty array of column names")
    seen = set()
    for d in deliver:
        if not isinstance(d, str) or d not in cols:
            _fail(f"recipe.deliver names unknown column {d!r}")
        if d in seen:
            _fail(f"recipe.deliver repeats column {d!r}")
        seen.add(d)
    return Recipe(cols, contract, steps, deliver)


def load_recipe_file(path: str) -> Recipe:
    with open(path, "r", encoding="utf-8") as f:
        try:
            obj = json.load(f)
        except json.JSONDecodeError as e:
            _fail(f"{path}: not valid JSON: {e}")
    try:
        return parse_recipe(obj)
    except RecipeError as e:
        raise RecipeError(f"{path}: {e}") from None
