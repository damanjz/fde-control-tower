"""Local LLM client (Ollama): chat with tools, and embeddings. One chat call at a time: the GPU is shared."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

import httpx

from tower.config import Settings

_GPU = threading.Lock()


class LLMError(RuntimeError):
    """Ollama unreachable, slow, or returned something unusable."""


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ms: int = 0
    model: str = ""


def parse_chat(data: dict, model: str) -> ChatResult:
    msg = data.get("message") or {}
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append(ToolCall(fn.get("name", ""), args if isinstance(args, dict) else {}))
    return ChatResult((msg.get("content") or "").strip(), calls, data.get("prompt_eval_count", 0),
                      data.get("eval_count", 0), 0, data.get("model", model))


def chat(settings: Settings, messages: list[dict], tools: list[dict] | None = None, *, temperature: float = 0.1) -> ChatResult:
    """/api/chat, no streaming, thinking off. Exactly one system message, first (Qwen templates reject more)."""
    if sum(1 for m in messages if m["role"] == "system") > 1 or (messages and messages[0]["role"] != "system"):
        raise LLMError("messages must start with exactly one system message")
    payload: dict = {"model": settings.llm_model, "messages": messages, "stream": False, "think": False,
                     "options": {"temperature": temperature, "num_ctx": settings.llm_num_ctx}}
    if tools:
        payload["tools"] = tools
    t0 = time.monotonic()
    with _GPU:
        try:
            r = httpx.post(f"{settings.llm_url}/api/chat", json=payload, timeout=settings.llm_timeout_s)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMError(f"chat request failed: {type(e).__name__}") from e
    res = parse_chat(data, settings.llm_model)
    res.ms = int((time.monotonic() - t0) * 1000)
    return res


def embed(settings: Settings, texts: list[str], kind: str = "document") -> list[list[float]]:
    """nomic-embed-text vectors (768 dims). `kind` is 'document' or 'query' (the model wants a task prefix)."""
    prefix = "search_query: " if kind == "query" else "search_document: "
    try:
        # Embeddings run on the CPU (num_gpu 0): on a 16 GB card the 27B chat model needs nearly all of the
        # video memory, and a resident embedding model pushed it out of VRAM (measured 11 vs 44 tok/s).
        r = httpx.post(f"{settings.llm_url}/api/embed", timeout=120,
                       json={"model": settings.embed_model, "input": [prefix + t for t in texts],
                             "options": {"num_gpu": 0}})
        r.raise_for_status()
        vecs = r.json()["embeddings"]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        raise LLMError(f"embed request failed: {type(e).__name__}") from e
    if len(vecs) != len(texts) or any(len(v) != 768 for v in vecs):
        raise LLMError("embedding response has the wrong shape")
    return vecs


def vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def llm_up(settings: Settings) -> bool:
    try:
        return httpx.get(f"{settings.llm_url}/api/tags", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False
