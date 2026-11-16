"""Command line for meetmem. Run `python main.py -h`."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meetmem import brief, config, embed, evaluate, extract, intake, quotes, reconcile, store  # noqa: E402
from meetmem.agent import Agent  # noqa: E402
from meetmem.llm import LLM, reviewer  # noqa: E402


def cmd_ingest(db, a):
    n = store.ingest(db)
    m, t, w = db.execute("SELECT COUNT(*), SUM(n_turns), SUM(n_words) FROM meetings").fetchone()
    c = db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    print(f"Added {n} meetings. Store: {m} meetings, {t} turns, {w} words, {c} chunks.")


def cmd_extract(db, a):
    n = extract.run(db, LLM(db), a.team, a.limit, redo=getattr(a, "redo", False))
    print(f"Extracted {n} meetings. Ledger: {db.execute('SELECT COUNT(*) FROM items').fetchone()[0]} items.")


def cmd_reconcile(db, a):
    reconcile.run(db, LLM(db), a.team)


def cmd_embed(db, a):
    n = embed.build(db)
    total = db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    print(f"Embedded {n} new passages and items. Meaning search covers {total}.")


def cmd_build(db, a):
    cmd_ingest(db, a)
    cmd_extract(db, a)
    cmd_reconcile(db, a)
    cmd_embed(db, a)


def cmd_add(db, a):
    """Add new transcript files: check, store, extract, and re-link only the teams they touch."""
    if len(a.files) > 1 and (a.team or a.number):
        raise SystemExit("--team and --number describe one meeting; add files one at a time when using them.")
    added = []
    for f in a.files:
        path = Path(f)
        try:
            text = path.read_text(encoding="utf-8")
            if a.check:
                p = intake.preview(db, text, path.stem, a.team, a.number)
                where = f"new team {p['team']}" if p["new_team"] else f"team {p['team']} (has {', '.join(p['team_meetings'])})"
                print(f"{path.name}: OK. {p['meeting_id']}, meeting {p['number']} of {where}; "
                      f"{p['turns']} turns, {p['words']} words, speakers {', '.join(p['speakers'])}"
                      + ("; ALREADY IN STORE (needs --replace)" if p["exists"] else ""))
                continue
            info = intake.add(db, text, path.stem, a.team, a.number, replace=a.replace)
        except (store.TranscriptError, OSError, UnicodeDecodeError) as e:
            print(f"{path.name}: NOT ADDED. {e}")
            continue
        added.append(info["meeting_id"])
        print(f"{path.name}: stored as {info['meeting_id']} (team {info['team']}, meeting {info['idx']}), "
              f"{len(info['turns'])} turns, {info['words']} words. Searchable now.")
    if added and not a.no_model:
        teams = intake.process(db, LLM(db), added)
        print(f"Ledger and threads updated for: {', '.join(teams)}")
    elif added:
        print("Skipped the model steps. Run `python main.py build` to extract and link them.")


def cmd_search(db, a):
    team = a.team or store.find_team(a.query, db)
    items, mode = embed.search_items(db, a.query, team)
    print(f"LEDGER ({mode})")
    for r in items:
        print(" ", store.fmt_item(db, r))
    print("\nTRANSCRIPT")
    for r in embed.search_chunks(db, a.query, team, k=a.k)[0]:
        print(f"--- {r['meeting_id']} turns {r['turn_start']}-{r['turn_end']}\n{r['text']}\n")


def cmd_read(db, a):
    for t in store.get_turns(db, a.meeting, a.start, a.end):
        print(f"[{t['turn']}] {t['t']} {t['speaker']}: {t['text']}")


def cmd_timeline(db, a):
    as_of = store.meeting_index(db, a.as_of)
    print(reconcile.timeline(db, a.team, a.topic, as_of, max_threads=4 if a.topic else 999))


def cmd_ask(db, a):
    res = Agent(db, LLM(db), log=print).run(a.question, a.team)
    print("\n" + res["answer"])
    for c in res["citations"]:
        print(f'  [{c["meeting_id"]}] "{c["quote"]}"')
    if res["unverified"]:
        print("  WARNING:", res["unverified"])


def cmd_answer_all(db, a):
    """Run the 25 questions and the brief, write answers.json. Resumable."""
    spec = json.loads((config.DATA / "questions.json").read_text(encoding="utf-8"))
    config.RUNS.mkdir(exist_ok=True)
    cache_path = config.RUNS / "answers_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    llm = LLM(db)
    agent = Agent(db, llm, log=print)
    for q in spec["questions"]:
        qid = q["question_id"]
        if qid in cache and not a.redo:
            continue
        print(f"{qid} ({q['team']})")
        cache[qid] = agent.run(q["question"], q["team"])
        cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    if "brief" not in cache or a.redo:
        print("Brief")
        # One call that a whole team reads: worth the stronger model when there is quota for it.
        cache["brief"] = brief.build(db, reviewer(db) or llm, spec["brief_task"]["team"])
        cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    out = {
        "answers": [
            {"question_id": q["question_id"], "answer": cache[q["question_id"]]["answer"],
             "citations": cache[q["question_id"]]["citations"]}
            for q in spec["questions"]
        ],
        "brief": cache["brief"],
    }
    Path(a.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {a.out}")
    a.file = a.out
    cmd_verify(db, a)


def _passes():
    """Every saved pass of answers, oldest first: answers_cache_v1.json, _v2.json, ... then the current one."""
    old = sorted(config.RUNS.glob("answers_cache_v*.json"), key=lambda p: int(p.stem.rsplit("v", 1)[1]))
    return [json.loads(p.read_text(encoding="utf-8")) for p in old + [config.RUNS / "answers_cache.json"]]


def cmd_adjudicate(db, a):
    """Several full passes of the agent, one judge per question -> answers.json.

    To add a pass: copy runs/answers_cache.json to answers_cache_vN.json, then
    `answer-all --redo`. Resumable; delete runs/adjudication.json to judge again.
    """
    spec = json.loads((config.DATA / "questions.json").read_text(encoding="utf-8"))
    passes = _passes()
    out_path = config.RUNS / "adjudication.json"
    picks = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    agent = Agent(db, LLM(db), log=print)
    for n, q in enumerate(spec["questions"]):
        qid = q["question_id"]
        if qid not in picks:
            # Code rule: a draft with no verified citation cannot win.
            usable = [i for i, p in enumerate(passes) if qid in p and p[qid]["citations"]]
            if len(usable) == 1:
                picks[qid] = {"pick": usable[0] + 1, "why": "the only pass with a verified citation"}
            else:
                # Rotate which pass is shown first, so a judge that favours a position cannot favour a pass.
                order = usable[n % len(usable):] + usable[:n % len(usable)]
                i, why = agent.adjudicate(q["question"], [passes[j][qid] for j in order], q["team"])
                picks[qid] = {"pick": order[i] + 1, "why": why, "judge": agent.review_llm.model, "of": len(passes)}
            out_path.write_text(json.dumps(picks, indent=1), encoding="utf-8")
        print(f"{qid}: pass {picks[qid]['pick']}  {picks[qid]['why'][:150]}")
    chosen = lambda qid: passes[picks[qid]["pick"] - 1][qid]
    final = {
        "answers": [
            {"question_id": q["question_id"], "answer": chosen(q["question_id"])["answer"],
             "citations": chosen(q["question_id"])["citations"]}
            for q in spec["questions"]
        ],
        "brief": passes[-1]["brief"],
    }
    Path(a.out).write_text(json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8")
    tally = ", ".join(f"{sum(p['pick'] == i + 1 for p in picks.values())} from pass {i + 1}" for i in range(len(passes)))
    print(f"Wrote {a.out}: {tally}")
    a.file = a.out
    cmd_verify(db, a)


def cmd_brief(db, a):
    print(brief.build(db, reviewer(db) or LLM(db), a.team))


def cmd_verify(db, a):
    """Re-check a finished answers.json the way the grader will."""
    data = json.loads(Path(a.file).read_text(encoding="utf-8"))
    total = bad = 0
    for ans in data["answers"]:
        if not 1 <= len(ans["citations"]) <= 3:
            print(f"{ans['question_id']}: {len(ans['citations'])} citations (need 1 to 3)")
        for c in ans["citations"]:
            total += 1
            if not quotes.verify(db, c["meeting_id"], c["quote"]):
                bad += 1
                print(f"{ans['question_id']}: NOT EXACT {c['meeting_id']}: {c['quote'][:70]}")
    print(f"Citations exact: {total - bad}/{total}")


def cmd_eval(db, a):
    evaluate.run(db, LLM(db))


def cmd_stats(db, a):
    """Measured tokens and time per stage, scaled to a target corpus size."""
    n_done = db.execute("SELECT COUNT(DISTINCT meeting_id) FROM extracted").fetchone()[0]
    print(f"{'stage':<10}{'calls':>7}{'in tok':>11}{'out tok':>10}{'seconds':>9}")
    for r in db.execute(
        "SELECT purpose, COUNT(*) n, SUM(prompt_tokens) i, SUM(completion_tokens) o, SUM(seconds) s "
        "FROM llm_calls GROUP BY purpose"
    ):
        print(f"{r['purpose']:<10}{r['n']:>7}{r['i']:>11}{r['o']:>10}{r['s']:>9.0f}")
    r = db.execute(
        "SELECT SUM(prompt_tokens) i, SUM(completion_tokens) o, SUM(seconds) s, COUNT(*) n FROM llm_calls "
        "WHERE purpose IN ('extract', 'reconcile')"
    ).fetchone()
    if not n_done or not r["n"]:
        print("\nNo extraction logged yet.")
        return
    k = a.meetings / n_done
    cost = (r["i"] * a.price_in + r["o"] * a.price_out) / 1e6 * k
    print(
        f"\nIngest so far: {n_done} meetings, {r['n']} calls, {r['s']:.0f}s of model time.\n"
        f"Scaled to {a.meetings} meetings: {r['i'] * k / 1e6:.1f}M input + {r['o'] * k / 1e6:.1f}M output tokens, "
        f"${cost:.2f} at ${a.price_in}/${a.price_out} per 1M tokens, "
        f"{r['s'] * k / 3600:.1f}h of model time run one call at a time."
    )


def cmd_serve(db, a):
    from meetmem import server
    db.close()
    server.serve(a.port)


def cmd_models(db, a):
    llm = LLM(db)
    print(f"provider={llm.provider} model={llm.model}")
    print("\n".join(llm.models()))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Meeting memory agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help):
        s = sub.add_parser(name, help=help)
        s.set_defaults(fn=fn)
        return s

    add("ingest", cmd_ingest, "parse and index new transcripts (no model)")
    s = add("add", cmd_add, "add new transcript files to the memory")
    s.add_argument("files", nargs="+", help="transcript .txt files")
    s.add_argument("--team", help="team name, if the file does not say")
    s.add_argument("--number", type=int, help="the meeting's place in the team's order, if the file does not say")
    s.add_argument("--replace", action="store_true", help="reload a meeting that is already in the store")
    s.add_argument("--check", action="store_true", help="only check the files; change nothing")
    s.add_argument("--no-model", action="store_true", help="store and index only; extract later with build")
    s = add("extract", cmd_extract, "model reads each meeting once into the ledger")
    s.add_argument("--team")
    s.add_argument("--limit", type=int, help="stop after N meetings")
    s.add_argument("--redo", action="store_true", help="with --team: drop that team's items and extract again")
    add("reconcile", cmd_reconcile, "link ledger items into threads and supersessions").add_argument("--team")
    add("embed", cmd_embed, "add new passages and items to meaning search")
    s = add("build", cmd_build, "ingest + extract + reconcile + embed")
    s.add_argument("--team")
    s.add_argument("--limit", type=int)
    s = add("search", cmd_search, "raw retrieval, no model (for debugging)")
    s.add_argument("query")
    s.add_argument("--team")
    s.add_argument("-k", type=int, default=4)
    s = add("read", cmd_read, "print turns of a meeting")
    s.add_argument("meeting")
    s.add_argument("start", type=int)
    s.add_argument("end", type=int)
    s = add("timeline", cmd_timeline, "how a team's decisions changed over time")
    s.add_argument("team")
    s.add_argument("--topic")
    s.add_argument("--as-of", help="hide everything after this meeting: a letter, a number or a meeting id")
    s = add("ask", cmd_ask, "ask the agent one question")
    s.add_argument("question")
    s.add_argument("--team")
    s = add("answer-all", cmd_answer_all, "answer questions.json + brief -> answers.json")
    s.add_argument("--out", default=str(config.ROOT / "answers.json"))
    s.add_argument("--redo", action="store_true")
    add("adjudicate", cmd_adjudicate, "judge two passes of answers question by question -> answers.json").add_argument(
        "--out", default=str(config.ROOT / "answers.json"))
    add("brief", cmd_brief, "pre-meeting brief for a team").add_argument("team", nargs="?", default="TS3012")
    add("verify", cmd_verify, "check citations in an answers.json").add_argument(
        "file", nargs="?", default=str(config.ROOT / "answers.json"))
    add("eval", cmd_eval, "score the agent on eval/my_questions.json")
    s = add("stats", cmd_stats, "measured tokens/time, scaled to N meetings")
    s.add_argument("--meetings", type=int, default=1000)
    s.add_argument("--price-in", type=float, default=0.30, help="USD per 1M input tokens")
    s.add_argument("--price-out", type=float, default=2.50, help="USD per 1M output tokens")
    add("models", cmd_models, "list the provider's model ids")
    add("serve", cmd_serve, "local web UI").add_argument("--port", type=int, default=8000)

    a = p.parse_args()
    db = store.connect()
    a.fn(db, a)


if __name__ == "__main__":
    main()
