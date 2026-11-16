"""Pre-meeting brief, built in three steps.

1. Code selects: the team's ledger as it stood before the final meeting, with
   every item flagged current or superseded (time filtering is a query).
2. The model writes: which points matter, and how to say them briefly. It cites
   by ledger item id.
3. Code cites: each id is swapped for the meeting id and the stored, already
   verified quote. A point with no valid id is dropped, so nothing in the brief
   is uncited.
"""
from . import quotes, reconcile, store

SYSTEM = """You write the brief a project team reads before its next meeting.
You get the team's ledger so far, grouped into threads in time order. Items marked SUPERSEDED were replaced later; the newest unsuperseded item in a thread is the current position.

Write four sections:
- "decided": what currently stands. One point per decision that still holds.
- "changed": what changed along the way: the earlier position, what replaced it, and why if stated.
- "open_actions": things someone still has to do before or at the next meeting. Set "owner" to the speaker letter, with the role only if it is in the confirmed roles. Leave out actions that later items show were already done, and standing habits such as filing the minutes. Include anything the team said it had not been able to check yet (for example costs), as an action for whoever raised it.
- "open_disagreements": objections and questions raised but not settled.

Each point is one or two plain sentences that a busy person can act on, with real specifics (numbers, materials, names of features). Every point lists 1 or 2 "item_ids" from the ledger that back it; for a change, cite the old and the new item.

You also get the CLOSING TURNS of the most recent meeting, word for word. Next steps are usually agreed there, and so are things left unchecked (for example a cost that was never worked out). Use them for open_actions and open_disagreements. A point that rests on a closing turn cites it with "turn_quotes": [{"turn": 603, "quote": "6 to 30 consecutive words copied exactly from that turn"}], instead of or as well as item_ids.

Use only what is in the ledger and the closing turns. Aim for 4 to 8 points per section; a short honest section beats padding.

Return JSON: {"summary": "two sentences on where the project stands", "decided": [{"point": "...", "item_ids": [1]}], "changed": [...], "open_actions": [{"point": "...", "owner": "B (industrial designer)", "item_ids": [2], "turn_quotes": []}], "open_disagreements": [...]}"""

CLOSING_TURNS = 60

SECTIONS = [
    ("decided", "DECIDED SO FAR"),
    ("changed", "WHAT CHANGED ALONG THE WAY"),
    ("open_actions", "OPEN ACTIONS AND OWNERS"),
    ("open_disagreements", "DISAGREEMENTS STILL OPEN"),
]


def build(db, llm, team, log=print):
    meetings = db.execute("SELECT * FROM meetings WHERE team=? ORDER BY idx", (team,)).fetchall()
    final = meetings[-1]
    as_of = final["idx"] - 1  # "before their final meeting": the last one is unseen
    seen = [m["meeting_id"] for m in meetings if m["idx"] <= as_of]

    ledger = reconcile.timeline(db, team, as_of=as_of, max_threads=999)
    loose = db.execute(
        "SELECT * FROM items WHERE team=? AND idx<=? AND thread_id IS NULL ORDER BY idx, turn", (team, as_of)
    ).fetchall()
    roles = ", ".join(f"Speaker {k} = {v}" for k, v in store.team_roles(db, team).items()) or "none established"
    user = (
        f"Team {team}. Meetings held so far, in order: {', '.join(seen)}. Next meeting: {final['meeting_id']}.\n"
        f"Confirmed roles: {roles}. Give any other speaker by letter only.\n\nTHREADS\n{ledger}\n\nOTHER ITEMS\n"
        + "\n".join(store.fmt_item(db, it, as_of) for it in loose)
    )
    # Recency matters most for actions, and the ledger can miss a casual "do that for next
    # time", so the end of the latest meeting goes in verbatim. Bounded: 60 turns, any corpus size.
    latest = seen[-1]
    closing = [t for t in store.get_turns(db, latest) if len(t["text"].split()) >= 3][-CLOSING_TURNS:]
    user += f"\n\nCLOSING TURNS of {latest}\n" + "\n".join(store.fmt_turn(t) for t in closing)
    out = llm.chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], purpose="brief", max_tokens=8192
    )

    lines = [f"BRIEF FOR TEAM {team} BEFORE MEETING {final['meeting_id']}", f"Based on meetings {', '.join(seen)}.", ""]
    if out.get("summary"):
        lines += [str(out["summary"]).strip(), ""]
    dropped = 0
    for key, title in SECTIONS:
        lines.append(title)
        n = 0
        for p in out.get(key) or []:
            cites = []
            for i in reconcile._ints(p.get("item_ids") if isinstance(p, dict) else [])[:2]:
                it = db.execute(
                    "SELECT * FROM items WHERE item_id=? AND team=? AND idx<=? AND quote IS NOT NULL", (i, team, as_of)
                ).fetchone()
                if it:
                    cites.append(f'[{it["meeting_id"]}, Speaker {it["speaker"]}: "{it["quote"]}"]')
            for tq in (p.get("turn_quotes") or [] if isinstance(p, dict) else [])[:2]:
                if len(cites) >= 2 or not isinstance(tq, dict):
                    break
                try:
                    hint = int(tq.get("turn"))
                except (TypeError, ValueError):
                    hint = None
                found = quotes.find_in_meeting(db, latest, tq.get("quote"), hint)
                if found:  # same rule as everywhere: the words come from the store, not the model
                    cite = f'[{latest}, Speaker {found[0]["speaker"]}: "{found[1]}"]'
                    if not any(found[1] in c or c.split('"')[1] in found[1] for c in cites):  # no repeats
                        cites.append(cite)
            if not cites or not str(p.get("point") or "").strip():
                dropped += 1
                continue
            owner = f" Owner: {p['owner']}." if key == "open_actions" and p.get("owner") else ""
            lines.append(f"- {str(p['point']).strip()}{owner} {' '.join(cites)}")
            n += 1
        if not n:
            lines.append("- Nothing found in the record.")
        lines.append("")
    log(f"  brief: {dropped} uncited point(s) dropped by code")
    return "\n".join(lines).strip()
