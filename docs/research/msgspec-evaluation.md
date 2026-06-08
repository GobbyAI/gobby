# msgspec Evaluation for LLM Response Validation

**Task:** gt-575bca
**Date:** 2026-01-07
**Status:** ✅ Recommended for adoption

## Executive Summary

msgspec (v0.20.0) is recommended for replacing manual JSON parsing in LLM response handling. Testing confirms it integrates cleanly with our `extract_json_from_text()` utility and provides significant boilerplate reduction.

## Test Results

### 1. Basic Struct with Enums
```python
class IssueType(str, Enum):
    TEST_FAILURE = "test_failure"
    LINT_ERROR = "lint_error"

class Issue(msgspec.Struct):
    type: IssueType
    severity: IssueSeverity
    title: str
    location: str | None = None
    recurring_count: int = 0
```
**Result:** ✅ Enums parsed directly, optional fields work correctly

### 2. Error Message Quality

| Scenario | Error Message |
|----------|---------------|
| Invalid enum | `Invalid enum value 'invalid_type' - at $.issues[0].type` |
| Missing field | `Object missing required field 'title' - at $.issues[0]` |
| Wrong type | `Expected 'str', got 'int' - at $.issues[0].title` |

**Result:** ✅ Clear errors with JSON path for debugging

### 3. Type Coercion (LLM Quirks)

LLMs sometimes return `"5"` instead of `5`. With `strict=False`:
- `"5"` → `5` (int) ✅
- `"3.14"` → `3.14` (float) ✅

**Result:** ✅ Use `strict=False` for LLM responses

### 4. Integration with extract_json_from_text()

```python
llm_response = '''Here are the issues:
```json
{"issues": [{"type": "test_failure", "severity": "major", "title": "Test failed"}]}
```
'''

json_str = extract_json_from_text(llm_response)
result = msgspec.json.decode(json_str, type=ValidationResponse, strict=False)
```
**Result:** ✅ Two-step extraction + decode works perfectly

### 5. Nested Backticks (Recent Bug Fix)

JSON with backticks inside strings:
```json
{"description": "Output like:\n```\nresult\n```"}
```
**Result:** ✅ Works correctly with our fixed extract_json_from_text()

## File Coverage Analysis

### validation_models.py
- **Current:** 90 lines with manual `to_dict()`/`from_dict()`
- **With msgspec:** ~35 lines (Struct handles serialization)
- **Reduction:** 60%

### issue_extraction.py
- **Current:** 140 lines of manual field validation
- **With msgspec:** ~30 lines (one decode call)
- **Reduction:** 80%

### expansion.py
- **Current:** 50 lines of SubtaskSpec parsing
- **With msgspec:** ~15 lines
- **Reduction:** 70%

### external_validator.py
- **Current:** 60 lines parsing ExternalValidationResult
- **With msgspec:** ~20 lines
- **Reduction:** 65%

### spec_parser.py
- **Current:** 5 dataclasses with manual parsing
- **With msgspec:** 5 Structs with automatic parsing
- **Reduction:** 50%

## Compatibility Assessment

### Pydantic Coexistence
- **Config models:** Keep Pydantic (complex validation, env vars, YAML)
- **LLM responses:** Use msgspec (simple schemas, performance)
- **Result:** ✅ No conflicts, different use cases

### Migration Complexity
- Structs are similar to dataclasses
- Replace `@dataclass` with `class X(msgspec.Struct)`
- Remove manual `to_dict()`/`from_dict()` methods
- Replace parsing loops with single `decode()` call
- **Result:** ✅ Low complexity, incremental migration possible

## Implementation (Completed)

### Helper Function
Added to `gobby/utils/json_helpers.py`:
```python
def decode_llm_response(
    text: str,
    response_type: type[T],
    *,
    strict: bool = False,  # Default False for LLM responses (type coercion)
) -> T | None:
    """Extract JSON from LLM response and decode to typed struct.

    Args:
        text: Raw LLM response text that may contain JSON
        response_type: The msgspec Struct type to decode into
        strict: Controls type coercion in msgspec.json.decode():
            - strict=False (default): Allows type coercion. Useful for noisy
              LLM outputs where the model might return "3" instead of 3, or
              "true" instead of true. This is the recommended setting for LLM
              responses since models frequently return slightly mistyped JSON.
            - strict=True: Enforces exact types. Safer for internal or
              pre-validated data. A string "123" won't coerce to int 123.

    Returns:
        Decoded struct instance, or None if extraction/validation fails.

    Note:
        This helper defaults to strict=False for LLM responses, which is the
        recommended setting for raw LLM output to tolerate common JSON quirks.
        Pass strict=True only when processing pre-validated data where you want
        fail-fast behavior on type mismatches. The underlying msgspec.json.decode()
        call respects this flag.
    """
    json_str = extract_json_from_text(text)
    if json_str is None:
        return None
    try:
        return msgspec.json.decode(json_str.encode(), type=response_type, strict=strict)
    except msgspec.ValidationError as e:
        logger.warning(f"Invalid LLM response structure: {e}")
        return None
```

### Configuration
Strictness is now caller policy rather than provider config. Workflows can set
`llm_json_strict`; otherwise callers pass the default they want for the response
shape.

### Usage Pattern
Callers look up config/workflow variable and pass explicit `strict` value:
```python
# Get strict mode from workflow variables, defaulting permissive for LLM output
strict = workflow_state.variables.get("llm_json_strict", False)
result = decode_llm_response(llm_text, MyResponseType, strict=strict)
```

## Decision

**✅ ADOPT msgspec for LLM response validation**

### Rationale
1. 60-80% reduction in parsing boilerplate
2. Clear error messages with JSON paths
3. Configurable strict mode (default True, override per-workflow)
4. Integrates cleanly with existing code
5. No conflicts with Pydantic config

### Migration Priority

To generate tasks for this migration, create a spec file and use:
```bash
gobby tasks parse-spec docs/plans/msgspec-migration.md
```

Priority order:
1. `validation_models.py` + `issue_extraction.py` (highest impact)
2. `expansion.py` (SubtaskSpec)
3. `external_validator.py`
4. `spec_parser.py` (lowest priority, most complex)
