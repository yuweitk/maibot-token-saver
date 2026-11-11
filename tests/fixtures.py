"""构造 Context Item 快照的测试夹具。

结构严格对齐 ``src/llm_models/payload_content/context_snapshot.py`` 的
``serialize_context_item_snapshot()`` 输出，保证单测与 Host 真实载荷一致。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_MESSAGE_TYPES = frozenset({"SystemMessageItem", "UserMessageItem", "AssistantMessageItem"})


def make_item(
    item_type: str,
    index: int,
    *,
    text: str = "",
    turn_id: Optional[str] = None,
    call_id: str = "",
    output: str = "",
    args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """生成单个 Item 快照字典。

    Args:
        item_type: Item 类型名。
        index: 用于生成稳定 item_id 的序号。
        text: 消息类 Item 的文本内容。
        turn_id: logical_turn_id。
        call_id: 工具调用 ID。
        output: 工具结果文本。
        args: 工具调用参数。

    Returns:
        Dict[str, Any]: 可直接传给优化函数的 Item 字典。
    """

    item: Dict[str, Any] = {
        "item_type": item_type,
        "meta": {
            "item_id": f"item-{index}",
            "logical_turn_id": turn_id,
            "timestamp": "2026-01-01T00:00:00",
        },
    }
    if item_type in _MESSAGE_TYPES:
        item["parts"] = [{"type": "text", "text": text}]
    elif item_type == "ReasoningItem":
        item["representation"] = "plain"
        item["summary_parts"] = []
        item["text_parts"] = [text]
    elif item_type == "FunctionCallItem":
        item["tool_call"] = {
            "call_id": call_id or f"call-{index}",
            "func_name": "query_memory",
            "args": args or {"query": "test"},
            "extra_content": None,
        }
    elif item_type == "FunctionCallOutputItem":
        item.update(
            {"call_id": call_id or f"call-{index}", "output": output, "success": True, "tool_name": "query_memory"}
        )
    elif item_type == "ProviderActivityItem":
        item.update(
            {
                "provider_type": "test", "call_id": f"pa-{index}", "status": "done",
                "display_summary": text, "action_type": "search", "details": [], "source_count": 1,
            }
        )
    return item


def make_conversation(*, tool_turns: int = 1, tail_messages: int = 16, long_output: bool = True) -> List[Dict[str, Any]]:
    """构造一段含系统提示词、旧历史、工具轮次与近期消息的完整请求。

    Args:
        tool_turns: 工具调用轮次数量。
        tail_messages: 尾部近期普通消息数量。
        long_output: 工具结果是否为超长文本。

    Returns:
        List[Dict[str, Any]]: 完整 Item 列表。
    """

    items: List[Dict[str, Any]] = [
        make_item("SystemMessageItem", 0, text="你是麦麦。你的性格：活泼。" * 3),
        make_item("UserMessageItem", 1, text="早啊，今天聊点什么？"),
        make_item("ReasoningItem", 2, text="我需要先判断用户情绪……" * 80),
        make_item("ProviderActivityItem", 3, text="搜索了一次知识库"),
        make_item("UserMessageItem", 4, text="旧消息 " * 20),
    ]
    index = 5
    for turn in range(tool_turns):
        turn_id = f"turn-{turn}"
        call_id = f"call-{turn}"
        items.append(
            make_item("FunctionCallItem", index, turn_id=turn_id, call_id=call_id)
        )
        index += 1
        output = ("工具返回的长文本片段。" * 200) if long_output else "简短结果"
        items.append(
            make_item(
                "FunctionCallOutputItem", index, turn_id=turn_id, call_id=call_id, output=output
            )
        )
        index += 1
    for number in range(tail_messages):
        role = "UserMessageItem" if number % 2 == 0 else "AssistantMessageItem"
        items.append(make_item(role, index, text=f"近期消息 {number}：哈哈哈"))
        index += 1
    return items


def make_tool_definitions(count: int = 2, *, long_description: bool = True) -> List[Dict[str, Any]]:
    """构造 OpenAI function schema 风格的工具定义列表。

    Args:
        count: 工具数量。
        long_description: 描述是否为超长文本。

    Returns:
        List[Dict[str, Any]]: 工具定义列表。
    """

    definitions = []
    for number in range(count):
        description = (
            "这个工具用于查询信息。" * 80 + " 使用时请注意参数格式。" if long_description else "查询信息"
        )
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": f"demo_tool_{number}",
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "title": "DemoParameters",
                        "properties": {
                            "query": {"type": "string", "title": "Query", "description": "要查询的关键词" * 40},
                        },
                        "required": ["query"],
                    },
                },
            }
        )
    return definitions
