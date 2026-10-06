class EmptyStreamError(RuntimeError):
    """Raised when a provider closes a stream without yielding a response."""


class StaleStreamNoOutputError(TimeoutError):
    """The stale detector killed a stream that never produced output.

    Raised instead of retrying the same silent model when a fallback provider is
    pending, so the turn fails over after one stale timeout instead of several."""


class MoAPresetNotFoundError(ValueError):
    """Raised when a persisted MoA preset no longer exists in config."""
