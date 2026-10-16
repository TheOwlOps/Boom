"""Sinh OpenAPI 3.0 spec + route stubs từ mô tả resources (skill api-designer).

Luồng:
  1. design_api(title, resources, ...) -> validate input
  2. build_openapi(...)                -> dict spec OpenAPI 3.0
  3. validate_design(...)              -> list {rule, status, detail}
  4. render_stubs(..., framework)      -> code FastAPI hoặc Flask

Quy ước REST được enforce:
  - resource name: danh từ số nhiều, chữ thường, gạch ngang (vd 'products')
  - CRUD chuẩn: GET list (có pagination), POST create, GET one, PUT/PATCH
    update, DELETE (204, không body)
  - status code: 200 list/get, 201 create, 200 update, 204 delete,
    400/404/422 cho lỗi
  - endpoint list BẮT BUỘC có page/page_size + envelope
    {items, total, page, page_size}

Chỉ dùng stdlib.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Ánh xạ type mô tả -> OpenAPI schema
# ---------------------------------------------------------------------------

# type người dùng khai báo -> (openapi type, format)
_TYPE_MAP = {
    "string": ("string", None),
    "integer": ("integer", "int32"),
    "number": ("number", "double"),
    "boolean": ("boolean", None),
    "date": ("string", "date"),
    "datetime": ("string", "date-time"),
    "uuid": ("string", "uuid"),
    "email": ("string", "email"),
}

_METHODS_HOP_LE = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _schema_cua(type_str):
    """Chuyển type mô tả (kể cả array[x]) thành OpenAPI schema dict."""
    m = re.fullmatch(r"array\[(.+)\]", type_str.strip())
    if m:
        return {"type": "array", "items": _schema_cua(m.group(1))}
    base, fmt = _TYPE_MAP.get(type_str.strip().lower(), ("string", None))
    schema = {"type": base}
    if fmt:
        schema["format"] = fmt
    return schema


def _ten_resource_hop_le(name):
    """Danh từ số nhiều, chữ thường, cho phép gạch ngang."""
    return bool(re.fullmatch(r"[a-z][a-z0-9-]*s", name or ""))


def _ten_schema(resource_name):
    """'products' -> 'Product' (tên schema số ít, viết hoa chữ đầu)."""
    don = resource_name[:-1] if resource_name.endswith("s") else resource_name
    return "".join(part.capitalize() for part in don.split("-"))


# ---------------------------------------------------------------------------
# Validate input
# ---------------------------------------------------------------------------

def validate_resources(resources):
    """Validate danh sách resources. Trả về (errors, resources_chuan_hoa).

    errors: list chuỗi mô tả lỗi; rỗng nghĩa là input hợp lệ.
    """
    errors = []
    if not resources:
        return ["resources rỗng — cần ít nhất 1 resource"], []
    thay = set()
    chuan = []
    for i, r in enumerate(resources):
        ten = (r.get("name") or "").strip()
        if not ten:
            errors.append("resource[%d]: thiếu name" % i)
            continue
        if not _ten_resource_hop_le(ten):
            errors.append(
                "resource '%s': tên phải là danh từ số nhiều chữ thường "
                "(vd 'products', không phải 'Product' hay 'product')" % ten)
        if ten in thay:
            errors.append("resource '%s': tên bị trùng" % ten)
        thay.add(ten)
        fields = r.get("fields") or {}
        if not fields:
            errors.append("resource '%s': cần ít nhất 1 field" % ten)
        for fname, ftype in fields.items():
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", fname):
                errors.append("resource '%s': tên field '%s' không hợp lệ"
                              % (ten, fname))
            if not re.fullmatch(r"array\[[a-z]+\]", str(ftype).strip()) \
                    and str(ftype).strip().lower() not in _TYPE_MAP:
                errors.append("resource '%s': type '%s' của field '%s' "
                              "không được hỗ trợ" % (ten, ftype, fname))
        chuan.append({"name": ten, "fields": dict(fields),
                      "description": r.get("description", "")})
    return errors, chuan


# ---------------------------------------------------------------------------
# Sinh OpenAPI 3.0
# ---------------------------------------------------------------------------

def _error_responses():
    """Error responses dùng chung cho mọi endpoint."""
    return {
        "400": {"description": "Bad request — dữ liệu gửi lên không hợp lệ",
                "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Error"}}}},
        "404": {"description": "Not found — resource không tồn tại",
                "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Error"}}}},
        "422": {"description": "Validation error — sai định dạng field",
                "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Error"}}}},
    }


def _pagination_params():
    return [
        {"name": "page", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1, "default": 1},
         "description": "Trang hiện tại (bắt đầu từ 1)"},
        {"name": "page_size", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1, "maximum": 100,
                    "default": 20},
         "description": "Số item mỗi trang (tối đa 100)"},
    ]


def build_openapi(title, resources, version="1.0.0"):
    """Dựng dict spec OpenAPI 3.0 từ resources đã validate."""
    paths = {}
    schemas = {
        "Error": {
            "type": "object",
            "required": ["code", "message"],
            "properties": {
                "code": {"type": "string",
                         "description": "Mã lỗi nội bộ, vd 'NOT_FOUND'"},
                "message": {"type": "string",
                            "description": "Mô tả lỗi cho người dùng"},
                "details": {"type": "object",
                            "description": "Chi tiết thêm (optional)"},
            },
        },
        "PaginatedList": {
            "type": "object",
            "required": ["items", "total", "page", "page_size"],
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
                "page": {"type": "integer"},
                "page_size": {"type": "integer"},
            },
            "description": "Envelope chuẩn cho mọi endpoint list",
        },
    }

    for r in resources:
        ten = r["name"]
        schema_name = _ten_schema(ten)
        # Schema của resource: id tự sinh + các field
        props = {"id": {"type": "string", "format": "uuid",
                        "description": "ID tự sinh, không gửi khi create"}}
        required = []
        for fname, ftype in r["fields"].items():
            props[fname] = _schema_cua(ftype)
            required.append(fname)
        schemas[schema_name] = {
            "type": "object",
            "required": ["id"] + required,
            "properties": props,
            "description": r["description"] or "Resource %s" % ten,
        }
        # Schema cho create: bỏ id
        schemas[schema_name + "Create"] = {
            "type": "object",
            "required": required,
            "properties": {k: v for k, v in props.items() if k != "id"},
        }

        base = "/" + ten
        item = base + "/{id}"
        ref = "#/components/schemas/" + schema_name
        ref_create = "#/components/schemas/" + schema_name + "Create"

        paths[base] = {
            "get": {  # LIST — bắt buộc pagination
                "summary": "Liệt kê %s (có phân trang)" % ten,
                "parameters": _pagination_params(),
                "responses": {
                    "200": {"description": "Danh sách %s" % ten,
                            "content": {"application/json": {
                                "schema": {
                                    "allOf": [
                                        {"$ref": "#/components/schemas/PaginatedList"},
                                    ],
                                }}}},
                },
            },
            "post": {  # CREATE — 201
                "summary": "Tạo %s mới" % schema_name.lower(),
                "requestBody": {"required": True,
                               "content": {"application/json": {
                                   "schema": {"$ref": ref_create}}}},
                "responses": {
                    "201": {"description": "%s đã tạo" % schema_name,
                            "content": {"application/json": {
                                "schema": {"$ref": ref}}}},
                    **_error_responses(),
                },
            },
        }
        paths[item] = {
            "get": {
                "summary": "Lấy 1 %s theo id" % schema_name.lower(),
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "string", "format": "uuid"}}],
                "responses": {
                    "200": {"description": schema_name,
                            "content": {"application/json": {
                                "schema": {"$ref": ref}}}},
                    "404": _error_responses()["404"],
                },
            },
            "put": {
                "summary": "Cập nhật toàn bộ %s" % schema_name.lower(),
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "string", "format": "uuid"}}],
                "requestBody": {"required": True,
                               "content": {"application/json": {
                                   "schema": {"$ref": ref_create}}}},
                "responses": {
                    "200": {"description": schema_name + " đã cập nhật",
                            "content": {"application/json": {
                                "schema": {"$ref": ref}}}},
                    **_error_responses(),
                },
            },
            "delete": {  # DELETE — 204, không body
                "summary": "Xóa %s" % schema_name.lower(),
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "string", "format": "uuid"}}],
                "responses": {
                    "204": {"description": "Đã xóa, không trả body"},
                    "404": _error_responses()["404"],
                },
            },
        }

    return {
        "openapi": "3.0.3",
        "info": {"title": title, "version": version,
                 "description": "API spec sinh tự động bởi skill api-designer"},
        "paths": paths,
        "components": {"schemas": schemas},
    }


# ---------------------------------------------------------------------------
# Validate thiết kế
# ---------------------------------------------------------------------------

def validate_design(spec):
    """Kiểm tra spec theo quy ước REST. Trả về list {rule, status, detail}."""
    ket_qua = []

    def _ghi(rule, ok, detail):
        ket_qua.append({"rule": rule, "status": "pass" if ok else "fail",
                        "detail": detail})

    paths = spec.get("paths", {})

    # 1) Method chuẩn REST
    methods_la = set()
    for path, ops in paths.items():
        for m in ops:
            methods_la.add(m.upper())
    la = methods_la - _METHODS_HOP_LE
    _ghi("rest_methods", not la,
         "methods dùng: %s" % sorted(methods_la) if not la
         else "method không chuẩn REST: %s" % sorted(la))

    # 2) Endpoint list phải có pagination
    thieu_page = []
    for path, ops in paths.items():
        if "{" not in path and "get" in ops:  # collection endpoint
            params = {p["name"] for p in ops["get"].get("parameters", [])}
            if not {"page", "page_size"} <= params:
                thieu_page.append(path)
    _ghi("list_pagination", not thieu_page,
         "mọi endpoint list đều có page/page_size" if not thieu_page
         else "thiếu pagination ở: %s" % thieu_page)

    # 3) Status code đúng quy ước
    loi_code = []
    for path, ops in paths.items():
        if "post" in ops and "201" not in ops["post"].get("responses", {}):
            loi_code.append("%s POST thiếu 201" % path)
        if "delete" in ops and "204" not in ops["delete"].get("responses", {}):
            loi_code.append("%s DELETE thiếu 204" % path)
    _ghi("status_codes", not loi_code,
         "201 cho create, 204 cho delete — đúng quy ước" if not loi_code
         else "; ".join(loi_code))

    # 4) Error schema thống nhất
    schemas = spec.get("components", {}).get("schemas", {})
    _ghi("error_schema",
         "Error" in schemas
         and set(schemas["Error"].get("required", [])) >= {"code", "message"},
         "schema Error chuẩn {code, message, details?}" if "Error" in schemas
         else "thiếu schema Error dùng chung")

    # 5) Tên path hợp lệ (số nhiều, chữ thường)
    loi_ten = [p for p in paths
               if not re.fullmatch(r"/[a-z][a-z0-9-]*s(/\{id\})?", p)]
    _ghi("resource_naming", not loi_ten,
         "tên path đều là danh từ số nhiều chữ thường" if not loi_ten
         else "tên path sai quy ước: %s" % loi_ten)

    return ket_qua


# ---------------------------------------------------------------------------
# Route stubs
# ---------------------------------------------------------------------------

def render_stubs(resources, framework="fastapi"):
    """Sinh code route stubs. framework: 'fastapi' | 'flask'."""
    if framework not in ("fastapi", "flask"):
        raise ValueError("framework phải là 'fastapi' hoặc 'flask'")
    if framework == "fastapi":
        return _stubs_fastapi(resources)
    return _stubs_flask(resources)


def _stubs_fastapi(resources):
    dong = ['"""Route stubs sinh bởi skill api-designer (FastAPI).',
            'Điền logic vào chỗ TODO.',
            '"""',
            "",
            "from fastapi import APIRouter, HTTPException, Query, status",
            "from pydantic import BaseModel",
            "from typing import List, Optional",
            "from uuid import UUID",
            "",
            "router = APIRouter()",
            ""]
    for r in resources:
        ten = r["name"]
        schema = _ten_schema(ten)
        dong += [
            "",
            "# ---- %s ----" % ten,
            "class %sCreate(BaseModel):" % schema,
        ]
        for fname, ftype in r["fields"].items():
            dong.append("    %s: %s  # %s" % (fname, _py_type(ftype), ftype))
        dong += [
            "",
            "class %s(%sCreate):" % (schema, schema),
            "    id: UUID",
            "",
            "@router.get('/%s')" % ten,
            "def list_%s(page: int = Query(1, ge=1)," % ten,
            "              page_size: int = Query(20, ge=1, le=100)):",
            "    \"\"\"Liệt kê %s — TODO: query DB có phân trang.\"\"\"" % ten,
            "    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)",
            "",
            "@router.post('/%s', status_code=status.HTTP_201_CREATED)" % ten,
            "def create_%s(payload: %sCreate):" % (schema.lower(), schema),
            "    \"\"\"Tạo %s mới — TODO: insert DB, trả 201.\"\"\"" % schema.lower(),
            "    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)",
            "",
            "@router.get('/%s/{item_id}')" % ten,
            "def get_%s(item_id: UUID):" % schema.lower(),
            "    \"\"\"TODO: get theo id, không thấy -> 404.\"\"\"",
            "    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)",
            "",
            "@router.put('/%s/{item_id}')" % ten,
            "def update_%s(item_id: UUID, payload: %sCreate):"
            % (schema.lower(), schema),
            "    \"\"\"TODO: update toàn bộ, không thấy -> 404.\"\"\"",
            "    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)",
            "",
            "@router.delete('/%s/{item_id}',"
            " status_code=status.HTTP_204_NO_CONTENT)" % ten,
            "def delete_%s(item_id: UUID):" % schema.lower(),
            "    \"\"\"TODO: xóa, trả 204 không body.\"\"\"",
            "    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)",
        ]
    return "\n".join(dong) + "\n"


def _stubs_flask(resources):
    dong = ['"""Route stubs sinh bởi skill api-designer (Flask).',
            'Điền logic vào chỗ TODO.',
            '"""',
            "",
            "from flask import Blueprint, jsonify, request",
            "",
            "bp = Blueprint('api', __name__)",
            ""]
    for r in resources:
        ten = r["name"]
        schema = _ten_schema(ten)
        dong += [
            "",
            "# ---- %s ----" % ten,
            "@bp.get('/%s')" % ten,
            "def list_%s():" % ten,
            "    \"\"\"Liệt kê %s — TODO: query DB có phân trang.\"\"\"" % ten,
            "    page = request.args.get('page', 1, type=int)",
            "    page_size = min(request.args.get('page_size', 20, type=int), 100)",
            "    return jsonify(items=[], total=0, page=page, page_size=page_size)",
            "",
            "@bp.post('/%s')" % ten,
            "def create_%s():" % schema.lower(),
            "    \"\"\"Tạo %s mới — TODO: validate + insert DB, trả 201.\"\"\""
            % schema.lower(),
            "    return jsonify({}), 201",
            "",
            "@bp.get('/%s/<item_id>')" % ten,
            "def get_%s(item_id):" % schema.lower(),
            "    \"\"\"TODO: get theo id, không thấy -> 404.\"\"\"",
            "    return jsonify(code='NOT_FOUND', message='not found'), 404",
            "",
            "@bp.put('/%s/<item_id>')" % ten,
            "def update_%s(item_id):" % schema.lower(),
            "    \"\"\"TODO: update toàn bộ, không thấy -> 404.\"\"\"",
            "    return jsonify({}), 200",
            "",
            "@bp.delete('/%s/<item_id>')" % ten,
            "def delete_%s(item_id):" % schema.lower(),
            "    \"\"\"TODO: xóa, trả 204 không body.\"\"\"",
            "    return '', 204",
        ]
    return "\n".join(dong) + "\n"


def _py_type(type_str):
    """Type mô tả -> annotation Python cho stub."""
    m = re.fullmatch(r"array\[(.+)\]", type_str.strip())
    if m:
        return "List[%s]" % _py_type(m.group(1))
    return {"string": "str", "integer": "int", "number": "float",
            "boolean": "bool", "date": "str", "datetime": "str",
            "uuid": "UUID", "email": "str"}.get(type_str.strip().lower(), "str")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def design_api(title, resources, version="1.0.0", framework="fastapi"):
    """Thiết kế API trọn gói: validate -> spec -> validate design -> stubs.

    Trả về {openapi, validation, stubs}. Raise ValueError nếu input sai.
    """
    errors, chuan = validate_resources(resources)
    if errors:
        raise ValueError("input không hợp lệ:\n- " + "\n- ".join(errors))
    spec = build_openapi(title, chuan, version)
    return {
        "openapi": spec,
        "validation": validate_design(spec),
        "stubs": render_stubs(chuan, framework),
    }


if __name__ == "__main__":
    import json
    demo = design_api("Demo API", [
        {"name": "products",
         "fields": {"name": "string", "price": "number",
                    "in_stock": "boolean"}},
    ])
    print(json.dumps(demo["openapi"], ensure_ascii=False, indent=2)[:1500])
    print("...")
    print(json.dumps(demo["validation"], ensure_ascii=False, indent=2))
