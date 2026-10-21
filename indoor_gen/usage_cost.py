"""Estimate what each indoor map cost to make, from Claude Code's session logs.

Claude Code writes every API response's token usage to
~/.claude/projects/<project dir>/<session>.jsonl (subagents under <session>/subagents/).
Usage is priced with pricing.json at API list prices, so on a subscription this is the
API-equivalent cost, not a bill.

Attribution is per turn (one user prompt up to the next):
- a turn that edits files of the tool itself (anything in the repository outside configs/ and
  output/, with Edit/Write or a shell command that writes to one) is development;
- otherwise it belongs to the buildings whose slugs its tool calls mention, split by mention count;
- anything else is "other".
Claude Code's own session totals (`cost-state` lines) also include calls it does not log per
message (Haiku for WebSearch / WebFetch); that remainder is spread over the session's turns by
their WebSearch / WebFetch calls.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PRICING = Path(__file__).parent / "pricing.json"
DEV = "_development"
OTHER = "_other"
TOKENS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output", "web_search")
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
WEB_TOOLS = {"WebSearch", "WebFetch"}
NOT_PROMPTS = ("<bash-input>", "<bash-stdout>", "<local-command-")  # `!` commands and their output
TOOL_FILE = r"(?:\./)?(?:indoor_gen/[\w/.-]+|tests/[\w/.-]+|\.claude/[\w/.-]+|main\.py|CLAUDE\.md|requirements\.txt)"
# Shell commands that write a file of the tool: a quoted path in a script that writes, or a redirect / sed -i.
QUOTED_TOOL_FILE = re.compile(r"""['"]""" + TOOL_FILE + r"""['"]""")
SCRIPT_WRITE = re.compile(r"write_text|\.write\(|json\.dump\(")
SHELL_WRITE = re.compile(r"(?:>>?|sed -i(?: '')?\s+(?:'[^']*'|\S+))\s*" + TOOL_FILE + r"(?![\w/.-])")


def _shell_edits_tool_code(command: str) -> bool:
    return bool(SHELL_WRITE.search(command) or (QUOTED_TOOL_FILE.search(command) and SCRIPT_WRITE.search(command)))


def log_dir(project_root: Path) -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return base / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project_root.resolve()))


def load_prices(path: Path = PRICING) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def model_prices(model: str, prices: dict) -> dict | None:
    keys = [k for k in prices["models"] if model.startswith(k)]
    return prices["models"][max(keys, key=len)] if keys else None


def usage_tokens(usage: dict) -> Counter:
    split = usage.get("cache_creation") or {}
    write_1h = split.get("ephemeral_1h_input_tokens", 0)
    return Counter({
        "input": usage.get("input_tokens", 0),
        "cache_write_5m": usage.get("cache_creation_input_tokens", 0) - write_1h,
        "cache_write_1h": write_1h,
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "web_search": (usage.get("server_tool_use") or {}).get("web_search_requests", 0),
    })


def tokens_cost(model: str, tokens: Counter, prices: dict, fast: bool = False) -> float | None:
    p = model_prices(model, prices)
    if p is None:
        return None
    usd = sum(tokens[k] * p[k] for k in TOKENS if k != "web_search") / 1e6
    usd *= p.get("fast_multiplier", 1.0) if fast else 1.0
    return usd + tokens["web_search"] * prices["web_search_per_1k"] / 1000


@dataclass
class Turn:
    session: str
    start: str
    end: str
    mentions: Counter = field(default_factory=Counter)
    dev: bool = False
    web_calls: int = 0
    tokens: dict = field(default_factory=lambda: defaultdict(Counter))  # model -> Counter
    usd: float = 0.0
    unlogged_usd: float = 0.0
    unpriced: set = field(default_factory=set)


def _seconds(start: str, end: str) -> float:
    """Length of a turn (prompt to its last message) in seconds."""
    def parse(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    return max(0.0, (parse(end) - parse(start)).total_seconds())


def _is_prompt(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        return not content.startswith(NOT_PROMPTS)
    return bool(content) and all(c.get("type") != "tool_result" for c in content)


def _edits_tool_code(path: str, root: Path) -> bool:
    p = Path(path) if Path(path).is_absolute() else root / path
    try:
        rel = p.resolve().relative_to(root)
    except ValueError:
        return False  # scratch files outside the repository
    return rel.parts[:1] not in (("configs",), ("output",))


def _read(files: list[Path]) -> tuple[list[tuple[str, int, dict]], dict[str, float]]:
    """(timestamp, is_main_log, entry) for prompts and assistant messages; last cost-state total per session."""
    events, totals = [], {}
    for path in files:
        main = path.parent.name != "subagents"
        with path.open("rb") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a line still being written
                if e.get("type") == "cost-state":
                    totals[e["sessionId"]] = e.get("totalCostUSD") or 0.0
                elif e.get("timestamp") and (e.get("type") == "assistant" or (main and _is_prompt(e))):
                    events.append((e["timestamp"], int(main), e))
    events.sort(key=lambda t: (t[2].get("sessionId", ""), t[0]))
    return events, totals


def turns(logs: Path, root: Path, slugs: set[str], prices: dict) -> list[Turn]:
    files = sorted(logs.glob("*.jsonl")) + sorted(logs.glob("*/subagents/*.jsonl"))
    events, totals = _read(files)
    slug_re = re.compile(r"(?<![\w-])(" + "|".join(map(re.escape, sorted(slugs, key=len, reverse=True)))
                         + r")(?![\w-])") if slugs else None
    root = root.resolve()

    out: list[Turn] = []
    current: dict[str, Turn] = {}
    last_usage: dict[str, tuple[Turn, str, Counter, bool]] = {}  # message id -> where its usage went
    for ts, main, e in events:
        sid = e.get("sessionId", "")
        if e["type"] == "user":
            current[sid] = Turn(sid, ts, ts)
            out.append(current[sid])
            continue
        turn = current.get(sid)
        if turn is None:  # messages before the first prompt (e.g. a resumed session's preamble)
            turn = current[sid] = Turn(sid, ts, ts)
            out.append(turn)
        turn.end = max(turn.end, ts)
        msg = e.get("message") or {}
        for block in msg.get("content") or []:
            if block.get("type") != "tool_use":
                continue
            name, args = block.get("name"), block.get("input") or {}
            turn.web_calls += name in WEB_TOOLS
            if name in EDIT_TOOLS and _edits_tool_code(args.get("file_path") or args.get("notebook_path") or "", root):
                turn.dev = True
            elif name == "Bash" and _shell_edits_tool_code(args.get("command") or ""):
                turn.dev = True
            if slug_re:
                turn.mentions.update(slug_re.findall(json.dumps(args, ensure_ascii=False)))
        usage, model = msg.get("usage"), msg.get("model") or ""
        if not usage or model.startswith("<"):
            continue
        # One response is logged once per content block, each line carrying the same usage: count it once.
        key = msg.get("id") or e.get("requestId") or ts
        if key in last_usage:
            old_turn, old_model, old_tokens, _ = last_usage[key]
            old_turn.tokens[old_model].subtract(old_tokens)
        tokens = usage_tokens(usage)
        turn.tokens[model].update(tokens)
        last_usage[key] = (turn, model, tokens, usage.get("speed") == "fast")

    for turn, model, tokens, fast in last_usage.values():
        usd = tokens_cost(model, tokens, prices, fast)
        if usd is None:
            turn.unpriced.add(model)
        else:
            turn.usd += usd

    by_session = defaultdict(list)
    for t in out:
        by_session[t.session].append(t)
    for sid, ts_ in by_session.items():
        unlogged = totals.get(sid, 0.0) - sum(t.usd for t in ts_)
        if unlogged <= 0:
            continue
        weights = [t.web_calls for t in ts_]
        if not any(weights):
            weights = [t.usd for t in ts_]
        total = sum(weights) or 1
        for t, w in zip(ts_, weights):
            t.unlogged_usd = unlogged * w / total
    return out


def _bucket_shares(turn: Turn) -> dict[str, float]:
    if turn.dev:
        return {DEV: 1.0}
    n = sum(turn.mentions.values())
    return {s: c / n for s, c in turn.mentions.items()} if n else {OTHER: 1.0}


def summarise(turn_list: list[Turn]) -> dict[str, dict]:
    """Bucket (slug, DEV or OTHER) -> cost summary."""
    acc: dict[str, dict] = {}
    for t in turn_list:
        if not t.usd and not t.unlogged_usd and not t.tokens:
            continue
        for bucket, share in _bucket_shares(t).items():
            b = acc.setdefault(bucket, {"usd": 0.0, "logged_usd": 0.0, "unlogged_usd": 0.0, "turns": 0,
                                        "active_seconds": 0.0, "sessions": set(), "first": t.start, "last": t.end,
                                        "models": defaultdict(Counter), "unpriced_models": set()})
            b["logged_usd"] += t.usd * share
            b["unlogged_usd"] += t.unlogged_usd * share
            b["turns"] += 1
            b["active_seconds"] += _seconds(t.start, t.end) * share
            b["sessions"].add(t.session)
            b["first"], b["last"] = min(b["first"], t.start), max(b["last"], t.end)
            for model, tokens in t.tokens.items():
                b["models"][model].update({k: v * share for k, v in tokens.items()})
            b["unpriced_models"] |= t.unpriced
    for b in acc.values():
        b["usd"] = round(b["logged_usd"] + b["unlogged_usd"], 4)
        b["logged_usd"], b["unlogged_usd"] = round(b["logged_usd"], 4), round(b["unlogged_usd"], 4)
        b["active_seconds"] = round(b["active_seconds"])
        b["sessions"] = sorted(b["sessions"])
        b["unpriced_models"] = sorted(b["unpriced_models"])
        b["models"] = {m: {k: round(v) for k, v in c.items() if k in TOKENS and round(v)}
                       for m, c in sorted(b["models"].items())}
    return acc


def known_slugs(root: Path) -> set[str]:
    """Slugs of the built outputs and of the configs, the names turns are matched against."""
    from indoor_gen import config

    out = root / "output"
    slugs = {p.name for p in out.iterdir() if p.is_dir()} if out.is_dir() else set()
    for p in [*root.glob("configs/*.json"), *root.glob("samples/*.json")]:
        try:
            slugs.add(config.slug_for(p, config.load_raw(p)))
        except config.ConfigError:
            pass
    return slugs


def for_slug(report: dict, slug: str) -> dict:
    """One building's cost.json: its bucket plus the report's context."""
    empty = {"usd": 0.0, "logged_usd": 0.0, "unlogged_usd": 0.0, "turns": 0, "active_seconds": 0, "sessions": [],
             "first": None, "last": None, "models": {}, "unpriced_models": []}
    context = {k: report[k] for k in ("computed_at", "log_dir", "prices_as_of", "note")}
    return {"slug": slug, **context, **report["buckets"].get(slug, empty),
            "development_usd": report["buckets"].get(DEV, {}).get("usd", 0.0)}


def estimate(root: Path, slugs: set[str], logs: Path | None = None, prices: dict | None = None) -> dict:
    """Cost summary per bucket plus the context needed to read it."""
    logs = logs or log_dir(root)
    prices = prices or load_prices()
    buckets = summarise(turns(logs, root, slugs, prices)) if logs.is_dir() else {}
    return {"computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "log_dir": str(logs), "prices_as_of": prices.get("as_of"),
            "note": "API list-price equivalent estimated from Claude Code session logs, not a bill",
            "total_usd": round(sum(b["usd"] for b in buckets.values()), 4),
            "buckets": dict(sorted(buckets.items(), key=lambda kv: -kv[1]["usd"]))}
