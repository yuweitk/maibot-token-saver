"""上下文字符数与 token 用量的估算工具。

MaiBot 自身在 ``src/maisaka/context/usage.py`` 里明确说明：本地只统计字符数，
真正的 token 由模型返回的 ``usage`` 给出，前端按字符占比换算，避免引入第二套
tokenizer 造成口径不一致。本模块沿用同样的原则，额外提供一个**仅供趋势对比**
的粗估函数，绝不用于计费。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

# 与 Context Item 快照中可能承载可见文本的字段保持一致
_TEXT_PART_TYPES = frozenset({"text", "refusal"})


def is_cjk(char: str) -> bool:
    """判断单个字符是否属于中日韩表意文字等「宽字符」。

    Args:
        char: 单个字符。

    Returns:
        bool: 属于 CJK / 全角标点时返回 True。
    """

    if len(char) != 1:
        return False
    code = ord(char)
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
        return True
    if 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF:
        return True
    if 0xAC00 <= code <= 0xD7AF or 0x3040 <= code <= 0x30FF:
        return True
    return False


def estimate_tokens(text: str) -> int:
    """粗略估算一段文本对应的 token 数，**仅用于趋势对比**。

    采用「中文约 0.7 token/字、其余约 1 token/3.2 字符」的混合口径。
    真实数值取决于具体模型的 tokenizer，请以模型返回的 ``usage`` 为准。

    Args:
        text: 待估算的文本。

    Returns:
        int: 估算的 token 数，最小为 0。
    """

    if not text:
        return 0
    cjk_count = sum(1 for char in text if is_cjk(char))
    other_count = len(text) - cjk_count
    return int(cjk_count * 0.7 + other_count / 3.2)


def item_visible_text(item: Dict[str, Any]) -> str:
    """提取单个 Context Item 中所有会进入请求的可见文本。

    ``image_base64`` 不参与字符统计（MaiBot 的 usage.py 同样如此处理），
    但图片数量会在 :func:`measure_items` 中单独记录。

    Args:
        item: ``serialize_prompt_items()`` 产出的单个 Item 字典。

    Returns:
        str: 拼接后的可见文本。
    """

    if not isinstance(item, dict):
        return ""
    chunks: List[str] = []

    for part in item.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") in _TEXT_PART_TYPES:
            value = part.get("text") or part.get("refusal")
            if isinstance(value, str):
                chunks.append(value)

    for key in ("summary_parts", "text_parts"):
        value = item.get(key)
        if isinstance(value, (list, tuple)):
            chunks.extend(str(entry) for entry in value if isinstance(entry, str))

    tool_call = item.get("tool_call")
    if isinstance(tool_call, dict):
        name = tool_call.get("func_name")
        if isinstance(name, str):
            chunks.append(name)
        args = tool_call.get("args")
        if args is not None:
            chunks.append(_stable_repr(args))
        extra = tool_call.get("extra_content")
        if extra:
            chunks.append(_stable_repr(extra))

    for key in ("output", "display_summary"):
        value = item.get(key)
        if isinstance(value, str):
            chunks.append(value)

    details = item.get("details")
    if isinstance(details, (list, tuple)):
        chunks.extend(str(entry) for entry in details if isinstance(entry, str))

    return "\n".join(chunks)


def _stable_repr(value: Any) -> str:
    """生成不依赖字典插入顺序的稳定字符串表示。

    Args:
        value: 任意可序列化值。

    Returns:
        str: 紧凑且稳定的表示，用于长度统计而非反序列化。
    """

    import json

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


@dataclass(slots=True)
class MeasureResult:
    """一次上下文测量的结果。

    Attributes:
        chars: 可见文本总字符数。
        estimated_tokens: 粗估 token 数（仅趋势对比）。
        item_count: Item 总数。
        image_count: 图片 part 数量。
        by_type: 按 Item 类型分组的字符数。
    """

    chars: int = 0
    estimated_tokens: int = 0
    item_count: int = 0
    image_count: int = 0
    by_type: Dict[str, int] = field(default_factory=dict)


def measure_items(items: Iterable[Dict[str, Any]]) -> MeasureResult:
    """测量一组 Context Items 的可见文本占用。

    Args:
        items: Context Item 快照字典的可迭代对象。

    Returns:
        MeasureResult: 字符数、粗估 token 数、条目数与分段统计。
    """

    result = MeasureResult()
    for item in items:
        if not isinstance(item, dict):
            continue
        result.item_count += 1
        item_type = str(item.get("item_type") or "Unknown")
        text = item_visible_text(item)
        chars = len(text)
        result.chars += chars
        result.by_type[item_type] = result.by_type.get(item_type, 0) + chars

        for part in item.get("parts") or []:
            if isinstance(part, dict) and part.get("type") == "image":
                result.image_count += 1

    # 中文约 0.7 token/字，中英混排时取 0.55 作为保守系数；
    # 只用于展示趋势，真实 token 以模型返回的 usage 为准。
    result.estimated_tokens = int(result.chars * 0.55)
    return result


def measure_tool_definitions(definitions: Iterable[Dict[str, Any]]) -> int:
    """测量工具定义序列化后的字符数。

    Args:
        definitions: OpenAI function schema 风格的工具定义列表。

    Returns:
        int: 所有定义按紧凑 JSON 序列化后的总字符数。
    """

    import json

    total = 0
    for definition in definitions:
        if isinstance(definition, dict):
            total += len(json.dumps(definition, ensure_ascii=False, separators=(",", ":")))
    return total


def format_bytes_delta(before: int, after: int) -> str:
    """生成便于阅读的节省比例文本。

    Args:
        before: 优化前字符数。
        after: 优化后字符数。

    Returns:
        str: 形如 ``12000 -> 5400 (-55.0%)`` 的描述。
    """

    if before <= 0:
        return "0 -> 0 (-0.0%)"
    ratio = (before - after) / before * 100
    return f"{before} -> {after} (-{ratio:.1f}%)"
