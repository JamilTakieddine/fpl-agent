"""The minimal HTTP surface the agent depends on.

Code takes these Protocols instead of requests.Session, so tests pass a fake without
type: ignore, and the dependency is explicit: only get/post with headers/timeout/data,
status_code, json() and raise_for_status(). requests.Session satisfies them structurally.
The keyword arguments are listed explicitly: a Protocol with **kwargs would demand that every
implementation accept arbitrary keywords, which requests.Session doesn't.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...
    def json(self) -> Any: ...
    def raise_for_status(self) -> None: ...


class HttpSession(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = ...,
        timeout: float | None = ...,
    ) -> HttpResponse: ...

    def post(
        self,
        url: str,
        *,
        data: Mapping[str, str] | None = ...,
        headers: Mapping[str, str] | None = ...,
        timeout: float | None = ...,
    ) -> HttpResponse: ...
