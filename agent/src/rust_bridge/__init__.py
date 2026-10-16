"""Bridge to Rust performance binaries (JSON over stdin/stdout via subprocess).

Each wrapper falls back to a pure-Python implementation when its binary
is not built, so the agent works without a Rust toolchain.
"""
