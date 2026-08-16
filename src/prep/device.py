"""Device selection for embedding.

This is why prep runs on the host: Docker Desktop has no GPU passthrough on macOS,
so embedding inside a container is CPU-only.
"""
from __future__ import annotations

import os


def pick_device() -> str:
    """EMBED_DEVICE if set, else cuda -> mps -> cpu."""
    explicit = os.getenv("EMBED_DEVICE")
    if explicit:
        return explicit

    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"
