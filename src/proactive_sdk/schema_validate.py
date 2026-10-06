"""Minimal JSON Schema 2020-12 validator for the keyword subset used by
``schemas/v1/*.json``.

Deliberately dependency-free so the contract tests run on the stdlib alone.
Supported keywords: ``type`` (single or list containing ``"null"``),
``properties``, ``required``, ``additionalProperties`` (boolean), ``enum``,
``const``, ``items``, ``minItems``/``maxItems``, ``uniqueItems``,
``minLength``/``maxLength``, ``minimum``/``maximum``, ``pattern``,
``format: date-time``, internal ``$ref`` (``#/$defs/<name>`` only).
Annotation keywords ($schema, $id, title, description, default, examples)
are accepted and ignored.

Cross-field rules are intentionally NOT expressed here; they live in
``contracts.validate_schedule`` / ``contracts.validate_decision`` with their
own tests (see schemas/README.md).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

__all__ = ["SchemaValidationError", "validate", "assert_valid"]

_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)


class SchemaValidationError(ValueError):
    """Raised by :func:`assert_valid` when instance fails the schema."""


def _check_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValueError(f"unknown schema type: {expected!r}")


def _check_format(value: str, fmt: str) -> str | None:
    if fmt != "date-time":
        return None  # unknown formats are annotations per spec
    if not _RFC3339_RE.fullmatch(value):
        return "not RFC 3339 date-time (T separator and Z/offset required)"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return "not a valid calendar time"
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return "timezone offset required"
    return None


def _resolve_ref(schema: dict, ref: str) -> dict:
    if not ref.startswith("#/$defs/"):
        raise ValueError(f"only internal #/$defs/ refs are supported: {ref!r}")
    try:
        target: Any = schema
        for part in ref[len("#/") :].split("/"):
            target = target[part]
    except (KeyError, TypeError):
        raise ValueError(f"unresolvable $ref: {ref!r}") from None
    return target


def validate(schema: dict, instance: Any, root: dict | None = None) -> list[str]:
    """Return a list of human-readable error paths/messages (empty = valid)."""
    root = root if root is not None else schema
    return _validate_node(schema, instance, root, "$")


def _validate_node(node: dict, instance: Any, root: dict, path: str) -> list[str]:
    errors: list[str] = []
    if "$ref" in node:
        return _validate_node(_resolve_ref(root, node["$ref"]), instance, root, path)

    expected_types = node.get("type")
    if expected_types is not None:
        type_list = expected_types if isinstance(expected_types, list) else [expected_types]
        if not any(_check_type(instance, t) for t in type_list):
            return [f"{path}: expected type {'/'.join(type_list)}, got {type(instance).__name__}"]

    if "const" in node and instance != node["const"]:
        errors.append(f"{path}: must equal const {node['const']!r}")
    if "enum" in node and instance not in node["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {node['enum']!r}")

    if isinstance(instance, str):
        fmt_err = _check_format(instance, node["format"]) if "format" in node else None
        if fmt_err:
            errors.append(f"{path}: {fmt_err}")
        if "minLength" in node and len(instance) < node["minLength"]:
            errors.append(f"{path}: shorter than minLength {node['minLength']}")
        if "maxLength" in node and len(instance) > node["maxLength"]:
            errors.append(f"{path}: longer than maxLength {node['maxLength']}")
        if "pattern" in node and re.search(node["pattern"], instance) is None:
            errors.append(f"{path}: does not match pattern {node['pattern']!r}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in node and instance < node["minimum"]:
            errors.append(f"{path}: below minimum {node['minimum']}")
        if "maximum" in node and instance > node["maximum"]:
            errors.append(f"{path}: above maximum {node['maximum']}")

    if isinstance(instance, list):
        if "minItems" in node and len(instance) < node["minItems"]:
            errors.append(f"{path}: fewer than minItems {node['minItems']}")
        if "maxItems" in node and len(instance) > node["maxItems"]:
            errors.append(f"{path}: more than maxItems {node['maxItems']}")
        if node.get("uniqueItems") is True:
            seen: list[Any] = []
            for item in instance:
                if item in seen:
                    errors.append(f"{path}: duplicate item {item!r} with uniqueItems")
                    break
                seen.append(item)
        if "items" in node:
            for idx, item in enumerate(instance):
                errors.extend(_validate_node(node["items"], item, root, f"{path}[{idx}]"))

    if isinstance(instance, dict):
        for key in node.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        props = node.get("properties", {})
        for key, value in instance.items():
            if key in props:
                errors.extend(_validate_node(props[key], value, root, f"{path}.{key}"))
            elif node.get("additionalProperties") is False:
                errors.append(f"{path}: unknown property {key!r}")
            elif isinstance(node.get("additionalProperties"), dict):
                errors.extend(
                    _validate_node(node["additionalProperties"], value, root, f"{path}.{key}")
                )
    return errors


def assert_valid(schema: dict, instance: Any) -> None:
    errors = validate(schema, instance)
    if errors:
        raise SchemaValidationError("; ".join(errors))
