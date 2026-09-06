"""Rendered prompt container with token and hashing metadata."""

import hashlib
from dataclasses import dataclass


@dataclass
class RenderedPrompt:
    text: str
    prompt_hash: str
    size_bytes: int
    estimated_tokens: int

    @classmethod
    def from_text(cls, text: str) -> "RenderedPrompt":
        encoded = text.encode("utf-8")
        h = hashlib.sha256(encoded).hexdigest()[:16]
        size = len(encoded)
        # Approximate tokens: ~4 chars per token for English / markdown
        tokens = max(1, len(text) // 4)
        return cls(
            text=text,
            prompt_hash=h,
            size_bytes=size,
            estimated_tokens=tokens,
        )
