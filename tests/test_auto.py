"""`main.py auto`: stages from a recipe, skipping what is up to date, leaving hand edits alone (a fake runner, no tools run)."""
import json

from indoor_gen import auto

SLUG = "b"


def make_root(tmp_path, steps):
    plans = tmp_path / "configs" / "plans" / SLUG
    plans.mkdir(parents=True)
    (plans / "recipe.json").write_text(json.dumps({"sources": [], "images": [], "steps": steps}), encoding="utf-8")
    (plans / "in.txt").write_text("v1", encoding="utf-8")
    (tmp_path / "configs" / f"{SLUG}.json").write_text("{}", encoding="utf-8")
    return plans


class Runner:
    def __init__(self, tmp_path, fail=()):
        self.calls, self.root, self.fail = [], tmp_path, fail

    def __call__(self, root, cmd):
        self.calls.append(cmd)
        if cmd in self.fail:
            return 2, "boom"
        if cmd.startswith("make"):
            (root / "configs" / "plans" / SLUG / "out.txt").write_text(f"made {len(self.calls)}", encoding="utf-8")
        return 0, ""


STEP = {"name": "make", "cmd": "make", "inputs": [f"configs/plans/{SLUG}/in.txt"],
        "outputs": [f"configs/plans/{SLUG}/out.txt"]}


def statuses(res):
    return {r["stage"]: r["status"] for r in res["stages"]}


def test_runs_then_skips_until_the_input_changes(tmp_path):
    plans = make_root(tmp_path, [STEP])
    runner = Runner(tmp_path)
    first = auto.run(tmp_path, SLUG, runner=runner)
    assert statuses(first)["make"] == "ran" and statuses(first)["build"] == "ran"
    again = auto.run(tmp_path, SLUG, runner=runner)
    assert statuses(again)["make"] == "up to date" and statuses(again)["build"] == "up to date"
    (plans / "in.txt").write_text("v2", encoding="utf-8")
    third = auto.run(tmp_path, SLUG, runner=runner)
    assert statuses(third)["make"] == "ran" and statuses(third)["build"] == "ran"
    assert runner.calls == ["make", f"main.py build configs/{SLUG}.json"] * 2


def test_an_output_that_exists_but_was_never_run_is_adopted_not_overwritten(tmp_path):
    plans = make_root(tmp_path, [STEP])
    (plans / "out.txt").write_text("hand made", encoding="utf-8")
    runner = Runner(tmp_path)
    res = auto.run(tmp_path, SLUG, runner=runner)
    assert statuses(res)["make"].startswith("adopted") and "make" not in runner.calls
    assert (plans / "out.txt").read_text(encoding="utf-8") == "hand made"


def test_a_hand_edited_output_is_not_overwritten_unless_forced(tmp_path):
    plans = make_root(tmp_path, [STEP])
    runner = Runner(tmp_path)
    auto.run(tmp_path, SLUG, runner=runner)
    (plans / "out.txt").write_text("edited", encoding="utf-8")
    (plans / "in.txt").write_text("v2", encoding="utf-8")
    res = auto.run(tmp_path, SLUG, runner=runner)
    assert statuses(res)["make"].startswith("edited by hand") and any("edited by hand" in m for m in res["review"])
    assert (plans / "out.txt").read_text(encoding="utf-8") == "edited"
    forced = auto.run(tmp_path, SLUG, runner=runner, force=True)
    assert statuses(forced)["make"] == "ran" and (plans / "out.txt").read_text(encoding="utf-8").startswith("made")


def test_a_failed_stage_stops_the_rest_and_manual_steps_go_to_the_review_list(tmp_path):
    make_root(tmp_path, [{"name": "pictograms", "review": "accept by eye"}, {"name": "export", "optional": True},
                         {"name": "boom", "cmd": "boom"}])
    res = auto.run(tmp_path, SLUG, runner=Runner(tmp_path, fail=("boom",)))
    st = statuses(res)
    assert st["boom"] == "failed" and st["build"].startswith("not-run") and res["ok"] is False
    assert "pictograms: accept by eye" in res["review"] and not any(m.startswith("export") for m in res["review"])


def test_dry_run_changes_nothing_and_only_selects_stages(tmp_path):
    make_root(tmp_path, [STEP])
    runner = Runner(tmp_path)
    res = auto.run(tmp_path, SLUG, runner=runner, dry_run=True)
    assert statuses(res)["make"] == "would run" and runner.calls == []
    assert not auto.state_path(tmp_path, SLUG).exists()
    only = auto.run(tmp_path, SLUG, runner=runner, only="make")
    assert statuses(only)["build"] == "not-selected" and runner.calls == ["make"]
