"""Turning a plain function into a tool (SPEC §22.1 item 9).

The point is that a user who already has a Python function should not have
to hand-write a JSON Schema to expose it. ``@tool`` reads the signature,
the type hints and the docstring, and derives the `ToolSpec`.

What it deliberately does NOT do: guess. Every declared parameter must
carry a type hint it understands, and a hint it cannot map is an error at
import time rather than a silently-wrong schema at run time. The declared
schema is what the model sees, so an approximated one would be a quiet lie
about the tool's surface.

Read-only is enforced, not defaulted: `ToolSpec` refuses `read_only=False`
because writes are supposed to go through grants and approvals, and a
decorator is not the place to bypass that.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import typing
from dataclasses import dataclass
from typing import Any, Callable, get_args, get_origin, get_type_hints

from .contracts import ErrorCode, PASError
from .tools import ToolSpec

__all__ = ["Tool", "tool", "schema_for_type", "register_tools"]

_NONE_TYPE = type(None)


def schema_for_type(annotation: Any, *, name: str) -> dict[str, Any]:
    """Map one Python annotation onto the JSON Schema subset PAS validates.

    Raises rather than approximating: a schema the tool does not actually
    honour is worse than a loud failure at import time.
    """
    origin = get_origin(annotation)

    if origin is typing.Union:  # Optional[X] and X | None
        members = [arg for arg in get_args(annotation) if arg is not _NONE_TYPE]
        if len(members) == 1:
            return schema_for_type(members[0], name=name)
        raise PASError(
            ErrorCode.INVALID_CONFIG,
            f"parameter {name!r}: unions of several types have no schema mapping",
            scope="toolkit",
        )
    if origin in (list, set, frozenset, tuple):
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}

    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is list:
        return {"type": "array"}
    if annotation is dict:
        return {"type": "object"}
    raise PASError(
        ErrorCode.INVALID_CONFIG,
        f"parameter {name!r}: cannot map {annotation!r} onto a JSON Schema type;"
        " annotate it explicitly",
        scope="toolkit",
    )


@dataclass(frozen=True)
class Tool:
    """A decorated function together with the surface it declares."""

    spec: ToolSpec
    handler: Callable[[dict[str, Any]], Any]

    def register_into(self, broker: Any) -> None:
        broker.register(self.spec, self._handler)
        return None

    async def _handler(self, arguments: dict[str, Any]) -> str:
        result = self.handler(arguments)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, str):
            return result
        # Anything structured becomes canonical JSON so the model gets a
        # stable, re-readable observation rather than a Python repr.
        return json.dumps(result, ensure_ascii=False, sort_keys=True)


def register_tools(broker: Any, tools: typing.Iterable[Any]) -> tuple[str, ...]:
    """Register a mixed list of `Tool` objects and raw `(spec, handler)` pairs."""
    names: list[str] = []
    for entry in tools:
        if isinstance(entry, Tool):
            entry.register_into(broker)
            names.append(entry.spec.name)
        elif isinstance(entry, tuple) and len(entry) == 2:
            spec, handler = entry
            broker.register(spec, handler)
            names.append(spec.name)
        else:
            raise PASError(
                ErrorCode.INVALID_CONFIG,
                f"cannot register {type(entry).__name__} as a tool;"
                " use @tool or a (ToolSpec, handler) pair",
                scope="toolkit",
            )
    return tuple(names)


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    capability: str | None = None,
    max_output_bytes: int | None = None,
) -> Any:
    """Expose a function to the agent's analysis loop.

    Usable bare (``@tool``) or configured (``@tool(capability="memory.read")``).
    ``capability`` is the grant a run must hold before the broker lets the
    call through — without it the tool is unreachable, which is the point.
    """

    def decorate(function: Callable[..., Any]) -> Tool:
        signature = inspect.signature(function)
        hints = get_type_hints(function)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter_name, parameter in signature.parameters.items():
            if parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    f"tool {function.__name__!r} must not take *args/**kwargs;"
                    " the declared schema has to be exact",
                    scope="toolkit",
                )
            annotation = hints.get(parameter_name, parameter.annotation)
            if annotation is inspect.Parameter.empty:
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    f"tool {function.__name__!r}: parameter {parameter_name!r} needs a"
                    " type hint (the model is shown this schema)",
                    scope="toolkit",
                )
            schema = schema_for_type(annotation, name=parameter_name)
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter_name)
            else:
                if parameter.default is not None:
                    schema = {**schema, "default": parameter.default}
                else:
                    schema = schema
            properties[parameter_name] = schema

        doc = inspect.getdoc(function) or ""
        summary = description or doc.split("\n\n")[0].strip() or function.__name__
        tool_name = name or function.__name__
        tool_description = summary.splitlines()[0][:1000]

        spec = ToolSpec(
            name=tool_name,
            description=tool_description,
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            required_capability=capability,
            read_only=True,
            **({"max_output_bytes": max_output_bytes} if max_output_bytes else {}),
        )

        async def call(arguments: dict[str, Any]) -> str:
            missing = [key for key in required if key not in arguments]
            if missing:
                raise PASError(
                    ErrorCode.INVALID_CONFIG,
                    f"tool {tool_name!r} missing required arguments: {missing}",
                    scope="toolkit",
                )
            result = function(**arguments)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, str):
                return result
            return json.dumps(result, ensure_ascii=False, sort_keys=True)

        return Tool(spec=spec, handler=call)

    if fn is not None:
        return decorate(fn)
    return decorate
