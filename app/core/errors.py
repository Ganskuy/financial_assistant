class SafeError(Exception):
    """Only messages from this exception family may be sent to the user."""


class InvalidInput(SafeError):
    pass


class NotFound(SafeError):
    def __init__(self) -> None:
        super().__init__("Not found, expired, or unavailable to this account.")


class BudgetExceeded(SafeError):
    def __init__(self) -> None:
        super().__init__(
            "Daily AI budget reached or insufficient allowance remains. Reset: 00:00 Asia/Jakarta. /balance, /history and /report still work."
        )


class ModelUnavailable(SafeError):
    def __init__(self) -> None:
        super().__init__(
            "AI is temporarily unavailable. Please try again later. No financial record was written."
        )


class InvalidModelOutput(SafeError):
    def __init__(self, reason: str = "invalid_output", *, retryable: bool = True) -> None:
        self.reason = reason
        self.retryable = retryable
        super().__init__(
            "AI output could not be validated. Please use clearer text or a sharper receipt photo. No financial record was written."
        )


class TelegramUnavailable(Exception):
    pass
