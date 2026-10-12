import time

from backend.agent import tools


class FakeResponse:
    def __init__(self, text="", payload=None):
        self.text = text
        self.payload = payload or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_web_results_are_not_ordered_by_provider_race(monkeypatch):
    def fake_get(url, **kwargs):
        if "duckduckgo" in url:
            time.sleep(0.03)
            return FakeResponse(
                '<a class="result__a" href="https://apple.com/mac/m1">Apple M1</a>'
                '<div class="result__snippet">Apple silicon overview</div>'
            )
        if "wikipedia" in url:
            time.sleep(0.02)
            return FakeResponse(payload={"query": {"search": []}})
        time.sleep(0.001)
        return FakeResponse(payload={"message": {"items": [{
            "title": ["Unrelated academic record"], "URL": "https://doi.org/10/example",
        }]}})

    monkeypatch.setattr("httpx.get", fake_get)
    result = tools.search_web("Apple M1 overview", limit=1)
    assert result["results"][0]["url"] == "https://apple.com/mac/m1"
    assert result["sources"] == ["Web"]
