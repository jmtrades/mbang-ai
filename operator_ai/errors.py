"""User-input errors raised below the route layer. The app turns every InputError into a plain-English
4xx answer (422 unless the error says otherwise), so malformed input never surfaces as a 500."""
from __future__ import annotations


class InputError(ValueError):
    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status
