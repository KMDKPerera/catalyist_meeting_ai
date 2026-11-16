"""Model step 2: link a team's ledger into decision threads, so the store knows
what is current.

Split of work:
- The model judges meaning: do "the beeper", "that locator thing" and "it"
  refer to the same subject, and does a later item replace an earlier one?
- Code owns time. A supersession is only stored if the new item really is
  later than the old one, and "current as of meeting c" is a query over those
  links, not something the model is asked.

The model reads the compact ledger for one team (a few thousand tokens), never
the transcripts, so this stays cheap as the corpus grows.
"""
from . import store

SYSTEM = """You maintain the decision record for one project team.
You get the team's ledger in time order: one line per item with id, meeting (m1, m2, m3... in time order), turn, speaker, kind, topic and statement.

1. Group items into threads: one thread per subject the team keeps coming back to (for example energy source, case material, a lost-remote finder, speech recognition, the screen, price and cost). Items that mean the same subject under different words belong together. Leave one-off items out.
2. Inside each thread, list supersessions: pairs [old_id, new_id] where the later item replaces, reverses or drops what the earlier item proposed or decided. Only pair items that truly conflict; a later item that repeats or refines an earlier one is not a supersession.
3. Give each thread an "outcome": one sentence on where the subject stands at the end and how it got there.

Return JSON: {"threads": [{"topic": "energy source", "item_ids": [3, 17, 42], "supersedes": [[3, 42]], "outcome": "..."}]}
An item id may appear in only one thread."""


def ledger_lines(db, team):
    rows = db.execute("SELECT * FROM items WHERE team=? ORDER BY idx, turn, item_id", (team,)).fetchall()
    lines = [
        f"#{r['item_id']} m{r['idx']} t{r['turn']} {r['speaker']} {r['kind']} | {r['topic']}: {r['statement'][:260]}"
        for r in rows
    ]
    return rows, lines


def run(db, llm, team=None, log=print):
    teams = [team] if team else [r[0] for r in db.execute("SELECT DISTINCT team FROM items ORDER BY team")]
    for tm in teams:
        rows, lines = ledger_lines(db, tm)
        seen = db.execute("SELECT n_items FROM reconciled WHERE team=?", (tm,)).fetchone()
        if not rows or (seen and seen[0] == len(rows)):
            continue  # nothing new for this team since last time
        out = llm.chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": f"Team {tm} ledger:\n" + "\n".join(lines)}],
            purpose="reconcile", max_tokens=16384,  # thinking models spend part of this budget on reasoning
        )
        when = {r["item_id"]: (r["idx"], r["turn"], r["item_id"]) for r in rows}
        links = bad = 0
        with db:
            db.execute("DELETE FROM supersedes WHERE old_id IN (SELECT item_id FROM items WHERE team=?)", (tm,))
            db.execute("DELETE FROM threads WHERE team=?", (tm,))
            db.execute("UPDATE items SET thread_id=NULL WHERE team=?", (tm,))
            used = set()
            for th in out.get("threads") or []:
                ids = [i for i in _ints(th.get("item_ids")) if i in when and i not in used]
                if len(ids) < 2:
                    continue
                used.update(ids)
                cur = db.execute(
                    "INSERT INTO threads(team, topic, outcome) VALUES(?,?,?)",
                    (tm, str(th.get("topic") or "").strip().lower(), str(th.get("outcome") or "").strip()),
                )
                db.executemany("UPDATE items SET thread_id=? WHERE item_id=?", [(cur.lastrowid, i) for i in ids])
                for pair in th.get("supersedes") or []:
                    p = _ints(pair)
                    # Time is code's job: an item can only be replaced by a later one.
                    if len(p) == 2 and p[0] in ids and p[1] in ids and when[p[0]] < when[p[1]]:
                        db.execute("INSERT OR IGNORE INTO supersedes VALUES(?,?)", (p[0], p[1]))
                        links += 1
                    else:
                        bad += 1
            db.execute("INSERT OR REPLACE INTO reconciled VALUES(?,?)", (tm, len(rows)))
        n_threads = db.execute("SELECT COUNT(*) FROM threads WHERE team=?", (tm,)).fetchone()[0]
        log(f"  {tm}: {len(rows)} items -> {n_threads} threads, {links} supersessions ({bad} rejected by code)")


def _ints(xs):
    out = []
    for x in xs or []:
        try:
            out.append(int(str(x).lstrip("#")))
        except ValueError:
            pass
    return out


def thread_view(db, thread_id, limit=30):
    """One thread as text: its outcome line, then its items in time order."""
    th = db.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
    items = db.execute("SELECT * FROM items WHERE thread_id=? ORDER BY idx, turn LIMIT ?", (thread_id, limit)).fetchall()
    head = f"== {th['topic']} ==" + (f"\n   outcome: {th['outcome']}" if th["outcome"] else "")
    return "\n".join([head] + ["   " + store.fmt_item(db, it) for it in items])


def timeline(db, team, topic=None, as_of=None, max_threads=4):
    """Text view of a team's decision threads, oldest entry first.

    `as_of` (meeting index) hides everything after that meeting, which is how
    the brief sees the team as it stood before its final meeting.
    """
    full, as_of = as_of is None, as_of or 10**9
    threads = db.execute("SELECT * FROM threads WHERE team=? ORDER BY thread_id", (team,)).fetchall()
    if not threads:
        return f"No threads for {team}. Run extract and reconcile first."
    items = {
        th["thread_id"]: db.execute(
            "SELECT * FROM items WHERE thread_id=? AND idx<=? ORDER BY idx, turn", (th["thread_id"], as_of)
        ).fetchall()
        for th in threads
    }
    threads = [th for th in threads if items[th["thread_id"]]]
    if topic:
        words = [w for w in store.fts_query(topic).replace('"', "").split(" OR ") if w]

        def score(th):
            text = (th["topic"] + " " + " ".join(i["statement"] + " " + i["topic"] for i in items[th["thread_id"]])).lower()
            return sum(3 if w in th["topic"] else 1 for w in words if w[:5] in text)

        ranked = sorted(threads, key=score, reverse=True)
        threads = [th for th in ranked if score(th) > 0][:max_threads]
        if not threads:
            return f"No thread matches '{topic}'. Threads for {team}: " + "; ".join(th["topic"] for th in ranked)
    elif len(threads) > max_threads:
        # No topic given: list what exists rather than dump everything.
        return f"Threads for {team} (ask again with a topic):\n" + "\n".join(
            f"- {th['topic']} ({len(items[th['thread_id']])} items)" for th in threads
        )
    out = []
    for th in threads:
        head = f"== {th['topic']} =="
        if full and th["outcome"]:
            head += f"\n   outcome: {th['outcome']}"
        out.append(head)
        out += ["   " + store.fmt_item(db, it, as_of) for it in items[th["thread_id"]]]
    return "\n".join(out)
