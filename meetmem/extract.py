"""Model step 1: read each meeting once and write ledger items into memory.

The model does the part that needs language understanding: spotting that
"yeah okay let's just go with the rubber then" is a decision about the case
material. Code does the rest: the speaker and time are looked up from the turn
the model points at (never taken from the model), and the quote is checked
against the transcript before the item is stored.
"""
from . import config, embed, quotes, store

KINDS = {"decision", "proposal", "objection", "action", "fact", "requirement"}

SYSTEM = """You build a searchable memory of a project team's meetings.
You are given part of one meeting transcript. Each line is "[turn] Speaker: words".
It is real speech: unfinished sentences, people talking over each other.

Extract every item a teammate might need to look up months later:
- decision: the group settles something. If it is provisional, say so in the statement.
- proposal: someone suggests an option, feature, material, price, target group...
- objection: someone pushes back or raises a concern. Say what it is against.
- action: someone commits to do something or is asked to. Set "owner" to the letter of who must do it.
- fact: a reported number or finding (cost, price, budget, total, survey result), or the reported answer to an earlier question.
- requirement: a constraint handed down by management or the client.

Each item has:
- "turn": the bracket number of the turn where it is said most clearly
- "kind": one of the six kinds above
- "topic": a 2-4 word noun phrase for the subject, e.g. "energy source", "case material", "lost-remote finder", "selling price". Use the same wording every time the same subject comes up.
- "statement": one self-contained sentence. Name people only by speaker letter ("Speaker B"), never by first name; spell out numbers and the concrete options, and replace "it"/"that" with what is meant.
- "quote": 6 to 30 consecutive words copied exactly from that turn
- "owner": only for actions

Also return "roles": map a speaker letter to a role (project manager, industrial designer, user interface designer, marketing) only where this text makes it clear.

Skip small talk, tool training, and agenda reading with no content. At most 40 items; prefer decisions, changes of mind, objections, numbers and commitments.
Return JSON: {"roles": {"A": "project manager"}, "items": [{"turn": 12, "kind": "decision", "topic": "...", "statement": "...", "quote": "...", "owner": null}]}"""


def segments(turns, budget=None):
    """Split a meeting into model-sized parts. Backchannel turns ("Yeah.",
    "Mm-hmm.") stay in the store but are not sent to the model."""
    budget = budget or config.EXTRACT_WORDS
    seg, n = [], 0
    for t in turns:
        w = len(t["text"].split())
        if w < 3:
            continue
        seg.append(t)
        n += w
        if n >= budget:
            yield seg
            seg, n = [], 0
    if seg:
        yield seg


def store_items(db, meeting, raw_items):
    """Validate the model's items against the transcript and save them."""
    turns = {t["turn"]: t for t in store.get_turns(db, meeting["meeting_id"])}
    speakers = {t["speaker"] for t in turns.values()}  # an owner must be someone in the meeting
    kept = dropped = 0
    for it in raw_items:
        try:
            turn_no = int(it.get("turn"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        kind = str(it.get("kind", "")).lower()
        statement = str(it.get("statement") or "").strip()
        if turn_no not in turns or kind not in KINDS or not statement:
            dropped += 1
            continue
        turn, quote = turns[turn_no], None
        found = quotes.find_in_meeting(db, meeting["meeting_id"], it.get("quote"), turn_no)
        if found and abs(found[0]["turn"] - turn_no) <= 5:
            turn, quote = found  # the turn the words are really in wins
        owner = str(it.get("owner") or "").strip().upper()[-1:] or None
        cur = db.execute(
            "INSERT INTO items(meeting_id, team, idx, turn, t, speaker, kind, topic, statement, owner, quote) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (meeting["meeting_id"], meeting["team"], meeting["idx"], turn["turn"], turn["t"],
             turn["speaker"], kind, str(it.get("topic") or "").strip().lower(), statement,
             owner if owner in speakers else None, quote),
        )
        db.execute(
            "INSERT INTO items_fts VALUES(?,?,?)",
            (f"{it.get('topic', '')}. {statement} {quote or ''}", cur.lastrowid, meeting["team"]),
        )
        kept += 1
    return kept, dropped


def forget(db, meeting_id):
    """Remove a meeting's ledger items so it can be extracted again."""
    with db:
        embed.ensure(db)
        db.execute("DELETE FROM vectors WHERE kind='item' AND meeting_id=?", (meeting_id,))
        ids = "SELECT item_id FROM items WHERE meeting_id=?"
        db.execute(f"DELETE FROM supersedes WHERE old_id IN ({ids}) OR new_id IN ({ids})", (meeting_id, meeting_id))
        db.execute(f"DELETE FROM items_fts WHERE item_id IN ({ids})", (meeting_id,))
        for table in ("items", "extracted", "roles"):
            db.execute(f"DELETE FROM {table} WHERE meeting_id=?", (meeting_id,))


def run(db, llm, team=None, limit=None, log=print, redo=False, only=None):
    sql, args = "SELECT * FROM meetings", []
    if team:  # a prefix works too ("ES"), so workers can split the corpus between them
        sql, args = sql + " WHERE team LIKE ?", [team + "%"]
    meetings = db.execute(sql + " ORDER BY team, idx", args).fetchall()
    if redo and team:
        for m in meetings:
            forget(db, m["meeting_id"])
    done_meetings = 0
    for m in meetings:
        if only is not None and m["meeting_id"] not in only:
            continue
        segs = list(segments(store.get_turns(db, m["meeting_id"])))
        todo = [
            i for i in range(len(segs))
            if not db.execute("SELECT 1 FROM extracted WHERE meeting_id=? AND seg=?", (m["meeting_id"], i)).fetchone()
        ]
        if not todo:
            continue
        if limit is not None and done_meetings >= limit:
            break
        for i in todo:
            text = "\n".join(store.fmt_turn(t) for t in segs[i])
            user = (
                f"Meeting {m['meeting_id']} (meeting {m['idx']} of team {m['team']}), "
                f"part {i + 1} of {len(segs)}.\n\n{text}"
            )
            out = llm.chat(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                purpose="extract", max_tokens=8192,
            )
            with db:  # one segment = one transaction, so a crash loses nothing
                kept, dropped = store_items(db, m, out.get("items") or [])
                for sp, role in (out.get("roles") or {}).items():
                    sp = str(sp).strip().upper()[-1:]
                    if sp in {t["speaker"] for t in segs[i]} and role:
                        db.execute("INSERT OR IGNORE INTO roles VALUES(?,?,?)", (m["meeting_id"], sp, str(role).lower()))
                db.execute("INSERT INTO extracted VALUES(?,?)", (m["meeting_id"], i))
            log(f"  {m['meeting_id']} part {i + 1}/{len(segs)}: {kept} items kept, {dropped} dropped")
        done_meetings += 1
    return done_meetings
