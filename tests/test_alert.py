import httpx

from autotik import alert


def test_posts_json_payload():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert alert.send("https://hook.test/x", "error", "discovery returned 0", client) is True
    assert seen["url"] == "https://hook.test/x"
    assert b"discovery returned 0" in seen["body"]
    assert b'"level":"error"' in seen["body"]


def test_connection_error_returns_false_without_raising():
    def handler(request):
        raise httpx.ConnectError("nope")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert alert.send("https://hook.test/x", "warn", "m", client) is False


def test_non_2xx_returns_false():
    def handler(request):
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert alert.send("https://hook.test/x", "error", "m", client) is False


def test_empty_url_is_noop():
    assert alert.send("", "error", "m") is False
