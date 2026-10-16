---
name: api-designer
description: "Design REST APIs from a description: generate OpenAPI 3.0 specs (paths, schemas, errors) + FastAPI/Flask route stubs, with REST-standard validation."
version: 1.0.0
author: Boom
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [api, openapi, rest, fastapi, flask, design]
    related_skills: [software-development/test-driven-development]
---

# API-Designer — Generate OpenAPI Spec + Route Stubs

## Name
`api-designer`

## Description
Takes resource descriptions (names + fields) → generates a complete **OpenAPI 3.0** specification: CRUD paths, schemas, standard error responses; also generates **route stubs** for FastAPI and Flask so developers only need to fill in the logic. Includes an automatic **validation** step:

- resource names must be plural nouns with valid kebab-case/plural form
- standard REST methods (GET/POST/PUT/PATCH/DELETE); never use GET to mutate
- status codes follow convention (201 for create, 204 for delete, 404/422 for errors)
- list endpoints **must** have pagination (`page`/`page_size`) and an envelope `{items, total, page, page_size}`

## When to use
- Starting a new service/API and need a spec before coding
- Need OpenAPI documentation to import into Swagger UI / Postman
- Want consistent route stubs across services

## Input
- `title` (str): API name
- `version` (str): API version, default `"1.0.0"`
- `resources` (list): each resource is a dict `{name, fields: {field: type}, description?}`. Supported types: `string`, `integer`, `number`, `boolean`, `date`, `datetime`, `uuid`, `email`, `array[<type>]`
- `framework` ("fastapi" | "flask", default `"fastapi"`): framework for the route stubs

## Output
Dict:
- `openapi`: OpenAPI 3.0 spec dict (can be dumped to JSON/YAML)
- `validation`: list of `{rule, status: pass|fail, detail}`
- `stubs`: Python route stub code for the chosen framework

## Example

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
print(result["stubs"])   # paste into your project, fill in the logic
```
