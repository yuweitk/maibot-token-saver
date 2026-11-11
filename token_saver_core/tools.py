"""工具定义（Tool Schema）瘦身。

MaiBot 在 ``chat_loop_service.py:1254`` 把 ``serialize_tool_definitions(all_tools)``
的结果交给 ``maisaka.planner.before_request``，插件改写 ``tool_definitions``
后 Host 会**直接采用**这份列表（``src/maisaka/chat_loop_service.py:1286``）。

工具 Schema 是 Planner 每轮都要重复携带的固定开销：MCP 工具、
插件工具的 description 常带长篇说明和示例。这里只做两类安全操作：
    - 压缩 description 的冗余空白与超长部分（保留首尾，因为参数约束常在末尾）；
    - 移除 JSON Schema 里纯展示用的 ``title`` / ``$schema`` 字段。

**默认不删除任何工具**：删工具会让 Planner 在需要时无法调用，且会改变
``_active_tool_names``，风险远高于收益。激进裁剪需显式开启。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from .budget import measure_tool_definitions
from .compressor import dedupe_consecutive_lines, normalize_whitespace, truncate_smart
from .policy import OptimizePolicy

# 这些字段只影响人类阅读，不参与 JSON Schema 校验，可安全移除
_REDUNDANT_SCHEMA_KEYS = frozenset({"title", "$schema", "$comment", "examples"})


@dataclass(slots=True)
class ToolOptimizeReport:
    """工具定义优化报告。

    Attributes:
        before_chars: 优化前字符数。
        after_chars: 优化后字符数（dry_run 时为预计值）。
        slimmed_count: 被压缩描述的工具数。
        removed_count: 被移除的工具数（仅激进模式）。
        applied: 是否真正应用。
        reason: 未应用原因。
    """

    before_chars: int = 0
    after_chars: int = 0
    slimmed_count: int = 0
    removed_count: int = 0
    applied: bool = False
    reason: str = ""

    @property
    def saved_chars(self) -> int:
        """返回节省的字符数。"""

        return max(0, self.before_chars - self.after_chars)


def _clean_schema(node: Any, policy: OptimizePolicy, *, depth: int = 0) -> Any:
    """递归精简 JSON Schema 节点。

    Args:
        node: 当前 Schema 节点。
        policy: 优化策略。
        depth: 当前递归深度，用于防止异常结构导致无限递归。

    Returns:
        Any: 精简后的节点。
    """

    if depth > 12 or not isinstance(node, dict):
        return node
    result: Dict[str, Any] = {}
    for key, value in node.items():
        if key in _REDUNDANT_SCHEMA_KEYS:
            continue
        if key == "description" and isinstance(value, str):
            cleaned = normalize_whitespace(value)
            cleaned = dedupe_consecutive_lines(cleaned)
            limit = max(80, policy.max_tool_description_chars // 3)
            if policy.max_tool_description_chars > 0:
                cleaned = truncate_smart(cleaned, limit, head_ratio=0.8)
            result[key] = cleaned.strip()
        elif isinstance(value, dict):
            result[key] = _clean_schema(value, policy, depth=depth + 1)
        elif isinstance(value, list):
            result[key] = [
                _clean_schema(entry, policy, depth=depth + 1) if isinstance(entry, dict) else entry
                for entry in value
            ]
        else:
            result[key] = value
    return result


def _slim_definition(definition: Dict[str, Any], policy: OptimizePolicy) -> Tuple[Dict[str, Any], bool]:
    """精简单个工具定义。

    Args:
        definition: OpenAI function schema 风格的工具定义。
        policy: 优化策略。

    Returns:
        Tuple[Dict[str, Any], bool]: (精简后的定义, 是否发生了改动)。
    """

    result = copy.deepcopy(definition)
    function = result.get("function")
    if not isinstance(function, dict):
        # 兼容少数扁平结构
        function = result
    changed = False

    description = function.get("description")
    if isinstance(description, str) and description:
        cleaned = normalize_whitespace(description)
        cleaned = dedupe_consecutive_lines(cleaned)
        if policy.max_tool_description_chars > 0:
            cleaned = truncate_smart(cleaned, policy.max_tool_description_chars, head_ratio=0.8)
        cleaned = cleaned.strip()
        if cleaned and cleaned != description:
            function["description"] = cleaned
            changed = True

    parameters = function.get("parameters")
    if isinstance(parameters, dict):
        cleaned_schema = _clean_schema(parameters, policy)
        if cleaned_schema != parameters:
            function["parameters"] = cleaned_schema
            changed = True

    return result, changed


def _definition_name(definition: Dict[str, Any]) -> str:
    """读取工具定义的名称。

    Args:
        definition: 单个工具定义。

    Returns:
        str: 工具名，缺失时返回空串。
    """

    function = definition.get("function")
    if isinstance(function, dict):
        value = function.get("name")
        return str(value) if value else ""
    value = definition.get("name")
    return str(value) if value else ""


def optimize_tool_definitions(
    definitions: Any,
    policy: OptimizePolicy,
) -> Tuple[Any, ToolOptimizeReport]:
    """优化 Planner 本轮的工具定义列表。

    Args:
        definitions: Hook 传入的 ``tool_definitions``（``list[dict]``）。
        policy: 优化策略。

    Returns:
        Tuple[Any, ToolOptimizeReport]: (可回传的定义列表, 优化报告)。
        任何异常都会返回原始列表。
    """

    report = ToolOptimizeReport()
    if not policy.enabled or not policy.enable_tool_slimming:
        report.reason = "disabled"
        return definitions, report
    if not isinstance(definitions, list) or not definitions:
        report.reason = "empty"
        return definitions, report

    report.before_chars = measure_tool_definitions(definitions)
    try:
        candidate: List[Dict[str, Any]] = []
        for definition in definitions:
            if not isinstance(definition, dict):
                candidate.append(definition)
                continue
            slimmed, changed = _slim_definition(definition, policy)
            candidate.append(slimmed)
            if changed:
                report.slimmed_count += 1

        if policy.aggressive_tool_pruning and len(candidate) > policy.max_tool_definitions > 0:
            keep = set(policy.tool_keep_names)
            kept_indices = [
                index for index, definition in enumerate(candidate)
                if _definition_name(definition) in keep
            ]
            kept_set = set(kept_indices)
            # 按工具名稳定排序，保证确定性（不能依赖字典/list 的偶然顺序）
            remaining_indices = sorted(
                (index for index in range(len(candidate)) if index not in kept_set),
                key=lambda index: _definition_name(candidate[index]),
            )
            slots = max(0, policy.max_tool_definitions - len(kept_indices))
            chosen = kept_set | set(remaining_indices[:slots])
            # 按原始顺序还原，避免改变模型看到的工具排列
            candidate = [
                definition for index, definition in enumerate(candidate) if index in chosen
            ]
            report.removed_count = len(definitions) - len(candidate)

        report.after_chars = measure_tool_definitions(candidate)
        if policy.dry_run or candidate == definitions:
            report.reason = "dry_run" if policy.dry_run else "no_change"
            return definitions, report
        report.applied = True
        return candidate, report
    except Exception as exc:  # noqa: BLE001 - fail-open
        report.after_chars = report.before_chars
        report.reason = f"fallback: {type(exc).__name__}: {exc}"
        return definitions, report
