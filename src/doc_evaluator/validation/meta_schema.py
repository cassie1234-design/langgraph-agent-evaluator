"""A structural meta-schema for OpenAPI documents.

Deliberately *not* the full OpenAPI 3.1 meta-schema. That document is ~1500
lines and its failure messages ("is not valid under any of the given schemas")
are useless to an integrator. What is worth catching with a schema is the
structural shape — is this even an OpenAPI document, are `paths` and `info`
the right kind of thing — and what is worth catching with named rules is
everything else, because those produce a message a human can act on.

So: schema for shape, rules for substance.
"""

from __future__ import annotations

from typing import Any

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

OPENAPI_STRUCTURE: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "OpenAPI structural shape",
    "type": "object",
    "required": ["openapi", "info", "paths"],
    "properties": {
        "openapi": {
            "type": "string",
            "pattern": r"^3\.[01](\.\d+)?$",
            "description": "OpenAPI 3.0.x or 3.1.x version string.",
        },
        "info": {
            "type": "object",
            "required": ["title", "version"],
            "properties": {
                "title": {"type": "string", "minLength": 1},
                "version": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
            },
        },
        "servers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["url"],
                "properties": {"url": {"type": "string", "minLength": 1}},
            },
        },
        "paths": {
            "type": "object",
            "propertyNames": {"pattern": "^/"},
            "additionalProperties": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    method: {"type": "object"} for method in HTTP_METHODS
                },
            },
        },
        "components": {"type": "object"},
    },
}
