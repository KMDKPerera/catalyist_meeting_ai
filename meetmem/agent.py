"""The question-answering agent: a tool loop over the memory store.

The model decides what to look up next and when it has enough. It never sees a
whole transcript, only what its tool calls return, so the same loop works on
138 meetings or 10,000. Code decides whether the final answer is acceptable:
every citation is checked against the transcript and rewritten with the
transcript's own words before anything is returned.

Tools are declared through the OpenAI-style function-calling interface, which
Gemini, Groq and Ollama all accept. (My first version asked for a JSON object
naming the tool in plain text. Gemini kept trying to turn that into a native
function call that had not been declared and returned empty replies with
finish_reason MALFORMED_FUNCTION_CALL, so I moved to the native interface.)
"""
from collections import Counter

from . import embed, quotes, reconcile, store
from .llm import LLM, reviewer

MAX_STEPS = 14
OBS_CHARS = 10000  # room for 8 passages: fused search needs 7 to 8 to beat keywords alone (eval/retrieval_check.py)


def _tool(name, description, props, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_STR, _INT = {"type": "string"}, {"type": "integer"}
TOOLS = [
    _tool("search_memory",
          "Search the ledger of items extracted from the meetings (decisions, proposals, objections, actions, "
          "facts): who said what, and where. Items marked SUPERSEDED were replaced later.",
          {"query": _STR, "team": _STR,
           "meeting": {"type": "string", "description": "optional meeting id such as TS3012b"},
           "kind": {"type": "string", "enum": ["decision", "proposal", "objection", "action", "fact", "requirement"]}},
          ["query", "team"]),
    _tool("timeline",
          "A team's decision threads in time order: how a subject changed from meeting to meeting and what "
          "replaced what. Without a topic it lists the thread names.",
          {"team": _STR, "topic": _STR}, ["team"]),
    _tool("search_transcript",
          "Search the raw transcripts by keyword and by meaning. Describe what was said or decided; "
          "if nothing fits, rephrase.",
          {"query": _STR, "team": _STR, "meeting": {"type": "string", "description": "optional meeting id"}},
          ["query", "team"]),
    _tool("read_turns", "Read up to 40 consecutive turns of one meeting.",
          {"meeting_id": _STR, "start": _INT, "end": _INT}, ["meeting_id", "start", "end"]),
    _tool("answer", "Give the final answer and finish.",
          {"answer": _STR,
           "citations": {"type": "array", "items": {
               "type": "object",
               "properties": {"meeting_id": _STR, "turn": _INT,
                              "quote": {"type": "string", "description": "6 to 30 consecutive words copied exactly from that turn"}},
               "required": ["meeting_id", "turn", "quote"]}}},
          ["answer", "citations"]),
]

SYSTEM = """You answer questions about a team's recorded meetings using tools over a memory store. You can only see the meetings through tools. Call one tool per turn, and call `answer` when you are done.

How to work:
- A team's meetings are given to you in time order (for most teams the ids end in a, b, c, d). A later statement can replace an earlier one, and people refer back to things without naming them. Before you call something final, check the timeline and the later meetings.
- The ledger is a set of leads, not evidence. Before answering, read the turns around what you plan to cite and make sure they say what you think.
- If a search comes back empty or off-topic, change the words and search again. Questions usually span at least two meetings of the team.
- Always name speakers by the letter in the transcript ("Speaker D"), never by a first name alone. When the confirmed roles list gives that speaker's role, add it: "Speaker D (marketing)". If a speaker is not in that list, give the letter only; do not work a role out yourself.
- For totals and final figures, read on to the end of the costing discussion: a number said part-way through is a running subtotal, not the result. A proposal is not a decision until someone agrees to it.
- The answer is one to three plain sentences covering every part of the question. If the store does not contain the evidence, say so; do not guess.
- Give one to three citations. Each quote is 6 to 30 consecutive words copied exactly from a single turn. When the answer spans meetings, cite different meetings."""

# A second reader with a fresh context. It sees only the question, the draft
# and the transcript around each citation, so it cannot inherit the agent's
# misreading. One extra call per question.
REVIEW = """You check a draft answer about a team's meetings before it is sent. You get the question, the draft, and the transcript around each quote the draft cites (lines are "[turn] Speaker: words").

Look for these mistakes:
- a figure that is a running subtotal or an early estimate when the excerpt goes on to give the final figure
- a proposal or a personal preference reported as the team's decision, or something the excerpt later overturns
- the wrong speaker letter for who said or proposed something
- a part of the question the draft does not answer, or a claim the excerpts do not support

Be strict about what the excerpts show, but do not ask for changes of wording or style. If the draft holds up, say so.
Return JSON: {"verdict": "ok" or "revise", "problems": ["one short sentence each, naming the turn that shows it"]}"""

ADJUDICATE = """Several drafts answer the same question about a team's meetings. You get each draft and the transcript around each quote it cites (lines are "[turn] Speaker: words"), and sometimes the team's record on the subject. Meeting ids that end in a, b, c, d are in that time order.

Pick the draft that answers the question more correctly and completely according to the transcripts:
- A claim the excerpts contradict counts heavily against a draft. So does a wrong meeting, speaker or number.
- A decision made in the meeting itself outranks a later recap or a passing remark about it.
- Every part of the question should be answered. Speakers should be named by letter; a role added to the letter counts in a draft's favour only if it matches the confirmed roles.
- If several are right, pick the one that is most specific and best supported, in one to three plain sentences.

Return JSON: {"pick": "the letter of one draft", "why": "one or two sentences naming the turn that decides it"}"""


class Agent:
    def __init__(self, db, llm, log=None, on_step=None, review_llm=None):
        self.db, self.llm, self.log = db, llm, log or (lambda *_: None)
        # The second reader defaults to the provider's stronger model: one call per question.
        self.review_llm = review_llm or (isinstance(llm, LLM) and reviewer(db)) or llm
        self.on_step = on_step  # called with each trace entry as it happens (the web UI streams these)

    # ------------------------------------------------------------ tools

    def _roles_line(self, meeting_id):
        roles = store.get_roles(self.db, meeting_id)
        return (f"roles in {meeting_id}: " + ", ".join(f"{k}={v}" for k, v in sorted(roles.items()))) if roles else ""

    def search_memory(self, query="", team=None, meeting=None, kind=None, **_):
        rows, _ = embed.search_items(self.db, query, team, meeting, kind)
        if not rows:
            return "No ledger items matched. Try other words, or search_transcript."
        return "\n".join(store.fmt_item(self.db, r) for r in rows)

    def timeline(self, team=None, topic=None, **_):
        return reconcile.timeline(self.db, team, topic)

    def search_transcript(self, query="", team=None, meeting=None, **_):
        rows, _ = embed.search_chunks(self.db, query, team, meeting, k=8)
        if not rows:
            return "No transcript passages matched. Try other words."
        return "\n\n".join(
            f"--- {r['meeting_id']} turns {r['turn_start']}-{r['turn_end']} ---\n{r['text']}" for r in rows
        )

    def read_turns(self, meeting_id="", start=1, end=None, **_):
        start = max(1, int(start))
        end = min(int(end or start + 39), start + 39)
        turns = store.get_turns(self.db, meeting_id, start, end)
        if not turns:
            return f"No turns {start}-{end} in {meeting_id!r}. Check the meeting id."
        head = f"--- {meeting_id} turns {start}-{end} --- {self._roles_line(meeting_id)}"
        return head + "\n" + "\n".join(f"[{t['turn']}] {t['t']} {t['speaker']}: {t['text']}" for t in turns)

    @staticmethod
    def _repeat(seen, tool, args):
        """True if this lookup was already made (search words compared as a set)."""
        a = {k: v for k, v in args.items() if v not in (None, "")}
        if "query" in a:
            a["query"] = " ".join(sorted(set(store.fts_query(str(a["query"])).replace('"', "").split(" OR "))))
        key = (tool, tuple(sorted((k, str(v)) for k, v in a.items())))
        if key in seen:
            return True
        seen.add(key)
        return False

    # ------------------------------------------------------------ checking

    def check_answer(self, args):
        """-> (answer, verified citations, problems, turn of each citation). All code, no model."""
        answer = str(args.get("answer") or "").strip()
        good, problems, turns = [], [], []
        if not answer:
            problems.append("The answer text is empty.")
        for c in (args.get("citations") or [])[:3]:
            if not isinstance(c, dict):
                continue
            mid = str(c.get("meeting_id") or "")
            try:
                hint = int(c.get("turn"))
            except (TypeError, ValueError):
                hint = None
            found = quotes.find_in_meeting(self.db, mid, c.get("quote"), hint)
            if found:
                good.append({"meeting_id": mid, "quote": found[1]})
                turns.append(found[0]["turn"])
            else:
                problems.append(
                    f"Citation not found in {mid}: \"{str(c.get('quote'))[:80]}\". Quotes must be at least "
                    f"{quotes.MIN_WORDS} consecutive words from one turn. Use read_turns and copy exactly."
                )
        if not good and not problems:
            problems.append("Give one to three citations.")
        return answer, good, problems, turns

    def review(self, question, answer, cites, turns, pad=12):
        """Second reader: problems found in the draft, or [] if it holds up."""
        parts = []
        for c, turn in zip(cites, turns):
            ctx = store.get_turns(self.db, c["meeting_id"], max(1, turn - pad), turn + pad)
            parts.append(
                f"--- {c['meeting_id']}, cited quote is in turn {turn}: \"{c['quote']}\" "
                f"{self._roles_line(c['meeting_id'])}\n" + "\n".join(store.fmt_turn(t) for t in ctx)
            )
        user = f"Question: {question}\n\nDraft answer: {answer}\n\n" + "\n\n".join(parts)
        try:
            out = self.review_llm.chat(
                [{"role": "system", "content": REVIEW}, {"role": "user", "content": user}],
                purpose="review", max_tokens=4096,
            )
        except Exception as e:  # the review is a bonus check; never lose an answer over it
            self.log(f"    review skipped: {str(e)[:100]}")
            return []
        if str(out.get("verdict", "")).lower() != "revise":
            return []
        return [str(p) for p in (out.get("problems") or [])][:4]

    def _excerpts(self, cites, pad):
        parts = []
        for c in cites:
            found = quotes.find_in_meeting(self.db, c["meeting_id"], c["quote"])
            if not found:
                continue
            turn = found[0]["turn"]
            ctx = store.get_turns(self.db, c["meeting_id"], max(1, turn - pad), turn + pad)
            parts.append(f"--- {c['meeting_id']}, cited turn {turn}: \"{c['quote']}\"\n"
                         + "\n".join(store.fmt_turn(t) for t in ctx))
        return "\n\n".join(parts) or "(no valid citation)"

    def adjudicate(self, question, drafts, team=None, pad=10):
        """Several independent runs of the agent, one judge. -> (index of the pick, reason).

        A small model's answers vary from run to run. The judge reads every draft
        with the transcript around its citations, plus the decision thread the
        question is about (fetched by code, so the judge is not limited to what
        the drafts happened to find), and picks one draft whole. It never writes
        a new answer, so the citations stay the ones code already verified.
        """
        letters = "ABCDEFGH"
        parts = [f"Question: {question}"]
        roles = store.team_roles(self.db, team) if team else {}
        if roles:
            parts.append("Roles confirmed for this team: " + ", ".join(f"Speaker {k} = {v}" for k, v in roles.items()))
        if team:
            leads, _ = embed.search_items(self.db, question, team, k=12)
            common = Counter(r["thread_id"] for r in leads if r["thread_id"]).most_common(1)
            if common and common[0][1] >= 2:
                parts.append("The team's record on this subject, in time order (a guide, not proof; "
                             "the transcript excerpts below are the evidence):\n"
                             + reconcile.thread_view(self.db, common[0][0]))
        for i, d in enumerate(drafts):
            parts.append(f"DRAFT {letters[i]}: {d['answer']}\nEvidence cited by {letters[i]}:\n"
                         f"{self._excerpts(d['citations'], pad)}")
        out = self.review_llm.chat(
            [{"role": "system", "content": ADJUDICATE}, {"role": "user", "content": "\n\n".join(parts)}],
            purpose="adjudicate", max_tokens=4096,
        )
        pick = str(out.get("pick", "A")).strip().upper()[:1]
        return (letters.index(pick) if pick in letters[:len(drafts)] else 0), str(out.get("why") or "")

    # ------------------------------------------------------------ loop

    def run(self, question, team=None):
        team = team or store.find_team(question, self.db)
        meetings = [r[0] for r in self.db.execute("SELECT meeting_id FROM meetings WHERE team=? ORDER BY idx", (team,))]
        user = f"Question: {question}"
        if meetings:
            user += f"\nTeam {team} has these meetings, in time order: {', '.join(meetings)}."
            roles = store.team_roles(self.db, team)
            if roles:
                user += ("\nRoles confirmed across this team's meetings: "
                         + ", ".join(f"Speaker {k} = {v}" for k, v in roles.items()) + ".")
            # Code does the first retrieval for free, so the agent always starts from the
            # ledger instead of keyword-searching the question's own wording.
            threads = [r[0] for r in self.db.execute("SELECT topic FROM threads WHERE team=? ORDER BY thread_id", (team,))]
            if threads:
                user += "\nDecision threads on record (use timeline with one of these as the topic): " + "; ".join(threads) + "."
            leads, _ = embed.search_items(self.db, question, team, k=12)
            if leads:
                user += "\n\nStarting leads from the ledger. These are leads, not evidence; read the turns before relying on them:\n"
                user += "\n".join(store.fmt_item(self.db, r) for r in leads[:8])
                # If several leads sit in one decision thread, show that thread's whole history:
                # this is where a changed or replaced position becomes visible.
                tid, n = Counter(r["thread_id"] for r in leads if r["thread_id"]).most_common(1)[0] if any(
                    r["thread_id"] for r in leads) else (None, 0)
                if n >= 2:
                    user += ("\n\nThe decision thread most of those leads belong to, in time order "
                             "(positions change; the early ones matter as much as the last):\n")
                    user += reconcile.thread_view(self.db, tid)
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
        trace, fixes, nudged, reviewed = [], 0, False, False
        tools = {
            "search_memory": self.search_memory, "timeline": self.timeline,
            "search_transcript": self.search_transcript, "read_turns": self.read_turns,
        }
        seen = set()
        best = None  # last answer seen, kept in case the loop runs out
        for step in range(1, MAX_STEPS + 1):
            # Small models ignore "two steps left" and keep searching, so on the last two steps
            # code takes the choice away: the only tool on offer is `answer`.
            force = "answer" if step >= MAX_STEPS - 1 else None
            msg, tool, args, call_id = self.llm.call_tool(messages, TOOLS, purpose="agent", force=force)
            tool = str(tool or "")
            if not isinstance(args, dict):
                args = {}
            self.log(f"    step {step}: {tool} {str(args)[:110]}")
            if tool == "answer":
                answer, cites, problems, cite_turns = self.check_answer(args)
                best = {"answer": answer, "citations": cites}
                one_meeting = len({c["meeting_id"] for c in cites}) == 1 and len(meetings) > 1
                feedback = None  # set when the draft is sent back; None means accept
                if problems and fixes < 2:
                    fixes += 1
                    feedback = "ANSWER REJECTED by the citation checker:\n- " + "\n- ".join(problems)
                elif one_meeting and not nudged and step < MAX_STEPS:
                    nudged = True
                    feedback = (
                        f"All your citations are from {cites[0]['meeting_id']}. This team met {len(meetings)} times "
                        "and the subject probably came up more than once. Check the other meetings for an earlier "
                        "or later position, then answer again (the same answer is fine if nothing changes)."
                    )
                elif cites and not reviewed and step < MAX_STEPS:
                    reviewed = True  # one review round only, so two models cannot argue forever
                    doubts = self.review(question, answer, cites, cite_turns)
                    self.log(f"    review: {'revise: ' + ' | '.join(doubts)[:160] if doubts else 'ok'}")
                    if doubts:
                        feedback = (
                            "REVIEW: a second reader checked your draft against the transcript and found:\n- "
                            + "\n- ".join(doubts)
                            + "\nRead those turns again (and further on, if a figure may be a subtotal), then answer "
                            "again. If you still think the draft is right, give it again."
                        )
                if feedback is None:
                    trace.append({"step": step, "tool": tool, "args": args, "observation": "accepted"})
                    return {**best, "steps": step, "trace": trace, "unverified": problems, "reviewed": reviewed}
                obs = feedback
            elif tool in tools and self._repeat(seen, tool, args):
                # Weaker models get stuck re-running one search with the words shuffled.
                obs = (
                    "You already ran this lookup; it returns the same thing. Change approach: search for the "
                    "thing itself rather than the question's wording (e.g. 'solar', 'kinetic', 'beep'), look at "
                    "the timeline for the topic, or read_turns around a ledger item you have already seen."
                )
            elif tool in tools:
                try:
                    obs = tools[tool](**args)
                except Exception as e:  # a bad tool call is feedback, not a crash
                    obs = f"Tool error: {e}"
            else:
                obs = f"Unknown tool {tool!r}. Use one of: {', '.join(tools)}, answer."
            obs = obs[:OBS_CHARS]
            left = MAX_STEPS - step
            if left <= 2:
                obs += f"\n\n({left} step(s) left: call answer now with what you have.)"
            trace.append({"step": step, "tool": tool, "args": args, "observation": obs})
            if self.on_step:
                self.on_step(trace[-1])
            if msg:  # native call: hand the model's own message back, then the result
                messages.append(msg)
                messages.append({"role": "tool", "tool_call_id": call_id, "name": tool, "content": obs})
            else:
                messages.append({"role": "assistant", "content": f"{tool} {args}"})
                messages.append({"role": "user", "content": "RESULT:\n" + obs})
        best = best or {"answer": "The agent ran out of steps before finding evidence.", "citations": []}
        return {**best, "steps": MAX_STEPS, "trace": trace, "unverified": ["ran out of steps"]}
