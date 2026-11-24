"""Test suite for eqcheck.  Run: python3 -m unittest discover -s tests -v"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from eqcheck.engine import Limits, replay_cex, review  # noqa: E402
from eqcheck.model import parse_recipe, RecipeError  # noqa: E402

WIDE = 10 ** 18


def base_recipe(**over):
    r = {
        "format": "supply-clean-recipe",
        "version": 1,
        "columns": [
            {"name": "id", "type": "int", "min": 0, "max": WIDE, "nullable": False},
            {"name": "age", "type": "int", "min": 0, "max": 130, "nullable": True},
            {"name": "region", "type": "enum", "values": ["EU", "US"], "nullable": True},
            {"name": "status", "type": "enum", "values": ["new", "ok", "bad"], "nullable": True},
        ],
        "input_contract": {"const": True},
        "steps": [
            {"kind": "isolate", "id": "s0", "when": {"column": "id", "op": "eq", "value": 0}, "reason": "zero"},
            {"kind": "set", "id": "s1", "when": {"column": "age", "op": "gt", "value": 17}, "target": "status", "value": "ok"},
        ],
        "deliver": ["id", "age", "region", "status"],
    }
    r.update(over)
    return r


def run(r, i=None, **kw):
    lim = Limits(**kw) if kw else None
    return review(r, i, limits=lim)


class TestSemantics(unittest.TestCase):
    def test_commuting_disjoint(self):
        r = base_recipe()
        res = run(r, 0)
        self.assertEqual(res.status, "EQUIVALENT", res.detail)

    def test_isolate_after_set_is_counterexample(self):
        r = base_recipe(steps=[
            {"kind": "isolate", "id": "a", "when": {"column": "status", "op": "eq", "value": "bad"}, "reason": "bad"},
            {"kind": "set", "id": "b", "when": {"column": "age", "op": "gt", "value": 17}, "target": "status", "value": "ok"},
        ])
        res = run(r, 0)
        self.assertEqual(res.status, "COUNTEREXAMPLE", res.detail)
        row = res.extra["row"]
        self.assertEqual(row["status"], "bad")
        self.assertGreater(row["age"], 17)
        self.assertTrue(0 <= row["id"] <= WIDE)
        # replay reproduces from embedded recipe+row only
        rep = replay_cex(res.extra["counterexample"])
        self.assertEqual(rep.status, "COUNTEREXAMPLE")
        self.assertTrue(rep.extra["legal_input"])

    def test_wide_domain_within_contract(self):
        # contract restricts to a single enormous id; no enumeration possible
        r = base_recipe(
            columns=[
                {"name": "id", "type": "int", "min": 0, "max": WIDE, "nullable": False},
                {"name": "age", "type": "int", "min": 0, "max": 130, "nullable": True},
                {"name": "region", "type": "enum", "values": ["EU", "US"], "nullable": True},
                {"name": "status", "type": "enum", "values": ["new", "ok", "bad"], "nullable": True},
            ],
            input_contract={"all": [{"column": "id", "op": "gt", "value": WIDE - 3},
                                    {"column": "id", "op": "le", "value": WIDE}]},
            steps=[
                {"kind": "set", "id": "a", "when": {"column": "age", "op": "lt", "value": 10}, "target": "status", "value": "bad"},
                {"kind": "isolate", "id": "b", "when": {"column": "status", "op": "eq", "value": "bad"}, "reason": "r_bad"},
            ],
        )
        res = run(r, 0)
        self.assertEqual(res.status, "COUNTEREXAMPLE", res.detail)
        self.assertTrue(WIDE - 3 < res.extra["row"]["id"] <= WIDE)

    def test_empty_contract_not_equivalent(self):
        r = base_recipe(input_contract={"all": [
            {"column": "age", "op": "gt", "value": 100},
            {"column": "age", "op": "lt", "value": 10}]})
        res = run(r, 0)
        self.assertEqual(res.status, "EMPTY_CONTRACT")
        self.assertNotEqual(res.status, "EQUIVALENT")

    def test_null_comparison_is_false(self):
        # age null: "age > 17" false; no isolation.  Both orders agree.
        r = base_recipe(steps=[
            {"kind": "set", "id": "a", "when": {"column": "age", "op": "ne", "value": 5}, "target": "status", "value": "ok"},
            {"kind": "isolate", "id": "b", "when": {"column": "status", "op": "eq", "value": "ok"}, "reason": "r"},
        ])
        res = run(r, 0)
        # swapped: isolate first (status 'ok' possible from input) then set
        self.assertEqual(res.status, "COUNTEREXAMPLE")

    def test_null_ne_does_not_fire(self):
        r = base_recipe(steps=[
            {"kind": "set", "id": "a", "when": {"column": "age", "op": "ne", "value": 5}, "target": "status", "value": "ok"},
            {"kind": "set", "id": "b", "when": {"column": "age", "op": "eq", "value": 7}, "target": "region", "value": "US"},
        ])
        # age==7 implies age!=5, so both fire and commute
        self.assertEqual(run(r, 0).status, "EQUIVALENT")

    def test_copy_null_into_nonnullable_typed_reason(self):
        r = base_recipe(
            columns=[
                {"name": "age", "type": "int", "min": 0, "max": 130, "nullable": True},
                {"name": "x", "type": "int", "min": 0, "max": 130, "nullable": False},
                {"name": "status", "type": "enum", "values": ["new", "ok", "bad"], "nullable": True},
            ],
            steps=[
                {"kind": "copy", "id": "cp", "when": {"const": True}, "source": "age", "target": "x"},
                {"kind": "isolate", "id": "iz", "when": {"is_null": "age"}, "reason": "age_null"},
            ],
            deliver=["x", "age"],
        )
        res = run(r, 0)
        self.assertEqual(res.status, "COUNTEREXAMPLE", res.detail)
        self.assertEqual(res.extra["original"], {"outcome": "isolated", "reason": "type:cp"})
        self.assertIsNone(res.extra["row"]["age"])

    def test_copy_range_overflow_typed_reason(self):
        r = base_recipe(
            columns=[
                {"name": "id", "type": "int", "min": 0, "max": 100, "nullable": False},
                {"name": "small", "type": "int", "min": 0, "max": 5, "nullable": False},
            ],
            steps=[
                {"kind": "copy", "id": "c", "when": {"const": True}, "source": "id", "target": "small"},
                {"kind": "set", "id": "s", "when": {"column": "id", "op": "gt", "value": 5}, "target": "id", "value": 1},
            ],
            deliver=["id", "small"],
        )
        res = run(r, 0)
        self.assertEqual(res.status, "COUNTEREXAMPLE", res.detail)
        self.assertEqual(res.extra["original"]["reason"], "type:c")
        self.assertGreater(res.extra["row"]["id"], 5)

    def test_isolated_rows_compare_reason_only(self):
        # Original: rows with id>10 isolate "big" *after* status was rewritten.
        # Swapped: isolation happens first.  The transient status differs, but
        # both final results are ("isolated","big"), so rows agree.
        r = base_recipe(steps=[
            {"kind": "set", "id": "b", "when": {"const": True}, "target": "status", "value": "ok"},
            {"kind": "isolate", "id": "a", "when": {"column": "id", "op": "gt", "value": 10}, "reason": "big"},
        ])
        self.assertEqual(run(r, 0).status, "EQUIVALENT")
        # A different first reason *is* a difference: moving the rewrite before
        # the age isolation changes which reason (if any) is emitted.
        r2 = base_recipe(steps=[
            {"kind": "isolate", "id": "a", "when": {"column": "age", "op": "gt", "value": 5}, "reason": "age_big"},
            {"kind": "set", "id": "b", "when": {"const": True}, "target": "age", "value": 1},
        ])
        res = run(r2, 0)
        self.assertEqual(res.status, "COUNTEREXAMPLE")
        self.assertEqual(res.extra["original"], {"outcome": "isolated", "reason": "age_big"})
        self.assertEqual(res.extra["swapped"]["outcome"], "ok")

    def test_undetermined_never_equivalent(self):
        r = base_recipe()
        res = run(r, 0, rlimit=1)
        self.assertEqual(res.status, "UNDETERMINED")
        self.assertNotEqual(res.status, "EQUIVALENT")

    def test_swap_index_out_of_range(self):
        self.assertEqual(run(base_recipe(), 5).status, "ERROR")

    def test_untouched_deliver_after_isolation(self):
        # isolated rows never run later steps; their deliver content is irrelevant
        r = base_recipe(steps=[
            {"kind": "set", "id": "a", "when": {"column": "id", "op": "gt", "value": 5}, "target": "age", "value": 3},
            {"kind": "isolate", "id": "b", "when": {"column": "id", "op": "gt", "value": 5}, "reason": "big"},
        ])
        self.assertEqual(run(r, 0).status, "EQUIVALENT")


class TestMoreSemantics(unittest.TestCase):
    def test_two_isolates_swapped_different_reason(self):
        r = base_recipe(steps=[
            {"kind": "isolate", "id": "a", "when": {"column": "id", "op": "gt", "value": 5}, "reason": "A"},
            {"kind": "isolate", "id": "b", "when": {"column": "id", "op": "gt", "value": 5}, "reason": "B"},
        ])
        res = run(r, 0)
        self.assertEqual(res.status, "COUNTEREXAMPLE", res.detail)
        self.assertEqual(res.extra["original"]["reason"], "A")
        self.assertEqual(res.extra["swapped"]["reason"], "B")

    def test_prefix_and_suffix_kept_in_place(self):
        # A prefix set makes the isolate's condition true; swapping the
        # isolate before that set means rows that only become "bad" later are
        # no longer isolated, so the pair genuinely differs.
        r = base_recipe(steps=[
            {"kind": "set", "id": "pre", "when": {"const": True}, "target": "status", "value": "bad"},
            {"kind": "isolate", "id": "iz", "when": {"column": "status", "op": "eq", "value": "bad"}, "reason": "bad"},
        ])
        res = run(r, 0)
        self.assertEqual(res.status, "COUNTEREXAMPLE", res.detail)
        self.assertEqual(res.extra["original"], {"outcome": "isolated", "reason": "bad"})
        self.assertEqual(res.extra["swapped"]["outcome"], "ok")
        # A suffix after the pair runs in both orders and does not hide it.
        r2 = base_recipe(steps=[
            {"kind": "set", "id": "pre", "when": {"const": True}, "target": "status", "value": "bad"},
            {"kind": "isolate", "id": "iz", "when": {"column": "status", "op": "eq", "value": "bad"}, "reason": "bad"},
            {"kind": "set", "id": "suf", "when": {"const": True}, "target": "age", "value": 1},
        ])
        self.assertEqual(run(r2, 0).status, "COUNTEREXAMPLE")

    def test_column_to_column_comparison(self):
        # a<b makes a become 9; the isolate then catches a==9.  Swapped, a row
        # with a<b and a!=9 is no longer isolated.
        r = base_recipe(
            columns=[
                {"name": "a", "type": "int", "min": 0, "max": 10, "nullable": True},
                {"name": "b", "type": "int", "min": 0, "max": 10, "nullable": True},
            ],
            deliver=["a", "b"],
            steps=[
                {"kind": "set", "id": "sa", "when": {"column": "a", "op": "lt", "other_column": "b"}, "target": "a", "value": 9},
                {"kind": "isolate", "id": "ia", "when": {"column": "a", "op": "eq", "value": 9}, "reason": "a9"},
            ],
        )
        res = run(r, 0)
        self.assertEqual(res.status, "COUNTEREXAMPLE", res.detail)
        self.assertEqual(res.extra["original"]["reason"], "a9")
        self.assertNotEqual(res.extra["row"]["a"], 9)

    def test_enum_column_to_column(self):
        cols = [
            {"name": "s1", "type": "enum", "values": ["x", "y"], "nullable": True},
            {"name": "s2", "type": "enum", "values": ["x", "y"], "nullable": True},
        ]
        r = base_recipe(columns=cols, deliver=["s1"], steps=[
            {"kind": "set", "id": "a", "when": {"column": "s1", "op": "eq", "other_column": "s2"}, "target": "s1", "value": "y"},
            {"kind": "isolate", "id": "b", "when": {"column": "s1", "op": "eq", "value": "y"}, "reason": "y"},
        ])
        self.assertEqual(run(r, 0).status, "COUNTEREXAMPLE")

    def test_copy_null_semantics_with_nullable_target(self):
        r = base_recipe(
            columns=[
                {"name": "a", "type": "int", "min": 0, "max": 10, "nullable": True},
                {"name": "b", "type": "int", "min": 0, "max": 20, "nullable": True},
            ],
            deliver=["b"],
            steps=[
                {"kind": "copy", "id": "cp", "when": {"const": True}, "source": "a", "target": "b"},
                {"kind": "set", "id": "sa", "when": {"is_null": "a"}, "target": "a", "value": 0},
            ],
        )
        # original: b copies null, a then becomes 0 -> b null
        # swapped: a becomes 0, b copies 0 -> b = 0
        res = run(r, 0)
        self.assertEqual(res.status, "COUNTEREXAMPLE", res.detail)
        self.assertEqual(res.extra["row"]["a"], None)
        self.assertEqual(res.extra["original"]["deliver"], [None])
        self.assertEqual(res.extra["swapped"]["deliver"], [0])


class TestFormat(unittest.TestCase):
    def check_err(self, r, frag):
        with self.assertRaises(RecipeError) as cm:
            parse_recipe(r)
        self.assertIn(frag, str(cm.exception))

    def test_interval_required(self):
        r = base_recipe(); del r["columns"][0]["max"]
        self.check_err(r, "must declare integer min and max")

    def test_enum_write_outside_set(self):
        r = base_recipe(); r["steps"][1]["value"] = "nope"
        self.check_err(r, "must be one of")

    def test_int_write_outside_interval(self):
        r = base_recipe(); r["steps"][1]["value"] = 999
        r["steps"][1]["target"] = "age"
        self.check_err(r, "outside declared interval")

    def test_set_null_nonnullable(self):
        r = base_recipe(); r["steps"][1] = {"kind": "set_null", "id": "n", "when": {"const": True}, "target": "id"}
        self.check_err(r, "not nullable")

    def test_cross_type_copy(self):
        r = base_recipe()
        r["steps"][0] = {"kind": "copy", "id": "k", "when": {"const": True},
                         "source": "age", "target": "status"}
        self.check_err(r, "cannot copy")

    def test_enum_mismatch_copy(self):
        r = base_recipe()
        r["steps"][0] = {"kind": "copy", "id": "k", "when": {"const": True}, "source": "region", "target": "status"}
        self.check_err(r, "different")

    def test_bad_json_rejected_by_cli(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
            p = f.name
        outs = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "eqcheck", "run.py"),
                               "check", p], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(outs.returncode, 3, outs.stdout + outs.stderr)
        os.unlink(p)


class TestCliReplay(unittest.TestCase):
    def test_cli_counterexample_and_replay(self):
        with tempfile.TemporaryDirectory() as d:
            rp = os.path.join(d, "r.json")
            with open(rp, "w") as f:
                json.dump(base_recipe(steps=[
                    {"kind": "isolate", "id": "a", "when": {"column": "status", "op": "eq", "value": "bad"}, "reason": "bad"},
                    {"kind": "set", "id": "b", "when": {"column": "age", "op": "gt", "value": 17}, "target": "status", "value": "ok"},
                ]), f)
            p = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "eqcheck", "run.py"),
                                "check", rp, "--swap", "0"], capture_output=True, text=True, cwd=d)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            out = json.loads(p.stdout)
            cef = out["results"][0]["counterexample_file"]
            cep = os.path.join(d, cef)
            self.assertTrue(os.path.exists(cep))
            p2 = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "eqcheck", "run.py"),
                                 "replay", cep], capture_output=True, text=True, cwd=d)
            self.assertEqual(p2.returncode, 1, p2.stdout + p2.stderr)
            self.assertEqual(json.loads(p2.stdout)["status"], "COUNTEREXAMPLE")


if __name__ == "__main__":
    unittest.main()
