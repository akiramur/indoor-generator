import json
import re

import pytest

from indoor_gen import usage_cost

PRICES = {"as_of": "test", "web_search_per_1k": 10.0,
          "models": {"claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write_5m": 5.0,
                                         "cache_write_1h": 8.0, "cache_read": 0.2, "fast_multiplier": 2.0}}}


def prompt(ts, text, sid="s1"):
    return {"type": "user", "sessionId": sid, "timestamp": ts, "message": {"role": "user", "content": text}}


def reply(ts, mid, tools=(), sid="s1", model="claude-opus-5-5", output=1_000_000, write_1h=0, **extra):
    content = [{"type": "tool_use", "name": n, "input": i} for n, i in tools] or [{"type": "text", "text": "ok"}]
    usage = {"input_tokens": 0, "cache_creation_input_tokens": write_1h, "cache_read_input_tokens": 0,
             "output_tokens": output, "cache_creation": {"ephemeral_1h_input_tokens": write_1h}, **extra}
    return {"type": "assistant", "sessionId": sid, "timestamp": ts,
            "message": {"id": mid, "model": model, "content": content, "usage": usage}}


def tool_result(ts, sid="s1"):
    return {"type": "user", "sessionId": sid, "timestamp": ts,
            "message": {"content": [{"type": "tool_result", "content": "done"}]}}


def write_log(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "configs").mkdir(parents=True)
    return root, tmp_path / "logs"


def run(root, logs, slugs=("tower", "station")):
    return usage_cost.estimate(root, set(slugs), logs=logs, prices=PRICES)


def test_turns_are_attributed_to_buildings_development_and_other(repo):
    root, logs = repo
    write_log(logs / "s1.jsonl", [
        prompt("2026-01-01T00:00:00Z", "タワーの屋内マップを作って"),
        reply("2026-01-01T00:00:01Z", "m1", [("Bash", {"command": "python main.py build configs/tower.json"})]),
        tool_result("2026-01-01T00:00:02Z"),
        # The same response logged once per content block: counted once.
        reply("2026-01-01T00:00:03Z", "m2", [("Read", {"file_path": "output/tower/selection.json"})]),
        reply("2026-01-01T00:00:03Z", "m2", [("Read", {"file_path": "output/tower/selection.json"})]),
        prompt("2026-01-01T00:01:00Z", "ボタンを付けて"),
        reply("2026-01-01T00:01:01Z", "m3", [("Edit", {"file_path": str(root / "indoor_gen/server.py")}),
                                             ("Bash", {"command": "main.py serve tower"})]),
        prompt("2026-01-01T00:02:00Z", "<bash-input>ls</bash-input>"),  # `!` command: not a turn
        reply("2026-01-01T00:02:01Z", "m4"),
        prompt("2026-01-01T00:03:00Z", "両方の駅を比べて"),
        reply("2026-01-01T00:03:01Z", "m5", [("Bash", {"command": "ls output/station output/tower output/station"})]),
    ])
    b = run(root, logs)["buckets"]
    assert b["tower"]["usd"] == pytest.approx(40 + 20 / 3, abs=1e-3)      # m1, m2 and a third of m5
    assert b["tower"]["turns"] == 2
    assert b["_development"]["usd"] == pytest.approx(40)        # m3 and m4 (the `!` command stays in its turn)
    assert b["station"]["usd"] == pytest.approx(40 / 3, abs=1e-3)
    assert b["tower"]["models"]["claude-opus-5-5"]["output"] == 2_000_000 + round(1_000_000 / 3)
    assert "_other" not in b


@pytest.mark.parametrize("command, dev", [
    ("python3 - <<'EOF'\np = 'indoor_gen/server.py'\ns = open(p).read()\nopen(p, 'w').write(s)\nEOF", True),
    ("cat >> tests/test_fetch.py <<'EOF'\ndef test(): pass\nEOF", True),
    ("sed -i '' 's/a/b/' main.py", True),
    ("python3 - <<'EOF'\nimport json\np = 'configs/tower.json'\njson.dump({}, open(p, 'w'))\nEOF", False),
    (".venv/bin/python main.py build configs/tower.json > /tmp/out.txt 2>&1", False),
    (".venv/bin/python -m pytest -q tests/", False),
])
def test_shell_edits_of_the_tool_count_as_development(command, dev):
    assert usage_cost._shell_edits_tool_code(command) is dev


def test_unlogged_session_cost_follows_web_research(repo):
    root, logs = repo
    write_log(logs / "s1.jsonl", [
        prompt("2026-01-01T00:00:00Z", "タワーを調べて"),
        reply("2026-01-01T00:00:01Z", "m1", [("WebSearch", {"query": "tower floor map"}),
                                             ("Write", {"file_path": str(root / "configs/tower.json")}),
                                             ("Bash", {"command": "main.py build configs/tower.json"})]),
        prompt("2026-01-01T00:01:00Z", "コミットして"),
        reply("2026-01-01T00:01:01Z", "m2", [("Bash", {"command": "git commit -m x"})]),
        {"type": "cost-state", "sessionId": "s1", "totalCostUSD": 43.0},  # 3.0 of Haiku web search not per message
    ])
    b = run(root, logs)["buckets"]
    assert b["tower"]["usd"] == pytest.approx(23.0)
    assert b["tower"]["unlogged_usd"] == pytest.approx(3.0)
    assert b["_other"]["usd"] == pytest.approx(20.0)


def test_subagents_cache_writes_fast_mode_and_unpriced_models(repo):
    root, logs = repo
    write_log(logs / "s1.jsonl", [
        prompt("2026-01-01T00:00:00Z", "駅を作って"),
        reply("2026-01-01T00:00:01Z", "m1", [("Agent", {"prompt": "research station"})], output=0, write_1h=1_000_000),
        reply("2026-01-01T00:00:09Z", "m3", [("Bash", {"command": "main.py build configs/station.json"})],
              output=1_000_000, speed="fast"),
    ])
    write_log(logs / "s1" / "subagents" / "agent-a.jsonl", [
        reply("2026-01-01T00:00:05Z", "m2", output=1_000_000, model="claude-new-model"),
        reply("2026-01-01T00:00:06Z", "m4", output=1_000_000),
    ])
    b = run(root, logs)["buckets"]["station"]
    assert b["usd"] == pytest.approx(8 + 40 + 20)  # 1h cache write, fast output, subagent's priced call
    assert b["unpriced_models"] == ["claude-new-model"]
    assert b["models"]["claude-new-model"]["output"] == 1_000_000


def test_for_slug_without_history_and_log_dir(repo, monkeypatch, tmp_path):
    root, logs = repo
    cost = usage_cost.for_slug(run(root, logs), "tower")
    assert cost["usd"] == 0 and cost["turns"] == 0 and cost["slug"] == "tower"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    assert usage_cost.log_dir(root) == tmp_path / "cc" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root.resolve()))


def test_model_prices_match_dated_ids():
    prices = usage_cost.load_prices()
    assert usage_cost.model_prices("claude-haiku-4-5-20251001", prices)["input"] == 1.0
    assert usage_cost.model_prices("claude-unknown", prices) is None


def test_turn_seconds():
    assert usage_cost._seconds("2026-10-08T02:20:10.500Z", "2026-10-08T02:21:40.500Z") == 90
    assert usage_cost._seconds("2026-10-08T02:21:00Z", "2026-10-08T02:20:00Z") == 0
