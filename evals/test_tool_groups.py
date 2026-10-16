"""Test cho tool_groups.py — bản tách của AgentEnvironment._init_tools.

Chạy: python3 test_tool_groups.py
Yêu cầu: repo read-only tại /tmp/boom_git (commit bf171b9), Python stdlib + pydantic.
"""
import sys
import tempfile
import unittest
from pathlib import Path

# Repo gốc (read-only) + thư mục chứa tool_groups.py
sys.path.insert(0, "/tmp/boom_git")
sys.path.insert(0, "/tmp/boom_git/agent/src")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from environment import AgentEnvironment  # noqa: E402
from tool_groups import (  # noqa: E402
    _init_filesystem_tools,
    _init_terminal_tools,
    _init_git_tools,
    _init_web_tools,
    _init_code_tools,
    _init_security_tools,
    _init_db_tools,
    init_all_tool_groups,
)

# Số tool kỳ vọng mỗi group, đếm trên env thật tại commit bf171b9.
EXPECTED_COUNTS = {
    "_init_filesystem_tools": 13,
    "_init_terminal_tools": 11,
    "_init_git_tools": 6,
    "_init_web_tools": 25,
    "_init_code_tools": 15,
    "_init_security_tools": 45,
    "_init_db_tools": 44,
}
EXPECTED_TOTAL = sum(EXPECTED_COUNTS.values())  # 159


class TestToolGroups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Dựng một AgentEnvironment thật trên thư mục tạm (repo không bị đụng)."""
        tmp = Path(tempfile.mkdtemp(prefix="boom_toolgroups_"))
        cls.env = AgentEnvironment(root_dir=tmp, db_path=tmp / "state.db")
        cls.original = cls.env.get_tools()
        cls.groups = {
            "_init_filesystem_tools": _init_filesystem_tools(cls.env),
            "_init_terminal_tools": _init_terminal_tools(cls.env),
            "_init_git_tools": _init_git_tools(cls.env),
            "_init_web_tools": _init_web_tools(cls.env),
            "_init_code_tools": _init_code_tools(cls.env),
            "_init_security_tools": _init_security_tools(cls.env),
            "_init_db_tools": _init_db_tools(cls.env),
        }
        cls.merged = init_all_tool_groups(cls.env)

    def test_01_functions_exist_and_callable(self):
        """7 hàm group tồn tại và gọi được."""
        assert callable(_init_filesystem_tools)
        assert callable(_init_terminal_tools)
        assert callable(_init_git_tools)
        assert callable(_init_web_tools)
        assert callable(_init_code_tools)
        assert callable(_init_security_tools)
        assert callable(_init_db_tools)

    def test_02_each_returns_nonempty_dict(self):
        """Mỗi hàm trả về dict non-empty."""
        for name, tools in self.groups.items():
            assert isinstance(tools, dict), f"{name} không trả về dict"
            assert len(tools) > 0, f"{name} trả về dict rỗng"

    def test_03_expected_counts_per_group(self):
        """Số tool mỗi group đúng như đếm trên bản gốc."""
        for name, expected in EXPECTED_COUNTS.items():
            actual = len(self.groups[name])
            assert actual == expected, f"{name}: kỳ vọng {expected}, thực tế {actual}"

    def test_04_total_equals_original(self):
        """Tổng tools của 7 group == số tools của _init_tools gốc."""
        total = sum(len(t) for t in self.groups.values())
        assert total == EXPECTED_TOTAL, f"tổng {total} != kỳ vọng {EXPECTED_TOTAL}"
        assert total == len(self.original), (
            f"tổng group {total} != _init_tools gốc {len(self.original)}"
        )

    def test_05_no_duplicate_names_across_groups(self):
        """Không trùng tên tool giữa các group."""
        flat = [n for tools in self.groups.values() for n in tools]
        assert len(flat) == len(set(flat)), "trùng tên tool giữa các group!"

    def test_06_names_are_valid_strings(self):
        """Tên tool là string hợp lệ, non-empty, không khoảng trắng."""
        for name, tools in self.groups.items():
            for tool_name in tools:
                assert isinstance(tool_name, str), f"{name}: tên tool không phải str"
                assert tool_name.strip() != "", f"{name}: tên tool rỗng"
                assert " " not in tool_name, f"{name}: tên tool chứa khoảng trắng: {tool_name}"

    def test_07_spec_shape(self):
        """Mỗi spec có đủ name/description/schema/func; name khớp key."""
        for group_name, tools in self.groups.items():
            for tool_name, spec in tools.items():
                assert isinstance(spec, dict), f"{tool_name}: spec không phải dict"
                assert spec.get("name") == tool_name, f"{tool_name}: spec['name'] không khớp key"
                assert isinstance(spec.get("description"), str) and spec["description"], (
                    f"{tool_name}: thiếu description"
                )
                assert isinstance(spec.get("schema"), dict), f"{tool_name}: thiếu schema"
                assert callable(spec.get("func")), f"{tool_name}: func không callable"

    def test_08_spot_check_group_membership(self):
        """Spot-check: tool nằm đúng group của nó."""
        assert "read_file" in self.groups["_init_filesystem_tools"]
        assert "shell_run" in self.groups["_init_terminal_tools"]
        assert "git_status" in self.groups["_init_git_tools"]
        assert "web_search" in self.groups["_init_web_tools"]
        assert "code_review" in self.groups["_init_code_tools"]
        assert "secret_scan" in self.groups["_init_security_tools"]
        assert "db_query" in self.groups["_init_db_tools"]

    def test_09_merged_names_match_original(self):
        """init_all_tool_groups merge ra đúng tập tên của _init_tools gốc."""
        assert set(self.merged.keys()) == set(self.original.keys()), (
            "tập tên tool sau merge khác bản gốc: "
            f"thiếu={sorted(set(self.original) - set(self.merged))}, "
            f"thừa={sorted(set(self.merged) - set(self.original))}"
        )

    def test_10_merged_description_schema_match_original(self):
        """description + schema sau merge phải y hệt bản gốc từng tool."""
        for tool_name, orig_spec in self.original.items():
            merged_spec = self.merged[tool_name]
            assert merged_spec["description"] == orig_spec["description"], (
                f"{tool_name}: description khác bản gốc"
            )
            assert merged_spec["schema"] == orig_spec["schema"], (
                f"{tool_name}: schema khác bản gốc"
            )

    def test_11_overwrite_precedence_preserved(self):
        """Các ghi đè có chủ đích trong bản gốc được giữ (legacy thắng factory)."""
        # web_search legacy (DuckDuckGo) thắng factory tools.web/tools.web_tools
        assert "DuckDuckGo" in self.groups["_init_web_tools"]["web_search"]["description"]
        # secret_scan legacy (Previews are masked) thắng factory tools.security
        assert "masked" in self.groups["_init_security_tools"]["secret_scan"]["description"]
        # code_review legacy thắng factory tools.code: legacy description ends with
        # "Returns a Markdown report." while the factory one says
        # "Returns issues + severity summary."
        assert "Markdown report" in self.groups["_init_code_tools"]["code_review"]["description"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
