"""The tax package's error for invalid input (shared by the engine and coin tracing)."""


class EngineError(ValueError):
    """The events are invalid input: the caller must not have produced them."""
