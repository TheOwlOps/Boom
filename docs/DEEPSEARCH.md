# DeepSearch + DeepThink — In-Depth Research in Boom

## 1. What are DeepSearch and DeepThink?

**DeepSearch** is "proactive search" mode: instead of calling `web_search`
once and stopping, the agent loops — reading results, extracting entities
(person names, technologies, numbers...), generating follow-up questions from
what's still missing, and searching again. Finally the sources are **cross-
referenced** to mark consensus and conflicting content.

**DeepThink** is the "sit down and think" step after sources are in hand: it takes
the DeepSearch output and produces a **summary**, **perspectives**
(multiple angles: supporting / critical / neutral), and **gaps** (what's
still unclear, what needs more research).

### Quick comparison with Grok (xAI's DeepSearch)

| Criterion           | Grok DeepSearch          | Boom DeepSearch (this version)    |
|---------------------|--------------------------|-----------------------------------|
| Search sources      | Web + X (real-time social) | DuckDuckGo HTML (Boom's web.py) |
| Entity extraction   | LLM (with context)       | Regex heuristics (simple, cheap)  |
| Cross-reference     | Embedding + LLM          | Word overlap                      |
| Summarization       | LLM-generated prose      | Quote stitching + heuristics      |
| Dependencies        | xAI API key              | None — stdlib + existing web.py   |

In short: Grok is strong because it uses an LLM at every step; the Boom version runs **offline logic**
(no extra API key, fast, cheap) at the cost of shallower reasoning.
Once Boom has a provider key, each step can be swapped for an LLM without changing the
interface (see section 5).

## 2. Architecture

```
                        +----------------+
                        |  user / agent  |
                        +-------+--------+
                                | query
                                v
                     +----------------------+
                     |      DeepSearch      |
                     |  (multi-hop loop)    |
                     +----------+-----------+
                                |
        +-----------+-----------+-----------+------------+
        v           v           v           v            v
   web_search -> web_fetch -> extract  -> generate      -> cross_reference
   (DuckDuckGo)  (details)   entities    follow-ups      (source consensus/
                                                         conflicts)
                                |
                                v
                     +----------------------+
                     |      DeepThink       |
                     |     .analyze()       |
                     +----------+-----------+
                                |
                    summary + perspectives + gaps
```

Each step in detail:

1. **search** — `web_search(query, max_results=10)` via DuckDuckGo HTML
   (no API key), returns a list of `{title, url, snippet}`.
2. **fetch** — `web_fetch(url)` pulls the detailed content of each source (up to
   500 KiB, with circuit breaker and retries).
3. **entities** — regex extracts proper nouns, numbers, and dates from the text.
   Regex is cheap but "context-blind": it can't tell whether "Apple" is the company or the fruit.
4. **follow-up** — from entities + incomplete snippets, generate follow-up questions, e.g.:
   an entity appears in only one source with no definition → query `"what is <entity>"`.
5. **crossref** — `cross_reference(sources)`: for each source pair, compute
   vocabulary overlap; high overlap + same assertions → consensus,
   high overlap + differing negations → suspected conflict.
6. **think** — `DeepThink.analyze()` stitches the key quotes into a summary,
   groups perspectives by sentiment, and lists gaps as entities seen only once
   or follow-up questions left unanswered.

## 3. Usage (Python code)

```python
# Use the research core directly
from research import DeepSearch, DeepThink, cross_reference

ds = DeepSearch(search_fn=web_search, fetch_fn=web_fetch, max_hops=3)
result = ds.research("BoomBot three-layer architecture")

print(result["num_sources"])   # number of sources gathered
print(result["entities"][:10]) # top 10 entities
print(result["follow_ups"])    # generated follow-up questions

tk = DeepThink()
analysis = tk.analyze(topic="BoomBot three-layer architecture",
                      research_result=result)
print(analysis["summary"])
print(analysis["perspectives"])  # supporting / critical / neutral
print(analysis["gaps"])         # information gaps
```

Via the tool wrappers (recommended for the agent):

```python
from research.tools_wrapper import deep_search, deep_think

r = deep_search("today's coffee prices", max_hops=3)
# r = {ok, query, num_sources, sources[5]{url,title,snippet},
#      entities[10], follow_ups, agreement}

t = deep_think("today's coffee prices")
# t = {ok, topic, summary, perspectives, gaps, num_sources}
```

## 4. Wiring into environment.py (copy-paste)

1. Copy 3 files into the repo:
   - `agent/src/research/__init__.py`   <- this `__init__.py`
   - `agent/src/research/tools_wrapper.py`
   - (the core `deepsearch.py`, `deepthink.py`, `crossref.py` — written by another worker)

2. Add the import at the top of `environment.py`:

```python
from research.tools_wrapper import deep_search, deep_think
```

3. In the environment's `__init__`, after the `web_*` registration block, add:

```python
self._tools["deep_search"] = {
    "name": "deep_search",
    "func": deep_search,
    "description": "In-depth multi-step research: search, read in detail, "
                   "extract entities, generate follow-up questions, cross-reference sources. "
                   "Returns top 5 sources, top 10 entities, consensus level.",
    "schema": _schema(
        {"query": {"type": "string", "description": "The question/topic to research."},
         "max_hops": {"type": "integer", "description": "Max search rounds (default 3, max 10)."}},
        ["query"],
    ),
}
self._tools["deep_think"] = {
    "name": "deep_think",
    "func": deep_think,
    "description": "Deep reasoning: runs deep_search then analyzes into "
                   "a summary, perspectives, and information gaps.",
    "schema": _schema(
        {"topic": {"type": "string", "description": "The topic to reason about."},
         "query": {"type": "string", "description": "Optional search query (defaults to topic)."}},
        ["topic"],
    ),
}
```

Note: `tools_wrapper` fails soft — if `research` isn't present on the machine,
the tool returns `{"ok": False, "error": ...}` instead of crashing the gateway.

## 5. Current limitations and upgrade paths

- **Simple regex entity extraction**: catches capitalized names, numbers, and dates
  by pattern; it struggles with Vietnamese (unaccented names, abbreviations)
  and understands no semantics. Upgrade: call an LLM `extract_entities` via
  Boom's provider registry.
- **Word-overlap cross-reference (no embeddings yet)**: two sources saying the same
  thing in different words get scored low. Upgrade: embeddings
  (local like MiniLM, or an API) for cosine similarity.
- **Needs an LLM for better summaries**: the current summary is stitched quotes, which
  reads a bit choppy. Upgrade: `DeepThink.analyze()` accepts an optional
  `llm_fn` — use the LLM to rewrite when available, otherwise fall back to
  heuristics (interface unchanged).
- `max_hops` is capped at 10 to avoid runaway request loops; each hop
  adds another web_search + web_fetch, so latency grows linearly.
