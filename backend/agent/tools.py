"""Small, bounded demo tools. No tool can access the host filesystem or shell."""
from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
import operator
from urllib.parse import parse_qs, urlparse


def calculate(expression: str):
    """Evaluate arithmetic only; names, calls, and all other AST nodes are denied."""
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
           ast.USub: operator.neg, ast.UAdd: operator.pos}

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            if isinstance(node.op, ast.Pow) and abs(visit(node.right)) > 8:
                raise ValueError("exponent exceeds limit")
            value = ops[type(node.op)](visit(node.left), visit(node.right))
            if abs(value) > 1e15:
                raise ValueError("result exceeds limit")
            return value
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](visit(node.operand))
        raise ValueError("only basic arithmetic is allowed")

    return visit(ast.parse(expression, mode="eval"))


def run_tool(name: str, args: dict):
    if name == "calculate":
        return {"value": calculate(str(args.get("expr", "")))}
    if name == "verify":
        if "actual" not in args or "expected" not in args:
            raise ValueError("verify requires actual and expected values")
        actual, expected = args["actual"], args["expected"]
        return {"passed": actual == expected, "actual": actual, "expected": expected}
    if name == "search":
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("search requires a non-empty query")
        if args.get("offline") is True:
            return {"query": query, "results": [],
                    "note": "Live web search is disabled in offline demo mode."}
        return search_web(query)
    if name == "apply_change":
        return {"accepted": True, "applied": False,
                "proposal": str(args.get("description", "")),
                "note": "Approved proposal recorded. Connect a scoped executor to apply real changes."}
    raise ValueError(f"unsupported tool: {name}")


class _DuckDuckGoResults(HTMLParser):
    """Extract result links and snippets from DuckDuckGo's HTML endpoint."""

    def __init__(self):
        super().__init__()
        self.results = []
        self._anchor = None
        self._snippet = False
        self._text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "a" and "result__a" in classes:
            self._anchor = {"title": [], "url": attrs.get("href", "")}
        if "result__snippet" in classes:
            self._snippet = True
            self._text = []

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor["title"].append(data)
        if self._snippet:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            title = " ".join("".join(self._anchor["title"]).split())
            raw_url = self._anchor["url"]
            parsed = urlparse(raw_url)
            target = parse_qs(parsed.query).get("uddg", [raw_url])[0]
            if title and target.startswith(("http://", "https://")):
                self.results.append({"title": title, "url": target, "snippet": ""})
            self._anchor = None
        if tag in ("div", "a") and self._snippet:
            snippet = " ".join("".join(self._text).split())
            if snippet and self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = snippet
            self._snippet = False


def search_web(query: str, limit: int = 5):
    """Search several public indexes, tolerating an unavailable search provider."""
    import httpx

    headers = {"User-Agent": "MultiplayerAgent/1.0 (research assistant)"}

    def duckduckgo():
        response = httpx.get(
            "https://html.duckduckgo.com/html/", params={"q": query},
            headers={**headers, "User-Agent": "Mozilla/5.0 (compatible; MultiplayerAgent/1.0)"},
            timeout=5.0, follow_redirects=True,
        )
        response.raise_for_status()
        parser = _DuckDuckGoResults()
        parser.feed(response.text)
        return parser.results

    def wikipedia():
        response = httpx.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query,
                    "format": "json", "utf8": 1, "srlimit": limit},
            headers=headers, timeout=5.0,
        )
        response.raise_for_status()
        rows = response.json().get("query", {}).get("search", [])
        return [{"title": row["title"],
                 "url": "https://en.wikipedia.org/?curid=" + str(row["pageid"]),
                 "snippet": _plain_text(row.get("snippet", "")),
                 "source": "Wikipedia"} for row in rows]

    def crossref():
        response = httpx.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": query, "rows": limit},
            headers=headers,
            timeout=5.0,
        )
        response.raise_for_status()
        items = response.json().get("message", {}).get("items", [])
        results = []
        for item in items:
            title = (item.get("title") or [""])[0]
            if not title:
                continue
            authors = ", ".join(
                " ".join(part for part in (author.get("given"), author.get("family")) if part)
                for author in item.get("author", [])[:3]
            )
            date_parts = (item.get("published", {}) or item.get("created", {})).get("date-parts", [[]])
            year = str(date_parts[0][0]) if date_parts and date_parts[0] else ""
            results.append({"title": title, "url": item.get("URL", ""),
                            "snippet": "; ".join(part for part in (authors, year) if part),
                            "source": "Crossref"})
        return results

    sources = (duckduckgo, wikipedia, crossref)
    collected = []
    failures = []
    # Query independent providers concurrently so a slow source cannot delay
    # usable results from the others. Each request has its own short timeout.
    pool = ThreadPoolExecutor(max_workers=len(sources))
    futures = [pool.submit(source) for source in sources]
    try:
        for future in as_completed(futures, timeout=7.0):
            try:
                collected.extend(future.result())
            except Exception as exc:
                failures.append(type(exc).__name__)
    except TimeoutError:
        failures.append("provider timeout")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    results = []
    seen = set()
    for item in collected:
        url = item.get("url", "")
        key = urlparse(url).netloc + urlparse(url).path
        if url.startswith(("http://", "https://")) and key not in seen:
            seen.add(key)
            results.append(item)
            if len(results) >= limit:
                break
    if not results:
        detail = ", ".join(failures) or "all providers returned no matches"
        raise RuntimeError(f"Web search could not reach a public search source ({detail}). Check outbound internet access from the deployed server.")
    return {"query": query, "results": results, "sources": sorted({item.get("source", "Web") for item in results})}


def _plain_text(value: str) -> str:
    """Remove HTML tags from snippets returned by Wikipedia."""
    from html import unescape
    parser = _TextOnly()
    parser.feed(value)
    return " ".join(unescape("".join(parser.parts)).split())


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)
