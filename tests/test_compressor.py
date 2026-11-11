"""文本压缩原语的单测。"""

from __future__ import annotations

import json

from token_saver_core.compressor import (
    compact_deferred_tools_reminder,
    compact_json_text,
    compress_text,
    dedupe_consecutive_lines,
    dedupe_requirements,
    normalize_whitespace,
    truncate_smart,
)
from token_saver_core.policy import OptimizePolicy


def test_normalize_whitespace_keeps_line_structure() -> None:
    """折叠冗余空白但保留单个换行与行首缩进（Markdown 结构）。"""
    raw = "第一行   \n\n\n\n- 列表项\n    - 子项\n"
    result = normalize_whitespace(raw)
    assert result.count("\n") <= 5
    assert "- 列表项" in result
    assert "    - 子项" in result  # 行首缩进必须保留，否则嵌套列表被压平
    assert result.startswith("第一行\n")


def test_dedupe_only_removes_consecutive_duplicates() -> None:
    """只删除连续重复行，非重复的相同内容必须保留。"""
    raw = "A\nA\nB\nA"
    assert dedupe_consecutive_lines(raw) == "A\nB\nA"


def test_compact_json_text_is_lossless() -> None:
    """JSON 紧凑化后语义不变。"""
    payload = {"a": [1, 2, 3], "b": {"中文": "值"}}
    raw = json.dumps(payload, ensure_ascii=False, indent=4)
    result = compact_json_text(raw)
    assert json.loads(result) == payload
    assert len(result) < len(raw)


def test_compact_json_ignores_plain_text() -> None:
    """非 JSON 文本原样返回。"""
    raw = "这是一段普通说明，不是 JSON。"
    assert compact_json_text(raw) == raw


def test_truncate_smart_keeps_head_and_tail() -> None:
    """截断必须保留开头与结尾（结论常在末尾）。"""
    raw = "HEAD" + "中" * 500 + "TAIL"
    result = truncate_smart(raw, 80)
    assert result.startswith("HEAD")
    assert result.endswith("TAIL")
    assert len(result) <= 80
    assert "已省略中间" in result


def test_truncate_smart_noop_when_under_limit() -> None:
    """未超限时不改动。"""
    raw = "短文本"
    assert truncate_smart(raw, 100) is raw


def test_compress_text_is_deterministic() -> None:
    """确定性是 prompt cache 命中的前提。"""
    policy = OptimizePolicy()
    raw = "内容   \n\n\n\n内容\n" * 5
    first = compress_text(raw, policy)
    second = compress_text(raw, policy)
    assert first == second
    assert len(first) < len(raw)


def test_compress_text_disabled_returns_original() -> None:
    """关闭压缩开关时原样返回。"""
    policy = OptimizePolicy(enable_text_compression=False)
    raw = "内容   \n\n\n\n内容"
    assert compress_text(raw, policy) == raw


def test_deferred_reminder_keeps_tool_names() -> None:
    """deferred 清单只截断描述，工具名与序号完整保留。"""
    raw = (
        "<system-reminder>\n"
        "以下工具当前未直接暴露给你：\n"
        "1. mcp_search_docs: " + "很长的使用说明" * 30 + "\n"
        "2. mcp_fetch_url: 短说明\n"
        "</system-reminder>"
    )
    result = compact_deferred_tools_reminder(raw, 40)
    assert "1. mcp_search_docs" in result
    assert "2. mcp_fetch_url: 短说明" in result
    assert len(result) < len(raw)


def test_dedupe_requirements_removes_exact_duplicates_only() -> None:
    """额外提示词去重不能误删相似但不同的要求。"""
    raw = "用口语回复\n用口语回复\n保持简短"
    assert dedupe_requirements(raw) == "用口语回复\n保持简短"
