class DomainError(Exception):
    """业务规则错误，映射为 HTTP 响应。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
