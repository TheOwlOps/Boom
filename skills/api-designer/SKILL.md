---
name: api-designer
description: "Thiết kế REST API từ mô tả: sinh OpenAPI 3.0 spec (paths, schemas, errors) + route stubs FastAPI/Flask, có validate chuẩn REST."
version: 1.0.0
author: Boom
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [api, openapi, rest, fastapi, flask, design]
    related_skills: [software-development/test-driven-development]
---

# API-Designer — Sinh OpenAPI Spec + Route Stubs

## Tên
`api-designer`

## Mô tả
Nhận mô tả resources (tên + fields) → sinh đặc tả **OpenAPI 3.0** đầy đủ: paths CRUD, schemas, error responses chuẩn; đồng thời sinh **route stubs** cho FastAPI và Flask để dev chỉ việc điền logic. Có bước **validate** tự động:

- resource name phải là danh từ số nhiều, kebab-case/plural hợp lệ
- method chuẩn REST (GET/POST/PUT/PATCH/DELETE), không dùng GET để mutate
- status code đúng quy ước (201 cho create, 204 cho delete, 404/422 cho lỗi)
- endpoint list **bắt buộc** có pagination (`page`/`page_size`) và envelope `{items, total, page, page_size}`

## Khi nào dùng
- Bắt đầu service/API mới, cần spec trước khi code
- Cần tài liệu OpenAPI để import vào Swagger UI / Postman
- Muốn route stubs nhất quán giữa các service

## Input
- `title` (str): tên API
- `version` (str): version API, mặc định `"1.0.0"`
- `resources` (list): mỗi resource là dict `{name, fields: {field: type}, description?}`. Type hỗ trợ: `string`, `integer`, `number`, `boolean`, `date`, `datetime`, `uuid`, `email`, `array[<type>]`
- `framework` ("fastapi" | "flask", mặc định `"fastapi"`): framework cho route stubs

## Output
Dict:
- `openapi`: dict spec OpenAPI 3.0 (dump ra JSON/YAML được)
- `validation`: list `{rule, status: pass|fail, detail}`
- `stubs`: code Python route stubs cho framework đã chọn

## Ví dụ

```python
from api_designer.design import design_api

result = design_api(
    title="Shop API",
    resources=[
        {"name": "products",
         "fields": {"name": "string", "price": "number", "in_stock": "boolean"}},
        {"name": "orders",
         "fields": {"product_id": "uuid", "quantity": "integer"}},
    ],
    framework="fastapi",
)
assert all(v["status"] == "pass" for v in result["validation"])
print(result["stubs"])   # paste vào project, điền logic
```
