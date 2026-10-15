def sample_echo(message: str) -> str:
    """Return the echoed message."""
    return f"echo: {message}"

def register(ctx):
    ctx.register_tool(
        name="sample_echo",
        func=sample_echo,
        description="Echoes input back to caller",
        schema={"type": "object", "properties": {"message": {"type": "string"}}}
    )
