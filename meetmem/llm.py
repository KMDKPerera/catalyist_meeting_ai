"""One small chat client for any OpenAI-compatible endpoint (Gemini, Groq, Ollama).

Standard library only. Every call is logged with token counts and latency so
the cost of ingesting N meetings is measured, not guessed.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

from . import config  # noqa: F401  (loads .env)

PROVIDERS = {
    "gemini": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        # flash-lite has a usable free daily quota; gemini-3.8-flash allows 20 requests a day
        "model": "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
        # The one review call per question goes to a stronger model: 20 free requests a day
        # each, so they are chained, ending on flash-lite when all are used up.
        "review_model": "gemini-3.6-flash,gemini-3.5-flash,gemini-3.8-flash,gemini-3.7-flash,"
                        "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
        "interval": 6.5,  # free tier is requests-per-minute bound
    },
    "groq": {
        "base": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
        "interval": 2.5,
    },
    "ollama": {"base": "http://localhost:11434/v1", "key_env": None, "model": "qwen2.5:7b", "interval": 0},
}


class LLMError(RuntimeError):
    pass


def pick_provider():
    name = os.environ.get("LLM_PROVIDER")
    if not name:
        name = next((n for n, p in PROVIDERS.items() if p["key_env"] and os.environ.get(p["key_env"])), None)
    if name not in PROVIDERS:
        raise LLMError(
            "No model configured. Put GEMINI_API_KEY=... or GROQ_API_KEY=... in .env "
            "(or set LLM_PROVIDER=ollama for a local model)."
        )
    return name


def reviewer(db=None):
    """The model used for the second-reader check, or None to reuse the main one."""
    model = os.environ.get("REVIEW_MODEL") or PROVIDERS[pick_provider()].get("review_model")
    return LLM(db, model=model) if model else None


def parse_json(text):
    """Models wrap JSON in fences or prose now and then; dig the object out."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a : b + 1])
        raise


class LLM:
    def __init__(self, db=None, model=None):
        self.provider = pick_provider()
        p = PROVIDERS[self.provider]
        self.base = os.environ.get("LLM_BASE_URL", p["base"])
        self.key = os.environ.get(p["key_env"]) if p["key_env"] else "ollama"
        # LLM_MODEL may be a comma-separated list: when one model's daily free
        # quota runs out, the client moves on to the next.
        # The list is an order of preference. A model is skipped for the rest of the run only
        # when its daily quota is gone; after a passing outage (503, dropped connection) the
        # next call goes back to the preferred model. (An earlier version stayed on whatever
        # model it had fallen back to, and one dropped connection put a whole eval run on the
        # weaker model.)
        self.preference = [m.strip() for m in (model or os.environ.get("LLM_MODEL") or p["model"]).split(",") if m.strip()]
        self.exhausted = set()
        self.model = self.preference[0]
        self.interval = float(os.environ.get("LLM_MIN_INTERVAL", p["interval"]))
        self.db = db
        self._last = 0.0

    def _request(self, path, body=None):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body else None,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "User-Agent": "meetmem/0.1",
            },
        )
        with urllib.request.urlopen(req, timeout=float(os.environ.get("LLM_TIMEOUT", "180"))) as r:
            return json.loads(r.read().decode("utf-8"))

    def models(self):
        return sorted(m["id"] for m in self._request("/models").get("data", []))

    def chat(self, messages, purpose, max_tokens=4096):
        """messages -> parsed JSON object. Retries rate limits and bad JSON."""
        def parse(choice):
            out = parse_json((choice["message"].get("content") or "").strip())
            if not isinstance(out, dict):
                raise ValueError("JSON was not an object")
            return out

        body = {"messages": messages, "max_tokens": max_tokens, "response_format": {"type": "json_object"}}
        return self._complete(body, purpose, parse)

    def call_tool(self, messages, tools, purpose, max_tokens=2048, force=None):
        """One agent step with native function calling.

        -> (assistant message to append to the history, tool name, args, call id).
        `force` names the one tool the model must call (used to end a loop that will not stop).
        """
        def parse(choice):
            msg = choice["message"]
            calls = msg.get("tool_calls") or []
            if calls:
                fn = calls[0]["function"]
                args = fn.get("arguments") or {}
                args = json.loads(args) if isinstance(args, str) else args
                # Keep only the first call so each step is one lookup.
                return {**msg, "tool_calls": calls[:1]}, fn["name"], args, calls[0].get("id") or "call_0"
            # Some models write the call as JSON text instead; accept that too.
            out = parse_json((msg.get("content") or "").strip())
            name = out.pop("tool", None) or out.pop("name", "")
            out.pop("thought", None)
            return None, name, out.get("args") or out.get("arguments") or out, None

        if force:  # offering only that tool works on every provider, unlike a named tool_choice
            tools = [t for t in tools if t["function"]["name"] == force]
        body = {"messages": messages, "max_tokens": max_tokens, "tools": tools, "tool_choice": "required"}
        return self._complete(body, purpose, parse)

    @property
    def fallbacks(self):
        """Models still worth trying after the current one, in order of preference."""
        i = self.preference.index(self.model) if self.model in self.preference else -1
        rest = self.preference[i + 1:] + self.preference[:i]
        return [m for m in rest if m not in self.exhausted]

    def _next_model(self, why, give_up_on_current=False):
        if give_up_on_current:
            self.exhausted.add(self.model)
        options = self.fallbacks
        if not options:
            return False
        print(f"    [{self.provider}] {why} on {self.model}, trying {options[0]}")
        self.model = options[0]
        return True

    def _complete(self, body, purpose, parse):
        live = [m for m in self.preference if m not in self.exhausted]
        self.model = live[0] if live else self.preference[0]
        body = {"model": self.model, "temperature": 0, **body}
        if os.environ.get("LLM_REASONING_EFFORT"):
            body["reasoning_effort"] = os.environ["LLM_REASONING_EFFORT"]
        last_err = None
        for attempt in range(7):
            wait = self.interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            t0 = self._last = time.time()
            try:
                res = self._request("/chat/completions", body)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                last_err = f"HTTP {e.code}: {detail}"
                if e.code == 429 and "per day" in detail and self._next_model("daily quota used up", True):
                    body["model"] = self.model
                    continue
                if e.code >= 500 and attempt >= 1 and self._next_model(f"{e.code} twice"):
                    body["model"] = self.model
                    time.sleep(5)
                    continue
                if e.code in (429, 500, 502, 503, 504):
                    retry_after = e.headers.get("Retry-After", "")
                    delay = float(retry_after) if retry_after.replace(".", "").isdigit() else min(90, 8 * 2**attempt)
                    print(f"    [{self.provider}] {e.code}, waiting {delay:.0f}s")
                    time.sleep(delay)
                    continue
                raise LLMError(last_err)
            except (urllib.error.URLError, OSError) as e:  # dropped connection, timeout
                last_err = str(e)
                # One dropped connection is usually just that: retry the same model. Only a
                # second failure in a row moves to the next one (a model that hangs, as some
                # of the stronger ones did, is as useless as one out of quota).
                if attempt >= 1 and self._next_model("no reply twice"):
                    body["model"] = self.model
                time.sleep(5)
                continue
            usage = res.get("usage") or {}
            if self.db is not None:
                with self.db:
                    self.db.execute(
                        "INSERT INTO llm_calls VALUES(?,?,?,?,?,?)",
                        (t0, purpose, self.model, usage.get("prompt_tokens", 0),
                         usage.get("completion_tokens", 0), time.time() - t0),
                    )
            choice = res["choices"][0]
            try:
                return parse(choice)
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                last_err = (f"unusable reply ({e}); finish_reason={choice.get('finish_reason')}; "
                            f"reply started: {str(choice.get('message', {}).get('content'))[:200]!r}")
                time.sleep(3 * (attempt + 1))
        raise LLMError(f"{purpose}: gave up after retries: {last_err}")
