"""Shared domain exceptions (kept dependency-free)."""


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class CostConfirmationRequired(Exception):
    """A batch is expensive enough to need explicit authorization.

    Carries the estimate so the caller can show the operator what to confirm
    rather than making them guess the figure.
    """

    def __init__(self, message: str, estimate: dict):
        super().__init__(message)
        self.estimate = estimate
