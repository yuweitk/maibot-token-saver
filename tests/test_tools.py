"""工具定义瘦身与用量估算的单测。"""

from __future__ import annotations

from fixtures import make_conversation, make_tool_definitions
from token_saver_core.budget import estimate_tokens, measure_items, measure_tool_definitions
from token_saver_core.policy import OptimizePolicy
from token_saver_core.tools import optimize_tool_definitions


def test_tool_slimming_never_removes_tools_by_default() -> None:
    """默认模式只压缩描述，绝不能删工具（否则 Planner 会调用失败）。"""
    definitions = make_tool_definitions(count=3)
    policy = OptimizePolicy()
    result, report = optimize_tool_definitions(definitions, policy)
    assert len(result) == len(definitions)
    assert report.removed_count == 0
    assert report.saved_chars > 0
    assert report.applied is True
    names = [item["function"]["name"] for item in result]
    assert names == [item["function"]["name"] for item in definitions]


def test_tool_slimming_removes_only_display_fields() -> None:
    """移除 title 等展示字段，但保留 required / type / properties。"""
    definitions = make_tool_definitions(count=1)
    result, _ = optimize_tool_definitions(definitions, OptimizePolicy())
    parameters = result[0]["function"]["parameters"]
    assert parameters["required"] == ["query"]
    assert parameters["type"] == "object"
    assert "query" in parameters["properties"]
    assert "title" not in parameters
    assert "title" not in parameters["properties"]["query"]


def test_tool_slimming_is_deterministic() -> None:
    """工具定义压缩必须确定性，避免破坏每轮固定的前缀缓存。"""
    definitions = make_tool_definitions(count=2)
    policy = OptimizePolicy()
    first, _ = optimize_tool_definitions(definitions, policy)
    second, _ = optimize_tool_definitions(definitions, policy)
    assert first == second


def test_aggressive_pruning_keeps_whitelist() -> None:
    """激进模式保留白名单工具，且结果稳定。"""
    definitions = make_tool_definitions(count=10)
    definitions[0]["function"]["name"] = "reply"
    policy = OptimizePolicy(
        aggressive_tool_pruning=True,
        max_tool_definitions=3,
        tool_keep_names=("reply", "stop"),
    )
    result, report = optimize_tool_definitions(definitions, policy)
    names = [item["function"]["name"] for item in result]
    assert "reply" in names
    assert len(names) <= 3
    assert report.removed_count > 0


def test_tool_slimming_disabled_returns_original() -> None:
    """关闭开关时原样返回同一个对象。"""
    definitions = make_tool_definitions(count=2)
    result, report = optimize_tool_definitions(definitions, OptimizePolicy(enable_tool_slimming=False))
    assert result is definitions
    assert report.applied is False


def test_measure_items_ignores_image_base64() -> None:
    """图片 base64 不计入字符统计，但数量要单独记录。"""
    items = [
        {
            "item_type": "UserMessageItem",
            "meta": {"item_id": "a", "logical_turn_id": None, "timestamp": "2026-01-01T00:00:00"},
            "parts": [
                {"type": "text", "text": "你好"},
                {"type": "image", "image_format": "png", "image_base64": "A" * 100000},
            ],
        }
    ]
    result = measure_items(items)
    assert result.chars == 2
    assert result.image_count == 1
    assert result.item_count == 1


def test_measure_conversation_has_expected_sections() -> None:
    """完整会话能被正确分段统计。"""
    items = make_conversation(tool_turns=1)
    result = measure_items(items)
    assert result.item_count == len(items)
    assert "SystemMessageItem" in result.by_type
    assert "ReasoningItem" in result.by_type
    assert result.chars > 0


def test_estimate_tokens_is_monotonic() -> None:
    """token 估算随文本长度单调不减，且空文本为 0。"""
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文测试") <= estimate_tokens("中文测试" * 10)


def test_measure_tool_definitions_counts_all() -> None:
    """工具定义字符数统计覆盖全部条目。"""
    definitions = make_tool_definitions(count=2)
    assert measure_tool_definitions(definitions) > 0
    assert measure_tool_definitions([]) == 0
