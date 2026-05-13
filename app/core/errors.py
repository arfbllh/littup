class AppError(Exception):
    def __init__(self, message: str, *, code: str = "APP_ERROR", status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class NotFoundError(AppError):
    def __init__(self, message: str, *, code: str = "NOT_FOUND"):
        super().__init__(message, code=code, status_code=404)


class ValidationError(AppError):
    def __init__(self, message: str, *, code: str = "VALIDATION_ERROR"):
        super().__init__(message, code=code, status_code=422)


class ConflictError(AppError):
    def __init__(self, message: str, *, code: str = "CONFLICT"):
        super().__init__(message, code=code, status_code=409)


class BudgetExceededError(AppError):
    def __init__(self, message: str, *, code: str = "BUDGET_EXCEEDED"):
        super().__init__(message, code=code, status_code=429)


class LLMUnavailableError(AppError):
    def __init__(self, message: str, *, code: str = "LLM_UNAVAILABLE"):
        super().__init__(message, code=code, status_code=503)


class IngestError(AppError):
    def __init__(self, message: str, *, code: str = "INGEST_ERROR"):
        super().__init__(message, code=code, status_code=422)


class RateLimitError(AppError):
    def __init__(self, message: str, *, code: str = "RATE_LIMITED", retry_after: int = 60):
        super().__init__(message, code=code, status_code=429)
        self.retry_after = retry_after
