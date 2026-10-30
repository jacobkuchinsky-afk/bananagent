"""Web search: one query in, a short list of results out.

No API key and no browser. The ddgs package does the fetching: it reads the
ordinary results pages of Bing, DuckDuckGo, Yahoo and others, trying them in
turn until one answers. It matters that it's ddgs and not a plain HTTP client:
it presents a real browser's TLS fingerprint, and without that the engines
spot a script and quietly hand back junk — Bing matches only the first word
("standing desk 55 inch" comes back as MLB standings), DuckDuckGo answers 202,
the rest show a captcha. That junk looks like results, so it failed silently.

A research subagent calls this dozens of times in a row, so searches are
spaced at least SEARCH_GAP_SECONDS apart and the same query in the same run of
the app is answered from memory instead of being sent again.
"""

import re
import threading
import time

import config
from .registry import MINOR, endpoint, param

_SPACE = re.compile(r"\s+")

_lock = threading.Lock()
_last = 0.0
_cache = {}  # (query, count) -> answer


def search(query, count=8):
    """Results for one query, from whichever engine answers first."""
    global _last
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    query = _SPACE.sub(" ", str(query)).strip()
    if not query:
        raise ValueError("Give it something to search for.")
    count = max(1, min(int(count or 8), 10))
    key = (query.lower(), count)
    if key in _cache:
        return _cache[key]

    # Two tries: an engine now and then answers a perfectly good query with
    # nothing, and the same query a moment later comes back full.
    found, problem = [], None
    for _ in range(2):
        with _lock:
            wait = config.SEARCH_GAP_SECONDS - (time.time() - _last)
            if wait > 0:
                time.sleep(wait)
            try:
                found = DDGS(timeout=config.SEARCH_TIMEOUT).text(
                    query, max_results=count, backend=config.SEARCH_BACKENDS)
            except DDGSException as e:
                # ddgs says "No results found." both when there are none and
                # when every engine refused; either way there's nothing here.
                found, problem = [], str(e)
            finally:
                _last = time.time()
        if found:
            break

    seen, results = set(), []
    for item in found or []:
        url = item.get("href") or ""
        if url in seen or not url.startswith("http"):
            continue
        seen.add(url)
        results.append({"title": _SPACE.sub(" ", item.get("title") or "").strip(),
                        "url": url,
                        "snippet": _SPACE.sub(" ", item.get("body") or "").strip()
                        [:config.SEARCH_SNIPPET_CHARS]})
    answer = {"query": query, "results": results[:count]}
    if not results:
        answer["note"] = ("Nothing came back. Try fewer or different words."
                          + (f" ({problem})" if problem else ""))
    else:
        _cache[key] = answer  # a miss might be a hiccup, so only hits are kept
    return answer


@endpoint(
    tier=MINOR,
    description=(
        "Searches the web and returns the top results: each one's title, link and a "
        "short snippet of the page. Use it for anything current or that you don't "
        "know for sure — news, prices, products, people, how-tos. One query per call; "
        "short keyword queries work best."
    ),
    params={
        "query": param("string", "What to search for, as you'd type it into a search engine.",
                       required=True),
        "count": param("integer", "How many results, 1-10.", default=8),
    },
    returns="The query and a list of results with title, url and snippet.",
)
def web_search(query, count=8):
    return search(query, count)
