---
name: data-validator
description: Validate JSON/CSV theo schema, type và format.
version: 1.0.0
author: boom-overnight
license: MIT
platforms: [linux, macos]
tags: [validation, json, csv, schema]
category: data
---

# Data Validator

Kiểm tra dữ liệu JSON/CSV trước khi đưa vào pipeline:

- `validate_json(data, schema)` — kiểm tra dict theo schema: type của từng
  field, field bắt buộc (`required`), format (`email`, `url`, `date`).
- `validate_csv(path, columns)` — kiểm tra file CSV có đủ cột yêu cầu và
  mỗi dòng đủ số cột không.

## Cách dùng

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

Kết quả luôn có dạng `{"valid": bool, "errors": [...]}` — mỗi error gồm
`field`, `message` để dễ hiển thị trong `web UI` hoặc log. Schema dùng
pydantic để ép type nghiêm ngặt (`StrictInt`, `StrictStr`...).
