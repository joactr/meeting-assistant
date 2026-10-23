from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

# JSON documents are the public application-command and native RPC boundary.
type JSONDocument = dict[str, Any]
type HandlerResult = JSONDocument | None
type CommandHandler = Callable[[JSONDocument], HandlerResult | Awaitable[HandlerResult]]
type CommandRegistry = dict[str, CommandHandler]
type NativeRequest = Callable[[str, JSONDocument], JSONDocument | Awaitable[JSONDocument]]


class NativeGateway(Protocol):
    def request(
        self, operation: str, payload: JSONDocument
    ) -> JSONDocument | Awaitable[JSONDocument]: ...
