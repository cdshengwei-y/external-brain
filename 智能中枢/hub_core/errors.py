class HubError(Exception):
    status_code = 400

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class NotFoundError(HubError):
    def __init__(self, message: str):
        super().__init__(message, 404)


class ConflictError(HubError):
    def __init__(self, message: str):
        super().__init__(message, 409)


class ValidationError(HubError):
    def __init__(self, message: str):
        super().__init__(message, 422)
