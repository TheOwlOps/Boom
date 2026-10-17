---
name: data-validator
description: Validate JSON/CSV against schema, types, and formats.
version: 1.0.0
author: boom-overnight
license: MIT
platforms: [linux, macos]
tags: [validation, json, csv, schema]
category: data
---

# Data Validator

Validate JSON/CSV data before feeding it into a pipeline:

- `validate_json(data, schema)` — check a dict against a schema: per-field
  type, required fields (`required`), formats (`email`, `url`, `date`).
- `validate_csv(path, columns)` — check that a CSV file has the required
  columns and each row has enough columns.

## Usage

```python
from validator import validate_json, validate_csv

schema = {
    "name": {"type": "str", "required": True},
    "email": {"type": "str", "required": True, "format": "email"},
    "age": {"type": "int", "required": False},
}
result = validate_json({"name": "Son", "email": "a@b.com"}, schema)
# {"valid": True, "errors": []}

result = validate_csv("/data/users.csv", columns=["name", "email", "age"])
```

Results always have the shape `{"valid": bool, "errors": [...]}` — each error
carries a `field` and a `message` for easy display in a `web UI` or logs. The
schema uses pydantic to strictly coerce types (`StrictInt`, `StrictStr`...).
