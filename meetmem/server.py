"""Local web front end. Standard library only: `python main.py serve`.

A thin JSON API over the same functions the command line uses, plus one static
page (web/index.html). Asking a question streams the agent's steps to the
browser as they happen, one JSON object per line.
"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, embed, intake, quotes, store
from .agent import Agent
from .llm import LLM, LLMError

WEB = config.ROOT / "web"
MAX_UPLOAD = 5 * 1024 * 1024


def teams(db):
    out = {}
    for m in db.execute("SELECT * FROM meetings ORDER BY team, idx"):
        out.setdefault(m["team"], []).append(m["meeting_id"])
    return [{"team": t, "meetings": ms} for t, ms in out.items()]


def item_json(db, it, as_of=None):
    sup = store.superseded_by(db, it["item_id"], as_of)
    return {
        "id": it["item_id"], "meeting_id": it["meeting_id"], "turn": it["turn"], "time": it["t"],
        "speaker": it["speaker"], "kind": it["kind"], "topic": it["topic"], "statement": it["statement"],
        "quote": it["quote"], "owner": it["owner"],
        "superseded_by": {"id": sup["item_id"], "meeting_id": sup["meeting_id"]} if sup else None,
        # Earlier items this one replaced. Never from the future: a replacement is always the later item.
        "replaces": [r[0] for r in db.execute("SELECT old_id FROM supersedes WHERE new_id=?", (it["item_id"],))],
    }


def timeline(db, team, as_of=None):
    full, as_of = as_of is None, as_of or 10**9
    out = []
    for th in db.execute("SELECT * FROM threads WHERE team=? ORDER BY thread_id", (team,)).fetchall():
        items = db.execute(
            "SELECT * FROM items WHERE thread_id=? AND idx<=? ORDER BY idx, turn", (th["thread_id"], as_of)
        ).fetchall()
        if items:
            rows = [item_json(db, it, as_of) for it in items]
            # "Stands" is claimed only where the links prove it: this item replaced an earlier
            # position and nothing has replaced it since.
            for r in rows:
                r["current"] = bool(r["replaces"]) and not r["superseded_by"]
            out.append({
                "topic": th["topic"],
                "outcome": th["outcome"] if full else None,  # the outcome knows the future
                "items": rows,
            })
    counts = db.execute(
        "SELECT COUNT(*) n, SUM(kind='decision') d, SUM(kind='action') a FROM items WHERE team=? AND idx<=?",
        (team, as_of),
    ).fetchone()
    return {"team": team, "threads": out, "roles": store.team_roles(db, team),
            "counts": {"items": counts["n"] or 0, "decisions": counts["d"] or 0, "actions": counts["a"] or 0}}


def search(db, q, team=None):
    items, mode = embed.search_items(db, q, team, k=12)
    passages, _ = embed.search_chunks(db, q, team, k=6)
    return {
        "mode": mode,
        "items": [item_json(db, r) for r in items],
        "passages": [
            {"meeting_id": r["meeting_id"], "turn_start": r["turn_start"], "turn_end": r["turn_end"], "text": r["text"]}
            for r in passages
        ],
    }


def context(db, meeting_id, turn=None, quote=None, pad=4):
    """Turns around a turn number or a quote, so any citation can be read in place."""
    exact, pad = None, max(1, min(int(pad), 60))
    if quote:
        found = quotes.find_in_meeting(db, meeting_id, quote, turn)
        if found:
            turn, exact = found[0]["turn"], found[1]
    if not turn:
        return {"meeting_id": meeting_id, "turns": [], "verified": False}
    last = db.execute("SELECT n_turns FROM meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
    return {
        "meeting_id": meeting_id, "focus": turn, "quote": exact, "pad": pad,
        "verified": bool(quote) and quotes.verify(db, meeting_id, quote),
        "roles": store.get_roles(db, meeting_id),
        "turns": [dict(t) for t in store.get_turns(db, meeting_id, max(1, turn - pad), turn + pad)],
        "last_turn": last[0] if last else turn,
    }


BRIEF_CITE = re.compile(r'\[(\S+), Speaker (\w): "(.*?)"\]')


def parse_brief(db, text):
    """The brief is stored as plain text (the submission format). For the page, split it back
    into sections, points and citations so each quote can be opened in the transcript."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    if not lines or not any(ln.startswith("- ") for ln in lines):
        return None
    out = {"title": lines[0], "intro": [], "sections": []}
    for ln in lines[1:]:
        if not ln:
            continue
        if ln.startswith("- "):
            if not out["sections"]:
                return None
            cites = [{"meeting_id": m, "speaker": s, "quote": q, "verified": quotes.verify(db, m, q)}
                     for m, s, q in BRIEF_CITE.findall(ln)]
            out["sections"][-1]["points"].append({"text": BRIEF_CITE.sub("", ln[2:]).strip(), "citations": cites})
        elif ln.isupper():
            out["sections"].append({"title": ln, "points": []})
        elif not out["sections"]:
            out["intro"].append(ln)
    return out


def submission(db):
    """answers.json joined with the questions, each citation re-checked."""
    qpath = config.DATA / "questions.json"  # a data set without a question file still has a working UI
    if not qpath.exists():
        return {"answers": [], "brief": ""}
    spec = json.loads(qpath.read_text(encoding="utf-8"))
    path = config.ROOT / "answers.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"answers": [], "brief": ""}
    by_id = {a["question_id"]: a for a in data["answers"]}
    jpath = config.RUNS / "adjudication.json"  # which pass the judge chose, and why
    judged = json.loads(jpath.read_text(encoding="utf-8")) if jpath.exists() else {}
    rows = []
    for q in spec["questions"]:
        a = by_id.get(q["question_id"], {"answer": "", "citations": []})
        rows.append({
            **q, "answer": a["answer"],
            "citations": [{**c, "verified": quotes.verify(db, c["meeting_id"], c["quote"])} for c in a["citations"]],
            "judge": judged.get(q["question_id"]),
        })
    brief = data.get("brief", "")
    return {"answers": rows, "brief": brief, "brief_parts": parse_brief(db, brief)}


def stats(db):
    one = lambda sql: db.execute(sql).fetchone()[0]
    return {
        "meetings": one("SELECT COUNT(*) FROM meetings"), "words": one("SELECT SUM(n_words) FROM meetings"),
        "items": one("SELECT COUNT(*) FROM items"), "threads": one("SELECT COUNT(*) FROM threads"),
        "supersessions": one("SELECT COUNT(*) FROM supersedes"),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in ("/", "/index.html"):
            body = (WEB / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        db = store.connect()  # one connection per request: sqlite connections are per thread
        try:
            as_of = store.meeting_index(db, q.get("as_of"))
            turn = int(q["turn"]) if q.get("turn") else None
            routes = {
                "/api/teams": lambda: teams(db),
                "/api/stats": lambda: stats(db),
                "/api/timeline": lambda: timeline(db, q["team"], as_of),
                "/api/search": lambda: search(db, q.get("q", ""), q.get("team") or store.find_team(q.get("q", ""), db)),
                "/api/context": lambda: context(db, q["meeting"], turn, q.get("quote"), int(q.get("pad") or 4)),
                "/api/submission": lambda: submission(db),
            }
            if url.path not in routes:
                return self._json({"error": "not found"}, 404)
            self._json(routes[url.path]())
        except (KeyError, ValueError) as e:
            self._json({"error": f"bad request: {e}"}, 400)
        finally:
            db.close()

    def _stream(self):
        """Start a response that is a stream of JSON lines; returns the function that sends one."""
        # No Content-Length: the stream ends when the socket closes.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(obj):
            self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
            self.wfile.flush()
        return emit

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/ask", "/api/check", "/api/add"):
            return self._json({"error": "not found"}, 404)
        try:
            size = int(self.headers.get("Content-Length", 0))
            if size > MAX_UPLOAD:
                return self._json({"error": "transcript too large (limit 5 MB)"}, 413)
            body = json.loads(self.rfile.read(size) or b"{}")
        except ValueError:
            return self._json({"error": "bad JSON"}, 400)
        if path == "/api/ask":
            return self._ask(body)
        return self._add(body, dry_run=(path == "/api/check"))

    def _add(self, body, dry_run):
        """Check a transcript, or add it and stream the progress of the model steps."""
        text = str(body.get("text") or "")
        name = str(body.get("filename") or "").rsplit(".", 1)[0] or None
        team = str(body.get("team") or "").strip() or None
        try:
            number = int(body["number"]) if str(body.get("number") or "").strip() else None
        except ValueError:
            return self._json({"error": "Meeting number must be a whole number."}, 400)
        db = store.connect()
        try:
            try:
                info = intake.preview(db, text, name, team, number)
            except store.TranscriptError as e:
                return self._json({"error": str(e)}, 400)
            if dry_run:
                return self._json(info)
            if info["exists"] and not body.get("replace"):
                return self._json({"error": f"{info['meeting_id']} is already in the store. Tick replace to reload it."}, 409)
            emit = self._stream()
            log = lambda line: emit({"type": "log", "text": str(line).strip()})
            try:
                added = intake.add(db, text, name, team, number, replace=bool(body.get("replace")))
                log(f"Stored {added['meeting_id']}: {len(added['turns'])} turns, {added['words']} words. Searchable now.")
                log("Reading the meeting into the ledger (model)...")
                intake.process(db, LLM(db), [added["meeting_id"]], log=log)
                n = db.execute("SELECT COUNT(*) FROM items WHERE meeting_id=?", (added["meeting_id"],)).fetchone()[0]
                emit({"type": "done", "meeting_id": added["meeting_id"], "team": added["team"], "items": n})
            except store.TranscriptError as e:
                emit({"type": "error", "error": str(e)})
            except LLMError as e:
                # The transcript is stored and searchable; only the ledger step is unfinished.
                emit({"type": "error", "error": "Stored, but the model step failed: " + str(e)[:400]
                      + " Run `python main.py build` later to finish it."})
            except (BrokenPipeError, ConnectionError):
                pass
        finally:
            db.close()

    def _ask(self, body):
        question = str(body.get("question") or "").strip()[:2000]
        if not question:
            return self._json({"error": "empty question"}, 400)
        emit = self._stream()
        db = store.connect()
        try:
            llm = LLM(db)
            agent = Agent(db, llm, on_step=lambda s: emit({
                "type": "step", "step": s["step"], "tool": s["tool"], "args": s["args"],
                "observation": s["observation"][:1500]}))
            emit({"type": "start", "team": body.get("team") or store.find_team(question, db), "model": llm.model})
            res = agent.run(question, body.get("team") or None)
            emit({"type": "answer", "answer": res["answer"], "citations": res["citations"],
                  "steps": res["steps"], "unverified": res["unverified"], "model": llm.model,
                  "reviewed": res.get("reviewed", False)})
        except LLMError as e:
            emit({"type": "error", "error": str(e)[:600]})
        except (BrokenPipeError, ConnectionError):
            pass  # the browser went away
        finally:
            db.close()


def serve(port=8000):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"meetmem UI: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
