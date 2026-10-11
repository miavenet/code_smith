"""The runner's own small validator for its result schemas, and the checks of meaning.

Deliberately limited: types, enums, required keys, no unknown keys, string lengths. It does not
claim to implement JSON Schema. A provider's schema feature makes valid answers likelier; this is
the check, whichever agent produced the answer.
"""

SUMMARY_MAX = 4000

RESPONSE = {"type": "object", "additionalProperties": False,
            "required": ["finding", "action", "note"],
            "properties": {"finding": {"type": "string"},
                           "action": {"type": "string", "enum": ["fixed", "disputed"]},
                           "note": {"type": "string"}}}

PRODUCE = {"type": "object", "additionalProperties": False,
           "required": ["outcome", "summary", "blocked_reason", "responses"],
           "properties": {"outcome": {"type": "string", "enum": ["done", "blocked"]},
                          "summary": {"type": "string", "maxLength": SUMMARY_MAX},
                          "blocked_reason": {"type": "string"},
                          "responses": {"type": "array", "items": RESPONSE},
                          "advisories_addressed": {"type": "array", "items": {
                              "type": "object", "additionalProperties": False,
                              "required": ["finding", "note"],
                              "properties": {"finding": {"type": "string"},
                                             "note": {"type": "string"}}}}}}

FINDING = {"type": "object", "additionalProperties": False,
           "required": ["severity", "title", "detail", "location", "caused_by"],
           "properties": {"severity": {"type": "string", "enum": ["blocking", "advisory"]},
                          "title": {"type": "string"}, "detail": {"type": "string"},
                          "location": {"type": "string"}, "caused_by": {"type": "string"},
                          "gateable": {"type": "object", "additionalProperties": False,
                                       "required": ["command_hint"],
                                       "properties": {"command_hint": {"type": "string"}}}}}

RESOLUTION = {"type": "object", "additionalProperties": False,
              "required": ["finding", "status", "note"],
              "properties": {"finding": {"type": "string"},
                             "status": {"type": "string", "enum": ["resolved", "unresolved"]},
                             "note": {"type": "string"}}}

REVIEW = {"type": "object", "additionalProperties": False,
          "required": ["verdict", "summary", "findings", "resolutions"],
          "properties": {"verdict": {"type": "string", "enum": ["pass", "block"]},
                         "summary": {"type": "string", "maxLength": SUMMARY_MAX},
                         "findings": {"type": "array", "items": FINDING},
                         "resolutions": {"type": "array", "items": RESOLUTION},
                         "reach_audit": {"type": "array", "items": {
                             "type": "object", "additionalProperties": False,
                             "required": ["test", "state_claimed", "reach_evidence", "oracle"],
                             "properties": {k: {"type": "string"} for k in
                                            ("test", "state_claimed", "reach_evidence", "oracle")}}}}}

SCHEMAS = {"produce": PRODUCE, "review": REVIEW}

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


def check_shape(value, schema, where="answer"):
    """Every way `value` departs from `schema`, in words. An empty list means it is valid."""
    kind = schema["type"]
    if not isinstance(value, _TYPES[kind]) or (kind != "boolean" and isinstance(value, bool)):
        return [f"{where} must be a JSON {kind}, not {_name(value)}"]
    errors = []
    if kind == "object":
        props = schema["properties"]
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where} lacks the required key '{key}'")
        for key in value:
            if key not in props:
                errors.append(f"{where} has the unknown key '{key}'")
            else:
                errors += check_shape(value[key], props[key], f"{where}.{key}")
    elif kind == "array":
        for n, item in enumerate(value):
            errors += check_shape(item, schema["items"], f"{where}[{n}]")
    elif kind == "string":
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{where} must be one of {', '.join(schema['enum'])}, not '{value}'")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where} is {len(value)} characters; the limit is {schema['maxLength']}")
    return errors


def _name(value):
    return {dict: "an object", list: "an array", str: "a string", bool: "a boolean",
            type(None): "null"}.get(type(value), "a number")


def check_produce(answer, needing_response=()):
    """Shape, then meaning: `responses` must cover exactly the findings that the attempt's feedback
    listed as needing a response, each once."""
    errors = check_shape(answer, PRODUCE)
    if errors:
        return errors
    if answer["outcome"] == "blocked" and not answer["blocked_reason"].strip():
        errors.append("answer.outcome is 'blocked' but blocked_reason is empty")
    if answer["outcome"] == "blocked":
        return errors                                   # a blocked author owes no responses
    given = [r["finding"] for r in answer["responses"]]
    needed = list(needing_response)
    for fid in needed:
        if given.count(fid) == 0:
            errors.append(f"answer.responses has no entry for finding {fid}")
        elif given.count(fid) > 1:
            errors.append(f"answer.responses answers finding {fid} more than once")
    for r in answer["responses"]:
        if r["action"] == "disputed" and not r["note"].strip():
            errors.append(f"answer.responses disputes {r['finding']} without a note saying why")
    for fid in sorted(set(given) - set(needed)):
        errors.append(f"answer.responses answers {fid}, which was not listed as needing a response")
    return errors


def check_review(answer, ledger_view=None):
    """Validate the wire shape. findings.apply_review validates ledger semantics atomically."""
    return check_shape(answer, REVIEW)
