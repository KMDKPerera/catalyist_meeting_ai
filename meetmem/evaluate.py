"""Score the agent on my own questions (eval/my_questions.json).

Scoring is plain code so it is repeatable and free:
- citations: does every quote appear word for word in the cited meeting?
- key points: each gold answer is broken into key points, each a regex that
  must match the agent's answer.
A question passes only if all key points match and it has a valid citation.
Regexes can be fooled both ways, so the report prints gold and agent answers
side by side for a human read.
"""
import json
import re
import time

from . import config, quotes
from .agent import Agent


def check_gold(db, questions):
    """The gold evidence must itself be exact, or the eval set is wrong."""
    bad = []
    for q in questions:
        for ev in q["evidence"]:
            if not quotes.verify(db, ev["meeting_id"], ev["quote"]):
                bad.append((q["id"], ev["meeting_id"], ev["quote"]))
    return bad


def score(db, q, res):
    hits = [bool(re.search(p, res["answer"], re.I)) for p in q["key_points"]]
    valid = [quotes.verify(db, c["meeting_id"], c["quote"]) for c in res["citations"]]
    verdict = "PASS" if all(hits) and valid and all(valid) else ("PARTIAL" if any(hits) else "FAIL")
    return {"hits": hits, "citations_valid": valid, "verdict": verdict}


def run(db, llm, path=None, log=print):
    path = path or config.ROOT / "eval" / "my_questions.json"
    questions = json.loads(path.read_text(encoding="utf-8"))
    bad = check_gold(db, questions)
    if bad:
        raise SystemExit(f"Gold evidence is not exact, fix the eval set first: {bad}")
    agent = Agent(db, llm, log=log)
    rows, t0 = [], time.time()
    config.RUNS.mkdir(exist_ok=True)
    cache_path = config.RUNS / "eval_cache.json"  # so a rate-limit crash doesn't redo finished questions
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    for q in questions:
        log(f"  {q['id']} ({q['team']})")
        if q["id"] not in cache:
            cache[q["id"]] = agent.run(q["question"], q["team"])
            cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        res = cache[q["id"]]
        rows.append((q, res, score(db, q, res)))
        log(f"    -> {rows[-1][2]['verdict']}")

    passed = sum(s["verdict"] == "PASS" for _, _, s in rows)
    cites = [v for _, _, s in rows for v in s["citations_valid"]]
    out = [
        "# Eval report: my own questions",
        "",
        f"- Model: `{llm.model}` via {llm.provider}",
        f"- Passed: **{passed} / {len(rows)}** (all key points matched and citations exact)",
        f"- Citations exact: {sum(cites)} / {len(cites)}",
        f"- Average steps per question: {sum(r['steps'] for _, r, _ in rows) / len(rows):.1f}",
        f"- Wall time: {time.time() - t0:.0f}s",
        "",
    ]
    for q, res, s in rows:
        out += [
            f"## {q['id']} {s['verdict']} ({q['team']})",
            f"**Q:** {q['question']}",
            f"**Gold:** {q['gold_answer']}",
            f"**Agent:** {res['answer']}",
            "**Key points:** " + ", ".join(f"{'ok' if h else 'MISSED'} `{p}`" for h, p in zip(s["hits"], q["key_points"])),
            "**Citations:** " + ("; ".join(
                f"{'ok' if v else 'INVALID'} {c['meeting_id']}: \"{c['quote']}\""
                for v, c in zip(s["citations_valid"], res["citations"])) or "none"),
            f"**Steps:** {res['steps']}",
            "",
        ]
    config.RUNS.mkdir(exist_ok=True)
    (config.RUNS / "eval_report.md").write_text("\n".join(out), encoding="utf-8")
    (config.RUNS / "eval_traces.json").write_text(
        json.dumps([{"id": q["id"], **res, **s} for q, res, s in rows], indent=1), encoding="utf-8"
    )
    log(f"\nPassed {passed}/{len(rows)}. Report: {config.RUNS / 'eval_report.md'}")
