"""Tests for best-ever tracking (evolve.best_ever_from_scores / update_best_ever).

best_ever.json is a cache; iteration_scores.yaml is the source of truth. These
tests pin that relationship, including the cases where the two can disagree.

Run with: pytest tests/test_best_ever.py
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import evolve  # noqa: E402


def write_scores(exp_dir, entries):
    """Seed iteration_scores.yaml. entries: [(iteration, pass_rate, extra?), ...]"""
    scores = []
    for entry in entries:
        iteration, pass_rate = entry[0], entry[1]
        extra = entry[2] if len(entry) > 2 else {}
        scores.append({"iteration": iteration, "pass_rate": pass_rate, "k": 1, **extra})
    (exp_dir / "iteration_scores.yaml").write_text(
        yaml.dump({"experiment": "t", "model": "m", "scores": scores}), encoding="utf-8")


def make_stats(pass_rate, n_pass, n_fail, n_exception=0, k=1, **extra):
    return {"pass_rate": pass_rate, "k": k, "n_pass": n_pass,
            "n_fail": n_fail, "n_exception": n_exception, **extra}


def read_cache(exp_dir):
    return json.loads((exp_dir / "best_ever.json").read_text(encoding="utf-8"))


# -- best_ever_from_scores ---------------------------------------------------

def test_from_scores_picks_highest_pass_rate(tmp_path):
    write_scores(tmp_path, [(1, 0.20), (2, 0.90), (3, 0.50)])
    assert evolve.best_ever_from_scores(tmp_path)["iteration"] == 2


def test_from_scores_before_iteration_excludes_current_and_later(tmp_path):
    write_scores(tmp_path, [(1, 0.20), (2, 0.90), (3, 0.50)])
    assert evolve.best_ever_from_scores(tmp_path, before_iteration=2)["iteration"] == 1


def test_from_scores_returns_none_without_ledger(tmp_path):
    assert evolve.best_ever_from_scores(tmp_path) is None


def test_from_scores_returns_none_for_empty_ledger(tmp_path):
    write_scores(tmp_path, [])
    assert evolve.best_ever_from_scores(tmp_path) is None


def test_from_scores_honours_legacy_capability_rate(tmp_path):
    (tmp_path / "iteration_scores.yaml").write_text(
        yaml.dump({"scores": [{"iteration": 4, "capability_rate": 0.66}]}), encoding="utf-8")
    assert evolve.best_ever_from_scores(tmp_path)["pass_rate"] == 0.66


def test_from_scores_carries_detail_fields(tmp_path):
    tasks = {"total": 10, "pass": 8, "fail": 2, "exception": 0}
    write_scores(tmp_path, [(1, 0.80, {"tasks": tasks, "pass_at": {1: 0.8}})])
    best = evolve.best_ever_from_scores(tmp_path)
    assert best["tasks"] == tasks
    assert best["pass_at"] == {1: 0.8}


# -- update_best_ever: normal operation --------------------------------------

def test_first_iteration_becomes_best(tmp_path):
    best = evolve.update_best_ever(tmp_path, 1, make_stats(0.25, 2, 6))
    assert (best["iteration"], best["pass_rate"]) == (1, 0.25)
    assert best["tasks"] == {"total": 8, "pass": 2, "fail": 6, "exception": 0}


def test_improvement_is_accepted(tmp_path):
    write_scores(tmp_path, [(1, 0.30)])
    best = evolve.update_best_ever(tmp_path, 2, make_stats(0.60, 6, 4))
    assert (best["iteration"], best["pass_rate"]) == (2, 0.60)
    assert read_cache(tmp_path)["iteration"] == 2


def test_regression_is_rejected_and_keeps_detail_fields(tmp_path):
    tasks = {"total": 10, "pass": 8, "fail": 2, "exception": 0}
    write_scores(tmp_path, [(1, 0.80, {"tasks": tasks})])
    best = evolve.update_best_ever(tmp_path, 2, make_stats(0.50, 5, 5))
    assert (best["iteration"], best["pass_rate"]) == (1, 0.80)
    assert best["tasks"] == tasks


def test_tie_keeps_the_earlier_iteration(tmp_path):
    write_scores(tmp_path, [(1, 0.50)])
    assert evolve.update_best_ever(tmp_path, 2, make_stats(0.50, 5, 5))["iteration"] == 1


def test_pass_at_k_payload_is_preserved(tmp_path):
    write_scores(tmp_path, [(1, 0.10)])
    best = evolve.update_best_ever(tmp_path, 2, make_stats(
        0.50, 0, 0, k=3,
        task_results={"a": "pass", "b": "fail", "c": "exception"},
        per_task_rollouts={"b": {"n_pass": 1}},
        trial_stats={"n_total": 9, "n_pass": 4, "n_fail": 4,
                     "n_exception": 1, "trial_pass_rate": 0.4444},
        pass_at_k={"pass_at": {1: 0.333, 3: 0.667}}))
    assert best["k"] == 3
    assert best["pass_at"] == {1: 0.333, 3: 0.667}
    assert best["trials"]["pass_rate"] == 0.4444
    assert best["tasks"]["passed_3_of_3"] == 1
    assert best["tasks"]["all_exception"] == 1


# -- update_best_ever: cache and ledger disagree -----------------------------

def test_cache_ahead_of_ledger_does_not_shadow_scored_iterations(tmp_path):
    """update_best_ever writes the cache before the ledger entry for the same
    iteration is appended, so a crash in between leaves the cache claiming an
    iteration that was never scored. The ledger must still win."""
    write_scores(tmp_path, [(1, 0.30), (2, 0.70)])
    evolve.update_best_ever(tmp_path, 3, make_stats(0.90, 9, 1))
    assert read_cache(tmp_path)["iteration"] == 3  # cache now ahead of the ledger

    # run dies before update_iteration_scores; iteration 3 is retried at 0.50
    best = evolve.update_best_ever(tmp_path, 3, make_stats(0.50, 5, 5))
    assert (best["iteration"], best["pass_rate"]) == (2, 0.70)
    assert read_cache(tmp_path)["iteration"] == 2


def test_stale_cache_does_not_suppress_a_real_improvement(tmp_path):
    write_scores(tmp_path, [(1, 0.20)])
    evolve.save_best_ever(tmp_path, {"iteration": 7, "pass_rate": 0.95})
    best = evolve.update_best_ever(tmp_path, 2, make_stats(0.40, 4, 6))
    assert (best["iteration"], best["pass_rate"]) == (2, 0.40)


def test_tampered_cache_cannot_admit_a_regression(tmp_path):
    """exp_dir is the evolve agent's sandbox root, so best_ever.json is writable
    by the process being scored. A forged low prior must not let a regressed
    iteration be recorded as best-ever (see issue #15)."""
    write_scores(tmp_path, [(3, 0.70)])
    (tmp_path / "best_ever.json").write_text(
        json.dumps({"iteration": 99, "pass_rate": 0.0}), encoding="utf-8")
    best = evolve.update_best_ever(tmp_path, 4, make_stats(0.40, 4, 6))
    assert (best["iteration"], best["pass_rate"]) == (3, 0.70)
    assert read_cache(tmp_path)["iteration"] == 3  # forged file repaired


@pytest.mark.parametrize("forged_iteration", [99, 0, -1])
def test_forged_iteration_never_reaches_the_prompt(tmp_path, forged_iteration):
    """best_ever['iteration'] renders a snapshot pointer into the next
    evolution prompt (build_evolution_query), so it must trace to a scored
    iteration."""
    write_scores(tmp_path, [(1, 0.60), (2, 0.75)])
    (tmp_path / "best_ever.json").write_text(
        json.dumps({"iteration": forged_iteration, "pass_rate": 0.0}), encoding="utf-8")
    best = evolve.update_best_ever(tmp_path, 3, make_stats(0.10, 1, 9))
    assert best["iteration"] in (1, 2)
