"""Managed local-model runtime — download + supervise `llama-server` so a fresh
user reaches first chat with no external install (no Ollama, no manual pull).

Phase 1 ships the CPU path (universal, no GPU/driver matching); GPU (CUDA)
acquisition is Phase 3. Everything here is stdlib-only (urllib + zip/tarfile +
subprocess) so it also works inside the frozen desktop sidecar.
"""
