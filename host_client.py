"""OmO host RPC client. Hermes-agnostic: socket, JSONL framing, protocol."""

from __future__ import annotations

import json


class JsonlBuffer:
    """Incremental JSONL decoder safe across arbitrary chunk boundaries."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> list[dict]:
        self._buf += chunk
        out: list[dict] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line.decode("utf-8")))
        return out
