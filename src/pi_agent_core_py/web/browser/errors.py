"""Public, fixed browser error codes shared by trusted internal components."""


class BrowserError(RuntimeError):
    def __init__(self, code: str = "browser_unavailable") -> None:
        super().__init__(code)
        self.code = code
