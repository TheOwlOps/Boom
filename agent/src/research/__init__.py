# -*- coding: utf-8 -*-
"""Package nghiên cứu sâu (DeepSearch + DeepThink) của Boom.

Module lõi:
  - ``research.deepsearch``: lớp ``DeepSearch`` — tìm kiếm đa bước
    (multi-hop), trích thực thể, sinh câu hỏi follow-up, cross-reference
    giữa các nguồn.
  - ``research.deepthink``: lớp ``DeepThink`` — phân tích kết quả nghiên
    cứu thành tóm tắt, nhiều góc nhìn (perspectives) và khoảng trống
    thông tin (gaps).
  - ``research.crossref``: hàm ``cross_reference`` — đối chiếu các nguồn,
    tính độ đồng thuận/xung đột bằng overlap từ (chưa dùng embedding).

Import được bọc try/except: nếu một module lõi chưa có trên máy (ví dụ
mới chỉ triển khai một phần), package vẫn import được và đặt symbol đó
thành ``None``. Code gọi nên kiểm tra ``is None`` trước khi dùng.
"""

__all__ = ["DeepSearch", "DeepThink", "cross_reference"]

DeepSearch = None  # type: ignore[assignment]
DeepThink = None  # type: ignore[assignment]
cross_reference = None  # type: ignore[assignment]

try:  # Không crash nếu module lõi chưa được triển khai
    from .deepsearch import DeepSearch  # noqa: F401,E402
except ImportError:
    pass

try:
    from .deepthink import DeepThink  # noqa: F401,E402
except ImportError:
    pass

try:
    from .crossref import cross_reference  # noqa: F401,E402
except ImportError:
    pass


def available() -> dict:
    """Trả về trạng thái sẵn sàng của từng thành phần lõi."""
    return {
        "DeepSearch": DeepSearch is not None,
        "DeepThink": DeepThink is not None,
        "cross_reference": cross_reference is not None,
    }
