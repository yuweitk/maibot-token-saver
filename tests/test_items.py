"""Context Items 优化引擎的单测：重点验证协议安全与 fail-open。"""

from __future__ import annotations

from fixtures import make_conversation, make_item
from token_saver_core.items import (
    ItemProtocolError,
    optimize_output_items,
    optimize_request_items,
    validate_request_items,
)
from token_saver_core.policy import OptimizePolicy


def test_valid_conversation_fixture_passes_validation() -> None:
    """测试夹具本身必须满足 Host 的关系协议。"""
    items = make_conversation(tool_turns=2)
    validate_request_items(items, original=items)


def test_drops_old_internal_items_and_truncates_tool_output() -> None:
    """旧推理 / Provider 活动被删除，超长工具结果被截断，协议仍完整。"""
    items = make_conversation(tool_turns=1, long_output=True)
    policy = OptimizePolicy(keep_recent_items=4, drop_old_reasoning=True, max_tool_output_chars=120)
    result, report = optimize_request_items(items, policy, request_kind="planner", budget=10**9)

    assert report.applied is True
    assert report.saved_chars > 0
    assert len(result) < len(items)
    types = [item["item_type"] for item in result]
    assert "ReasoningItem" not in types
    assert "ProviderActivityItem" not in types
    outputs = [item for item in result if item["item_type"] == "FunctionCallOutputItem"]
    assert outputs and len(outputs[0]["output"]) <= 120
    validate_request_items(result, original=items)


def test_system_prompt_and_recent_tail_are_untouched() -> None:
    """系统提示词与最近 N 条消息必须逐字不变（拟人效果保护）。"""
    items = make_conversation(tool_turns=1, tail_messages=10)
    policy = OptimizePolicy(keep_recent_items=6, drop_old_reasoning=True)
    result, _ = optimize_request_items(items, policy, request_kind="planner", budget=10**9)

    assert result[0] == items[0]
    assert result[-6:] == items[-6:]


def test_budget_trim_removes_whole_tool_turns() -> None:
    """预算裁剪必须按 logical_turn_id 成对删除 call/output。"""
    items = make_conversation(tool_turns=3, long_output=False, tail_messages=6)
    policy = OptimizePolicy(
        keep_recent_items=4, drop_old_reasoning=False, max_tool_output_chars=0,
        enable_history_trim=True,
    )
    result, report = optimize_request_items(items, policy, request_kind="planner", budget=300)

    calls = {item["tool_call"]["call_id"] for item in result if item["item_type"] == "FunctionCallItem"}
    outputs = {item["call_id"] for item in result if item["item_type"] == "FunctionCallOutputItem"}
    assert calls == outputs, "工具调用与结果必须成对保留"
    validate_request_items(result, original=items)
    assert report.before_chars > report.after_chars


def test_dry_run_never_rewrites() -> None:
    """试运行模式只统计、不改写。"""
    items = make_conversation(tool_turns=1)
    policy = OptimizePolicy(keep_recent_items=4, max_tool_output_chars=100, dry_run=True)
    result, report = optimize_request_items(items, policy, request_kind="planner", budget=10**9)
    assert result is items
    assert report.applied is False
    assert report.reason == "dry_run"
    assert report.saved_chars > 0


def test_disabled_policy_returns_original_object() -> None:
    """总开关关闭时原样返回同一个对象。"""
    items = make_conversation()
    result, _ = optimize_request_items(items, OptimizePolicy(enabled=False), request_kind="planner")
    assert result is items


def test_malformed_items_fail_open() -> None:
    """结构异常的 Item 不能让插件抛错，必须原样回退。"""
    items = [{"broken": True}]
    result, report = optimize_request_items(items, OptimizePolicy(keep_recent_items=1), request_kind="planner")
    assert result is items
    assert report.applied is False


def test_missing_output_is_detected() -> None:
    """删掉 output 却保留 call 会被校验拒绝。"""
    items = [
        make_item("SystemMessageItem", 0, text="system"),
        make_item("UserMessageItem", 1, text="hi"),
        make_item("FunctionCallItem", 2, turn_id="t1", call_id="c1"),
        make_item("FunctionCallOutputItem", 3, turn_id="t1", call_id="c1", output="ok"),
    ]
    broken = [item for item in items if item["item_type"] != "FunctionCallOutputItem"]
    try:
        validate_request_items(broken, original=items)
    except ItemProtocolError:
        return
    raise AssertionError("应当检测到 call/output 不成对")


def test_anchor_requirement_is_enforced() -> None:
    """首个 function call 之前必须有 user/output 锚点。"""
    items = [
        make_item("SystemMessageItem", 0, text="system"),
        make_item("FunctionCallItem", 2, turn_id="t1", call_id="c1"),
        make_item("FunctionCallOutputItem", 3, turn_id="t1", call_id="c1", output="ok"),
    ]
    try:
        validate_request_items(items, original=items)
    except ItemProtocolError:
        return
    raise AssertionError("应当检测到缺少锚点")


def test_output_condense_keeps_item_count() -> None:
    """输出压缩只改文本，绝不删除或新增 Item。"""
    items = [
        make_item("AssistantMessageItem", 0, text="内容   \n\n\n\n内容" * 20),
        make_item("ReasoningItem", 1, text="推理内容" * 200),
    ]
    policy = OptimizePolicy(max_reasoning_chars=200)
    result, report = optimize_output_items(items, policy)
    assert len(result) == len(items)
    assert report.saved_chars > 0
    assert result[0]["meta"] == items[0]["meta"]


def test_user_messages_are_never_deduplicated() -> None:
    """连续重复的用户原话属于群聊语气节奏，必须原样保留。"""
    items = [
        make_item("SystemMessageItem", 0, text="system"),
        make_item("UserMessageItem", 1, text="哈哈哈"),
        make_item("UserMessageItem", 2, text="哈哈哈"),
        make_item("UserMessageItem", 3, text="哈哈哈"),
        make_item("AssistantMessageItem", 4, text="recent-1"),
        make_item("UserMessageItem", 5, text="recent-2"),
    ]
    policy = OptimizePolicy(
        keep_recent_items=2, dedupe_consecutive=True, drop_old_reasoning=False,
        max_tool_output_chars=0, enable_history_trim=False,
    )
    result, _ = optimize_request_items(items, policy, request_kind="planner", budget=10**9)
    user_texts = [
        item["parts"][0]["text"] for item in result if item["item_type"] == "UserMessageItem"
    ]
    assert user_texts.count("哈哈哈") == 3


def test_deterministic_across_repeated_calls() -> None:
    """同一输入重复优化必须得到同一输出（prompt cache 要求）。"""
    items = make_conversation(tool_turns=2)
    policy = OptimizePolicy(keep_recent_items=4, max_tool_output_chars=150)
    first, _ = optimize_request_items(items, policy, request_kind="planner", budget=10**9)
    second, _ = optimize_request_items(items, policy, request_kind="planner", budget=10**9)
    assert first == second
