from __future__ import annotations

import hashlib
import json
import math
import re
import time


def request_text(payload: dict[str, object]) -> str:
    return " ".join(
        str(item.get("content", "")) for item in payload.get("messages", [])
        if isinstance(item, dict) and item.get("role") in {"user", "tool"}
    )


def vectorize(text: str, dimensions: int) -> tuple[float, ...]:
    values = [0.0] * dimensions
    for token in re.findall(r"[\w'-]+", text.casefold()):
        digest = hashlib.sha256(token.encode()).digest()
        values[int.from_bytes(digest[:4], "big") % dimensions] += 1
    norm = math.sqrt(sum(value * value for value in values)) or 1
    return tuple(value / norm for value in values)


def scope_key(payload: dict[str, object]) -> str:
    scoped = {
        key: payload.get(key) for key in (
            "model", "tools", "tool_choice", "response_format", "reasoning",
            "reasoning_effort", "temperature", "top_p", "max_tokens",
        )
    }
    roles = [item.get("role") for item in payload.get("messages", []) if isinstance(item, dict)]
    scoped["roles"] = roles
    return hashlib.sha256(
        json.dumps(scoped, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class MemorySemanticCache:
    def __init__(self, config):
        self.config = config
        self.entries: dict[
            str, list[tuple[float, str, str, tuple[float, ...], dict[str, object]]]
        ] = {}

    async def get(self, tenant_id: str, payload: dict[str, object]) -> dict[str, object] | None:
        target = vectorize(request_text(payload), self.config.dimensions)
        scope = scope_key(payload)
        now = time.time()
        best = None
        for created, version, entry_scope, candidate, response in self.entries.get(tenant_id, []):
            if (
                now - created > self.config.ttl_seconds
                or version != self.config.embedding_version
                or entry_scope != scope
            ):
                continue
            similarity = sum(left * right for left, right in zip(target, candidate))
            if similarity >= self.config.similarity_threshold and (best is None or similarity > best[0]):
                best = similarity, response
        return json.loads(json.dumps(best[1])) if best else None

    async def put(
        self, tenant_id: str, payload: dict[str, object], response: dict[str, object],
    ) -> None:
        items = self.entries.setdefault(tenant_id, [])
        items.append((
            time.time(), self.config.embedding_version,
            scope_key(payload),
            vectorize(request_text(payload), self.config.dimensions),
            json.loads(json.dumps(response)),
        ))
        if len(items) > self.config.max_entries_per_tenant:
            del items[:-self.config.max_entries_per_tenant]

    async def close(self) -> None:
        self.entries.clear()
