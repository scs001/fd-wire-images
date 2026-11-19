"""副驾流式端点与增量补全（assistant-streaming）。

覆盖：帧顺序、消息帧与阻塞端点等价、失败发 error 帧、可选流式补全（有则出
delta 帧、无则照常完成）、tool_calls 分片累积与解析失败回退。
"""

from __future__ import annotations

import json

import httpx
import pytest

from fd_wire_api.ai.llm import TokenGatewayLlmClient
from fd_wire_api.ai.protocols import LlmAssistantMessage, ToolCall

from test_ai_api import (  # noqa: F401 - fixture 复用（tests 目录在 sys.path 上）
    CUST,
    FakeExecutor,
    FakeLlm,
    client,
    create,
    fake_executor,
    fake_llm,
    text_reply,
    tool_reply,
)


def parse_frames(raw: str) -> list[tuple[str, dict | None]]:
    """SSE 文本 → [(event, payload)]；keepalive 注释行不产生条目。"""
    frames: list[tuple[str, dict | None]] = []
    for block in raw.split("\n\n"):
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if event:
            frames.append((event, data))
    return frames


class StreamingFakeLlm(FakeLlm):
    """在 FakeLlm 之上补一个流式实现：先吐几段增量，再交回脚本体。"""

    def __init__(self, script: list) -> None:
        super().__init__(script)
        self.chunks: list[str] = []

    def complete_stream(self, messages, tools, on_delta):
        self.calls.append({"messages": messages, "tools": tools, "stream": True})
        for acc in self.chunks:
            on_delta(acc)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _stream(client, conversation_id: int, content: str) -> str:
    resp = client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages/stream",
        json={"content": content},
        headers=CUST,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return resp.text


def test_stream_frames_and_done_equivalence(client, fake_llm) -> None:
    """帧按发生顺序到达，done 帧的消息集合与阻塞端点一致。"""
    fake_llm.script = [text_reply("你的数据集有三个。")]
    body = create(client, title="流式", message=None)
    cid = body["id"]

    frames = parse_frames(_stream(client, cid, "我有哪些数据集？"))
    kinds = [event for event, _ in frames]
    assert kinds == ["message", "message", "done"]

    # 第一条 = 客户消息（自己的消息也要即时可见）
    first = frames[0][1]
    assert first["role"] == "user"
    assert first["content"] == "我有哪些数据集？"

    # done 帧 = 本轮权威集合，与阻塞端点返回逐条一致
    done = frames[-1][1]
    blocking = client.get(f"/api/v1/ai/conversations/{cid}/messages", headers=CUST).json()
    assert [m["content"] for m in done["messages"]] == [m["content"] for m in blocking]
    assert [m["role"] for m in done["messages"]] == [m["role"] for m in blocking]
    assert done["conversation_id"] == cid


def test_stream_emits_delta_when_llm_supports_it(client) -> None:
    """模型侧支持流式 → 增量帧出现在消息帧之前，载荷是累加全文。"""
    llm = StreamingFakeLlm([text_reply("完整回答。")])
    llm.chunks = ["完整", "完整回答", "完整回答。"]
    from fd_wire_api.app import app
    from fd_wire_api.ai.routes import get_llm_client

    app.dependency_overrides[get_llm_client] = lambda: llm
    try:
        body = create(client, title="流式增量", message=None)
        frames = parse_frames(_stream(client, body["id"], "说点什么"))
    finally:
        app.dependency_overrides[get_llm_client] = lambda: None

    deltas = [payload["text"] for event, payload in frames if event == "delta"]
    assert deltas == ["完整", "完整回答", "完整回答。"]
    # 最后一条 delta 与最终 assistant 消息文本一致
    assistant = [p for e, p in frames if e == "message" and p["role"] == "assistant"]
    assert assistant[-1]["content"] == deltas[-1]


def test_stream_error_frame_on_llm_failure(client, fake_llm) -> None:
    """模型失败 → error 帧如实说明，不伪造 done。"""
    fake_llm.script = [RuntimeError("网关炸了")]
    body = create(client, title="失败", message=None)
    frames = parse_frames(_stream(client, body["id"], "问一句"))
    kinds = [event for event, _ in frames]
    assert kinds[-1] == "error"
    assert "done" not in kinds
    assert "网关炸了" in frames[-1][1]["message"]


def test_stream_frame_shape_matches_list_endpoint(client, fake_llm) -> None:
    """中间工具留痕也逐条推出（不等整轮结束）。"""
    fake_llm.script = [
        tool_reply(ToolCall(id="c1", name="dataset_list", arguments={})),
        text_reply("你有一个数据集。"),
    ]
    body = create(client, title="工具", message=None)
    frames = parse_frames(_stream(client, body["id"], "列一下数据集"))
    roles = [p["role"] for e, p in frames if e == "message"]
    assert roles == ["user", "assistant", "tool_trace", "assistant"]


# ── 增量补全的解析（直接打 TokenGatewayLlmClient，httpx 走 MockTransport）──


def _client_with(handler) -> TokenGatewayLlmClient:
    transport = httpx.MockTransport(handler)
    return TokenGatewayLlmClient(
        base_url="http://gw.test",
        api_key="k",
        model="m",
        http_client=httpx.Client(transport=transport),
    )


def _sse_body(frames: list[dict]) -> bytes:
    lines = [f"data: {json.dumps(f)}\n\n" for f in frames] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


def test_llm_complete_stream_accumulates_text() -> None:
    body = _sse_body(
        [
            {"choices": [{"delta": {"content": "你"}}]},
            {"choices": [{"delta": {"content": "好"}}]},
            {"choices": [{"delta": {}}]},
        ]
    )
    seen: list[str] = []
    client = _client_with(lambda request: httpx.Response(200, content=body))
    msg = client.complete_stream([], [], seen.append)
    assert seen == ["你", "你好"]  # 累加全文，不是片段
    assert msg.content == "你好"
    assert not msg.has_tool_calls


def test_llm_complete_stream_accumulates_tool_call_fragments() -> None:
    frag1 = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "c1", "function": {"name": "dataset_list", "arguments": '{"li'}}
    ]}}]}
    frag2 = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": 'mit": 5}'}}
    ]}}]}
    client = _client_with(lambda request: httpx.Response(200, content=_sse_body([frag1, frag2])))
    msg = client.complete_stream([], [], lambda _t: None)
    assert msg.tool_calls == (ToolCall(id="c1", name="dataset_list", arguments={"limit": 5}),)


def test_llm_complete_stream_falls_back_when_fragments_unparsable() -> None:
    """分片拼不成合法 JSON → 退回非流式补全（本轮只有打字效果受损）。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if payload.get("stream"):
            bad = {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "dataset_list", "arguments": "{oops"}}
            ]}}]}
            return httpx.Response(200, content=_sse_body([bad]))
        return httpx.Response(
            200,
            json={"choices": [{"message": {
                "content": None,
                "tool_calls": [{"id": "c1", "function": {
                    "name": "dataset_list", "arguments": "{}"}}],
            }}]},
        )

    client = _client_with(handler)
    msg = client.complete_stream([], [], lambda _t: None)
    assert [bool(c.get("stream")) for c in calls] == [True, False]
    assert msg.tool_calls == (ToolCall(id="c1", name="dataset_list", arguments={}),)
