class AppError(Exception):
    """An error with a safe, user-facing message. Never put secrets in `message`."""

    def __init__(self, code: str, message: str, status: int = 500):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
