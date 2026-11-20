class DomainError(Exception):
    """业务规则错误，映射为 HTTP 响应。

    extra 会并入响应 JSON，用于携带机器可读的补充信息
    （如缺稿录音号列表、失效原因），detail 仍为人类可读的说明。
    """

    def __init__(self, status_code: int, detail: str, extra: dict | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.extra = extra or {}
