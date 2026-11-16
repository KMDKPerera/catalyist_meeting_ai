"""Checks that need no model: the store, the citation checker, the eval gold
set, and the agent loop driven by a scripted fake model.

Run: python tests/test_offline.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from meetmem import config, evaluate, extract, intake, quotes, reconcile, store  # noqa: E402
from meetmem.agent import Agent  # noqa: E402


class FakeLLM:
    """Replays canned replies so the loop can be tested without an API."""
    model, provider = "fake", "fake"

    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages, purpose, max_tokens=0):
        return self.replies.pop(0)

    def call_tool(self, messages, tools, purpose, max_tokens=0, force=None):
        r = self.replies.pop(0)
        return None, r["tool"], r["args"], None


def main():
    db = store.connect(":memory:")
    assert store.ingest(db) == 138
    assert store.ingest(db) == 0, "ingest must be incremental"

    # acronyms spelled L_C_D_ in the corpus must be findable as "LCD"
    assert store.search_chunks(db, "LCD", team="IS1004"), "acronym search"
    assert store.find_team("What did Team TS3012 decide?") == "TS3012"

    # citation checker: exact, repaired, rejected
    turn = store.get_turns(db, "TS3012c", 57, 57)[0]["text"]
    assert quotes.locate("watches can go on kinetic energy because they move all the time", turn)
    fixed = quotes.locate("Watches can go on kinetic energy, since they move all the time", turn)
    assert fixed and quotes.verify(db, "TS3012c", fixed), "paraphrase must be repaired to real words"
    assert quotes.locate("the team agreed that kinetic energy was a bad idea", turn) is None
    short = quotes.locate("remotes lie still", turn)
    assert len(short.split()) >= quotes.MIN_WORDS, "short quotes are widened to six words"

    # my eval set: every gold quote must be word for word in the transcript
    gold = json.loads((ROOT / "eval" / "my_questions.json").read_text(encoding="utf-8"))
    bad = evaluate.check_gold(db, gold)
    assert not bad, f"gold evidence not exact: {bad}"

    # extraction validation: speaker comes from the store, bad items are dropped
    m = db.execute("SELECT * FROM meetings WHERE meeting_id='TS3012c'").fetchone()
    with db:
        kept, dropped = extract.store_items(db, m, [
            {"turn": 76, "kind": "decision", "topic": "energy source", "statement": "The team takes regular batteries.",
             "quote": "will just take regular batteries"},
            {"turn": 48, "kind": "proposal", "topic": "energy source", "statement": "C favours kinetic.",
             "quote": "My personal favourite is the kinetic provision"},
            {"turn": 99999, "kind": "decision", "topic": "x", "statement": "bad turn", "quote": "nope"},
            {"turn": 60, "kind": "gossip", "topic": "x", "statement": "bad kind", "quote": "nope"},
        ])
    assert (kept, dropped) == (2, 2)
    items = db.execute("SELECT * FROM items ORDER BY turn").fetchall()
    assert [i["speaker"] for i in items] == ["C", "A"], "speaker is looked up, not model-supplied"
    assert all(quotes.verify(db, "TS3012c", i["quote"]) for i in items)

    # reconcile: code refuses a supersession that runs backwards in time
    kinetic, battery = items[0]["item_id"], items[1]["item_id"]
    reconcile.run(db, FakeLLM([{"threads": [{
        "topic": "energy source", "item_ids": [kinetic, battery],
        "supersedes": [[kinetic, battery], [battery, kinetic]], "outcome": "Regular batteries."}]}]), log=lambda *_: None)
    assert db.execute("SELECT * FROM supersedes").fetchall()[0][:] == (kinetic, battery)
    assert "SUPERSEDED" in reconcile.timeline(db, "TS3012", "energy")

    # agent loop: search, read, a bad citation gets rejected, then a good one passes
    agent = Agent(db, FakeLLM([
        {"tool": "timeline", "args": {"team": "TS3012", "topic": "energy source"}},
        {"tool": "read_turns", "args": {"meeting_id": "TS3012c", "start": 56, "end": 78}},
        {"tool": "answer", "args": {"answer": "They chose regular batteries.", "citations": [
            {"meeting_id": "TS3012c", "turn": 76, "quote": "the team decided on standard batteries in the end"}]}},
        {"tool": "answer", "args": {"answer": "They chose regular batteries.", "citations": [
            {"meeting_id": "TS3012c", "turn": 75, "quote": "Okay, will just take regular batteries"}]}},
        {"tool": "answer", "args": {"answer": "They chose regular batteries.", "citations": [
            {"meeting_id": "TS3012c", "turn": 76, "quote": "Okay, will just take regular batteries"},
            {"meeting_id": "TS3012b", "turn": 1, "quote": "look if it's possible to make a rechargeable remote"}]}},
        # the second reader objects once; the agent re-reads and answers again
        {"verdict": "revise", "problems": ["Turn 66 says the rechargeable unit is ruled out; mention it."]},
        {"tool": "read_turns", "args": {"meeting_id": "TS3012c", "start": 60, "end": 70}},
        {"tool": "answer", "args": {"answer": "They ruled out the rechargeable unit and chose regular batteries.", "citations": [
            {"meeting_id": "TS3012c", "turn": 76, "quote": "Okay, will just take regular batteries"},
            {"meeting_id": "TS3012b", "turn": 1, "quote": "look if it's possible to make a rechargeable remote"}]}},
    ]))
    res = agent.run("What energy source did Team TS3012 choose?")
    assert res["steps"] == 7 and len(res["citations"]) == 2, res
    assert "REJECTED" in res["trace"][2]["observation"]
    assert "All your citations are from" in res["trace"][3]["observation"]
    assert res["trace"][4]["observation"].startswith("REVIEW") and "ruled out" in res["answer"]
    assert all(quotes.verify(db, c["meeting_id"], c["quote"]) for c in res["citations"])

    test_adding_meetings(db)
    print("all offline checks passed")


NEW_MEETING = """00:00:01 Speaker A
Right, last week we said the launch would be in March, does that still hold for everyone?

00:00:09 Speaker E
No, I think we have to move the launch to May because the supplier slipped by six weeks.

00:00:15 Speaker A
Okay, May it is then, and Erin will tell the client by Friday.
"""


def test_adding_meetings(db):
    """A transcript added later: a new team, a fifth speaker, no AMI-style id."""
    with tempfile.TemporaryDirectory() as tmp:
        config.DATA = Path(tmp)  # so the test never writes into the real data folder
        before = db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]

        # a file that says nothing about itself needs the team and number spelling out
        for bad in ("just some notes with no turns", NEW_MEETING):
            try:
                intake.add(db, bad, "notes")
                raise AssertionError("should have been refused")
            except store.TranscriptError:
                pass
        assert db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == before, "a refused file changes nothing"

        p = intake.preview(db, NEW_MEETING, "notes", team="ACME7", number=2)
        assert p["new_team"] and p["speakers"] == ["A", "E"] and not p["exists"]
        info = intake.add(db, NEW_MEETING, "notes", team="ACME7", number=2)
        assert info["team"] == "ACME7" and info["idx"] == 2
        assert "Team: ACME7" in (Path(tmp) / "transcripts" / "notes.txt").read_text(encoding="utf-8")
        assert store.search_chunks(db, "supplier slipped", team="ACME7"), "searchable straight away"
        assert store.find_team("What did ACME7 decide about the launch?", db) == "ACME7"
        assert store.find_team("What did Team TS3012 decide?", db) == "TS3012"

        # same slot in the team's order is refused; the id form "ACME7a" needs no arguments
        try:
            intake.add(db, NEW_MEETING, "other", team="ACME7", number=2)
            raise AssertionError("two meetings cannot share a place in the order")
        except store.TranscriptError:
            pass
        first = intake.add(db, NEW_MEETING.replace("March", "February"), "ACME7a")
        assert (first["team"], first["idx"]) == ("ACME7", 1)
        assert intake.preview(db, NEW_MEETING, "ACME7a")["exists"]

        # model steps touch only the new meeting and its team
        items_before = db.execute("SELECT COUNT(*) FROM items WHERE team != 'ACME7'").fetchone()[0]
        teams = intake.process(db, FakeLLM([
            {"roles": {}, "items": [
                {"turn": 2, "kind": "decision", "topic": "launch date", "statement": "Speaker E moves the launch to May.",
                 "quote": "we have to move the launch to May because the supplier slipped", "owner": None},
                {"turn": 3, "kind": "action", "topic": "client update", "statement": "Erin tells the client by Friday.",
                 "quote": "Erin will tell the client by Friday", "owner": "E"}]},
            {"threads": []},
        ]), ["notes"], log=lambda *_: None)
        assert teams == ["ACME7"]
        got = db.execute("SELECT speaker, owner FROM items WHERE meeting_id='notes' ORDER BY turn").fetchall()
        assert [tuple(r) for r in got] == [("E", None), ("A", "E")], "a fifth speaker letter is accepted"
        assert db.execute("SELECT COUNT(*) FROM items WHERE team != 'ACME7'").fetchone()[0] == items_before

        # a corrected file replaces the old one, ledger items included
        intake.add(db, NEW_MEETING.replace("May", "June"), "notes", team="ACME7", number=2, replace=True)
        assert not db.execute("SELECT 1 FROM items WHERE meeting_id='notes'").fetchone()
        assert store.search_chunks(db, "June", meeting="notes") and not store.search_chunks(db, "May", meeting="notes")
        assert reconcile.timeline(db, "ACME7", as_of=store.meeting_index(db, "ACME7a")) is not None


if __name__ == "__main__":
    main()
