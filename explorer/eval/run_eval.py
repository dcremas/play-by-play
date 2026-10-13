"""Ask the test set to one or more models and report accuracy, tokens, cost and time.

TWO IDENTICAL COPIES: pbp/explorer/eval/run_eval.py and
ec2-nginx/weather-sql-explorer/sql_explorer/eval/run_eval.py, kept in step by hand like
llm.py. It works out which app it is in from the files beside it.

    .venv/bin/python eval/run_eval.py MODEL [MODEL ...] [--only ID,ID] [--repeat N] [--yes]

    .venv/bin/python eval/run_eval.py gemini-3.8-flash claude-sonnet-5-5 --yes

MODEL is the same setting PBPX_MODEL / SQLX_MODEL takes (see llm.py). Without --yes it
only prints the plan and a rough cost ceiling, and checks the keys are set: every real run
spends real money, so spending is opt-in.

WHAT IT EXERCISES
-----------------
The app's own `agent.ask` -- the same prompt, tools, step limit and answer extraction a
visitor gets -- against the real MCP servers, which must be running (see run.sh). Each
model runs in its own process, because the model setting is read once, at import.

It never touches the live budget: the ledger is pointed at a throwaway file and the caps
are lifted for the run. The per-question token cap (MAX_..._QUESTION_TOKENS) is lifted
too, so a runaway question shows its true cost here instead of the capped charge.

GRADING
-------
Deterministic, against the answer TEXT -- what a visitor reads, not the rows behind it. A
question passes when every one of its checks passes:

    {"number": 52.8, "tol": 0.3}   some number in the answer is within tol of 52.8
                                   ("signless": true also accepts 27.9 for -27.9)
    {"range": [51.0, 54.0]}        some number in the answer falls in the range
    {"text": ["Cade York"]}        every string appears (case-insensitive)
    {"any": ["in progress", ...]}  at least one appears
    {"none": ["hPa"]}              none appears

A number check can be satisfied by an incidental number, and a phrase check by an
unexpected wording, so read the failures in the report rather than trusting the
percentage alone -- every answer is in it.

Results go to eval/results/<timestamp>/ (gitignored): rows.jsonl with everything, and
report.md with the summary, a pass/fail grid and each failed answer in full.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent
IS_PBP = (APP / "corpus.py").exists()
PREFIX = "PBPX" if IS_PBP else "SQLX"

# For the cost ceiling printed before a run. Deliberately pessimistic: the weather explorer
# measured 17-58k per question on Gemini, and a reasoning model can spend more.
CEILING_TOKENS_PER_QUESTION = 80_000
CEILING_OUTPUT_SHARE = 0.10


# --------------------------------------------------------------------------- #
# Grading
# --------------------------------------------------------------------------- #

_NUMBER = re.compile(r"[-−]?\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    found = []
    for raw in _NUMBER.findall(text):
        cleaned = raw.replace("−", "-").replace(",", "")
        try:
            found.append(float(cleaned))
        except ValueError:
            pass
    return found


def grade(answer: str, checks: list[dict]) -> list[dict]:
    """One {check, passed} per check, in order."""
    lowered = answer.lower()
    nums = numbers_in(answer)
    out = []
    for check in checks:
        if "number" in check:
            want, tol = float(check["number"]), float(check.get("tol", 0))
            ok = any(abs(n - want) <= tol + 1e-9 for n in nums)
            if not ok and check.get("signless"):
                ok = any(abs(abs(n) - abs(want)) <= tol + 1e-9 for n in nums)
        elif "range" in check:
            lo, hi = check["range"]
            ok = any(lo <= n <= hi for n in nums)
        elif "text" in check:
            ok = all(s.lower() in lowered for s in check["text"])
        elif "any" in check:
            ok = any(s.lower() in lowered for s in check["any"])
        elif "none" in check:
            ok = not any(s.lower() in lowered for s in check["none"])
        else:
            raise ValueError(f"unknown check {check}")
        out.append({"check": check, "passed": ok})
    return out


# --------------------------------------------------------------------------- #
# The worker: one model, in its own process
# --------------------------------------------------------------------------- #

def worker(model: str, questions: list[dict], repeat: int, out_path: Path) -> None:
    os.environ[f"{PREFIX}_MODEL"] = model
    ledger = Path(tempfile.mkdtemp(prefix="eval-ledger-")) / "budget.json"
    os.environ[f"{PREFIX}_STATE_PATH"] = str(ledger)
    os.environ[f"{PREFIX}_MAX_DAILY_TOKENS"] = str(10**12)
    os.environ[f"{PREFIX}_MAX_SESSION_QUESTIONS"] = str(10**6)
    os.environ[f"{PREFIX}_MAX_QUESTION_TOKENS"] = str(10**9)
    sys.path.insert(0, str(APP))
    os.chdir(APP)

    import agent  # imported here: the model setting must be in the environment first

    # Split the measured tokens into input / cached / output for the cost. The app's
    # ledger only needs the total, so it never kept the split; this wraps the one
    # function that sees every message, and returns exactly what it returned before.
    split: dict[str, int] = {}
    original = agent._tokens_from

    def _tokens_from(messages: list) -> int:
        split.clear()
        for m in messages:
            u = getattr(m, "usage_metadata", None)
            if isinstance(u, dict):
                split["input"] = split.get("input", 0) + int(u.get("input_tokens") or 0)
                split["output"] = split.get("output", 0) + int(u.get("output_tokens") or 0)
                cached = (u.get("input_token_details") or {}).get("cache_read") or 0
                split["cached"] = split.get("cached", 0) + int(cached)
        return original(messages)

    agent._tokens_from = _tokens_from

    if IS_PBP:
        import corpus

        def ask(q: dict) -> dict:
            r = agent.ask(corpus.resolve(q["corpus"]), q["question"], 0)
            return {"answer": r["answer"], "tokens": r["tokens"],
                    "sql": [x.get("asked", "") for x in r["queries"]]}
    else:
        built, _ = agent.build_agent()

        def ask(q: dict) -> dict:
            r = agent.ask(built, q["question"], 0)
            return {"answer": r["answer"], "tokens": r["tokens"],
                    "sql": [x.get("requested_sql", "") for x in r["queries"]]}

    with out_path.open("a") as out:
        for attempt in range(1, repeat + 1):
            for q in questions:
                row = {"model": model, "id": q["id"], "attempt": attempt,
                       "question": q["question"]}
                started = time.monotonic()
                for retry in range(2):
                    try:
                        split.clear()
                        row.update(ask(q))
                        row.pop("error", None)
                        break
                    except Exception as exc:  # noqa: BLE001 -- recorded, never fatal
                        row.update(answer="", tokens=None, sql=[],
                                   error=f"{type(exc).__name__}: {exc}"[:2000])
                        # A provider's rate limit: wait it out once rather than failing
                        # every remaining question inside the app's one-minute cooldown.
                        if retry == 0 and getattr(agent, "model_status", lambda: "")() == "rate":
                            time.sleep(65)
                            agent._unavailable_until = 0.0
                            continue
                        break
                row["seconds"] = round(time.monotonic() - started, 1)
                row["usage"] = dict(split)
                row["checks"] = grade(row.get("answer") or "", q["checks"])
                row["passed"] = not row.get("error") and all(c["passed"] for c in row["checks"])
                out.write(json.dumps(row) + "\n")
                out.flush()
                mark = "PASS" if row["passed"] else ("ERROR" if row.get("error") else "fail")
                print(f"  [{model}] {q['id']:28} {mark:5} {row['seconds']:6.1f}s "
                      f"{row.get('tokens') or 0:>8,} tok", flush=True)


# --------------------------------------------------------------------------- #
# The parent: plan, spawn, report
# --------------------------------------------------------------------------- #

def load_prices() -> dict:
    path = HERE / "prices.json"
    return json.loads(path.read_text()).get("prices", {}) if path.exists() else {}


def cost_of(usage: dict, price: dict | None) -> float | None:
    if not price or not usage:
        return None
    cached = usage.get("cached", 0)
    fresh = max(usage.get("input", 0) - cached, 0)
    return (fresh * price["input"] + cached * price.get("cached_input", price["input"])
            + usage.get("output", 0) * price["output"]) / 1e6


def missing_key(model: str) -> str | None:
    """The key variable a model needs and does not have, read the way llm.py reads it."""
    sys.path.insert(0, str(APP))
    import llm

    choice = llm.parse(model)
    if choice.provider == "google":
        names = (("PBPX_API_KEY", "GOOGLE_API_KEY") if IS_PBP else ("GOOGLE_API_KEY",))
    elif choice.provider == "anthropic":
        names = ("ANTHROPIC_API_KEY",)
    elif choice.provider in llm.COMPATIBLE:
        names = (llm.COMPATIBLE[choice.provider][1],)
    else:
        return "(no provider -- write it as provider:model; see llm.py)"
    env = dict(os.environ)
    env_file = APP / ".env"           # the app reads this at import; so does this check
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return None if any(env.get(n, "").strip() for n in names) else " or ".join(names)


def report(rows: list[dict], models: list[str], questions: list[dict], out_dir: Path) -> str:
    prices = load_prices()
    attempts = max((r["attempt"] for r in rows), default=1)
    lines = [f"# Test set results — {'Football SQL' if IS_PBP else 'Weather SQL Explorer'}",
             "", f"Run {out_dir.name}: {len(questions)} questions, {attempts} attempt(s) each.",
             "", "| Model | Passed | Errors | Tokens / question | Cost / question | Seconds / question |",
             "|---|---|---|---|---|---|"]
    for m in models:
        mine = [r for r in rows if r["model"] == m]
        if not mine:
            continue
        passed = sum(r["passed"] for r in mine)
        errors = sum(bool(r.get("error")) for r in mine)
        toks = [r["tokens"] for r in mine if r.get("tokens")]
        costs = [c for c in (cost_of(r.get("usage") or {}, prices.get(m)) for r in mine)
                 if c is not None]
        secs = [r["seconds"] for r in mine]
        tok = f"{sum(toks) / len(toks):,.0f}" if toks else "n/a"
        cost = f"${sum(costs) / len(costs):.4f}" if costs else "n/a"
        lines.append(f"| `{m}` | {passed}/{len(mine)} ({100 * passed / len(mine):.0f}%) "
                     f"| {errors} | {tok} | {cost} | {sum(secs) / len(secs):.1f} |")

    lines += ["", "## Per question", "", "| Question | " + " | ".join(f"`{m}`" for m in models) + " |",
              "|---|" + "---|" * len(models)]
    for q in questions:
        cells = []
        for m in models:
            mine = [r for r in rows if r["model"] == m and r["id"] == q["id"]]
            cells.append(" ".join("✅" if r["passed"] else ("⚠️" if r.get("error") else "❌")
                                  for r in mine) or "–")
        lines.append(f"| {q['id']} | " + " | ".join(cells) + " |")

    lines += ["", "## Failures, in full", "",
              ("Read these before trusting the percentages: a check can fail on wording "
               "and pass on an incidental number.")]
    by_id = {q["id"]: q for q in questions}
    for r in rows:
        if r["passed"]:
            continue
        q = by_id[r["id"]]
        failed = [c["check"] for c in r["checks"] if not c["passed"]]
        lines += ["", f"### `{r['model']}` — {r['id']} (attempt {r['attempt']})", "",
                  f"**Q:** {q['question']}", "",
                  f"**Trap:** {q.get('trap', '')}", ""]
        if r.get("error"):
            lines += [f"**Error:** `{r['error'][:400]}`"]
        else:
            lines += [f"**Failed checks:** `{json.dumps(failed)}`", "",
                      "**Answer:**", "", "> " + (r["answer"] or "(empty)").replace("\n", "\n> ")]
        for sql in r.get("sql") or []:
            lines += ["", "```sql", sql.strip(), "```"]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("models", nargs="*", help="model settings, as PBPX_MODEL / SQLX_MODEL takes")
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--repeat", type=int, default=1, help="ask each question N times")
    ap.add_argument("--yes", action="store_true", help="actually run (spends money)")
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    ap.add_argument("--out", help=argparse.SUPPRESS)
    args = ap.parse_args()

    questions = json.loads((HERE / "questions.json").read_text())["questions"]
    if args.only:
        wanted = set(args.only.split(","))
        unknown = wanted - {q["id"] for q in questions}
        if unknown:
            sys.exit(f"unknown question id(s): {sorted(unknown)}")
        questions = [q for q in questions if q["id"] in wanted]

    if args.worker:
        worker(args.worker, questions, args.repeat, Path(args.out))
        return
    if not args.models:
        ap.error("name at least one model")

    prices = load_prices()
    n = len(questions) * args.repeat
    print(f"{'Football SQL' if IS_PBP else 'Weather SQL Explorer'}: {len(questions)} questions"
          f" x {args.repeat} x {len(args.models)} model(s) = {n * len(args.models)} questions asked.")
    problems = []
    for m in args.models:
        price = prices.get(m)
        ceiling = cost_of({"input": CEILING_TOKENS_PER_QUESTION * n * (1 - CEILING_OUTPUT_SHARE),
                           "output": CEILING_TOKENS_PER_QUESTION * n * CEILING_OUTPUT_SHARE},
                          price)
        need = missing_key(m)
        if need:
            problems.append(f"{m}: {need} is not set")
        print(f"  {m:32} cost ceiling {f'~${ceiling:.2f}' if ceiling is not None else 'n/a (no price in prices.json)'}"
              f"{'   MISSING KEY: ' + need if need else ''}")
    if problems:
        sys.exit("\nNot running: " + "; ".join(problems))
    if not args.yes:
        print("\nThe ceiling assumes 80k tokens per question with no caching; real runs have "
              "come in well under it.\nNothing was sent. Add --yes to run.")
        return

    out_dir = HERE / "results" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    rows_path = out_dir / "rows.jsonl"
    rows_path.touch()       # so a worker that dies before its first row still yields a report
    for m in args.models:
        print(f"\n== {m}")
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", m,
               "--out", str(rows_path), "--repeat", str(args.repeat)]
        if args.only:
            cmd += ["--only", args.only]
        if subprocess.run(cmd, check=False).returncode:
            print(f"   the {m} worker exited with an error; its questions are missing below")

    rows = [json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
    text = report(rows, args.models, questions, out_dir)
    (out_dir / "report.md").write_text(text)
    print("\n" + text.split("\n## Per question")[0])
    print(f"Full report: {out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
