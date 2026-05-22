"""
Dedup-Hash-Tests (stdlib-only, kein Netz/DB). Sichern die Eigenschaft, dass
ein und dieselbe Story über Fetch-Zyklen hinweg denselben content_hash bekommt
— auch wenn HN-Punkte / GitHub-Stars im Text hochticken — und sich damit nicht
mehr 8-10x/Tag in raw_items dupliziert.

Lauf:  python3 -m ingestion.test_dedup   (aus dem Repo-Root)
"""
from .adapters import _canonical_url, _content_hash, _normalize_text


def _check(name, cond):
    if not cond:
        raise AssertionError(f"FAIL: {name}")
    print(f"ok: {name}")


def main():
    # 1. HN: gleiche Story, andere Punktzahl -> gleicher Hash (URL-getrieben).
    hn_url = "https://news.ycombinator.com/item?id=42"
    h1 = _content_hash("[HN 120pts] OpenAI ships new model", "hackernews", hn_url)
    h2 = _content_hash("[HN 487pts] OpenAI ships new model", "hackernews", hn_url)
    _check("HN same story, different points -> same hash", h1 == h2)

    # 2. GitHub: gleiches Repo, andere Sternzahl -> gleicher Hash.
    gh_url = "https://github.com/foo/bar"
    g1 = _content_hash("[GitHub ★120] foo/bar: a tool", "github_trending", gh_url)
    g2 = _content_hash("[GitHub ★1503] foo/bar: a tool", "github_trending", gh_url)
    _check("GitHub same repo, different stars -> same hash", g1 == g2)

    # 3. Verschiedene Stories -> verschiedene Hashes.
    d1 = _content_hash("[HN 10pts] Story A", "hackernews", "https://x.test/a")
    d2 = _content_hash("[HN 10pts] Story B", "hackernews", "https://x.test/b")
    _check("different URLs -> different hashes", d1 != d2)

    # 4. Tracking-Params + http/https + trailing slash kollabieren auf eine Identität.
    a = _content_hash("t", "tech_news", "http://Example.com/Path/?utm_source=rss&id=9")
    b = _content_hash("t", "tech_news", "https://example.com/Path?id=9&fbclid=abc")
    _check("tracking params / scheme / slash normalized -> same hash", a == b)

    # 5. Ohne URL: Fallback auf normalisierten Text (Badge-strip) + Source.
    n1 = _content_hash("[HN 5pts] No-link musing", "hackernews", None)
    n2 = _content_hash("[HN 99pts]   No-link   musing", "hackernews", None)
    _check("no-url fallback strips badge + whitespace -> same hash", n1 == n2)

    # 6. Fallback unterscheidet weiterhin echte verschiedene Texte.
    _check("no-url fallback keeps distinct text distinct",
           _content_hash("alpha", "s", None) != _content_hash("beta", "s", None))

    # 7. Helper-Sanity.
    _check("_canonical_url drops fragment",
           _canonical_url("https://a.test/x#frag") == "https://a.test/x")
    _check("_normalize_text strips GitHub badge",
           _normalize_text("[GitHub ★42] foo/bar") == "foo/bar")

    print("\nALL DEDUP TESTS PASSED")


if __name__ == "__main__":
    main()
