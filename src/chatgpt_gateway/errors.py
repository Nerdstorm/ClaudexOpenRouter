from typing import Any


class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        code: str,
        param: str | None = None,
        *,
        body: dict[str, Any] | None = None,
        request_id: str | None = None,
        retry_after: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.param = param
        self.message = message
        self.body = body
        self.request_id = request_id
        self.retry_after = retry_after
        self.headers = headers or {}

    def payload(self) -> dict[str, Any]:
        if self.body is not None:
            return self.body
        return {
            "error": {
                "message": self.message,
                "type": "invalid_request_error" if self.status < 500 else "gateway_error",
                "code": self.code,
                "param": self.param,
            }
        }


def invalid(message: str, param: str | None = None) -> GatewayError:
    return GatewayError(400, message, "unsupported_parameter", param)
