"""SSE 线格式助手测试。"""

from agentnet_core import SseParser, format_event


def test_format_event():
    assert format_event("state", '{"state": "working"}') == 'event: state\ndata: {"state": "working"}\n\n'


def test_parser_roundtrip():
    stream = (
        format_event("state", '{"state": "working"}')
        + format_event("delta", '{"text": "你好"}')
        + format_event("state", '{"state": "completed"}')
    )
    parser = SseParser()
    events = [e for line in stream.split("\n") if (e := parser.feed(line))]
    assert events == [
        ("state", '{"state": "working"}'),
        ("delta", '{"text": "你好"}'),
        ("state", '{"state": "completed"}'),
    ]


def test_parser_ignores_comments_and_buffers_partial():
    parser = SseParser()
    assert parser.feed(": heartbeat") is None
    assert parser.feed("event: delta") is None
    assert parser.feed('data: {"text": "a"}') is None
    assert parser.feed("") == ("delta", '{"text": "a"}')
    # 无 event 行时默认 message
    assert parser.feed('data: {"x": 1}') is None
    assert parser.feed("") == ("message", '{"x": 1}')
