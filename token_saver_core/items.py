"""Context Items 的安全改写引擎（插件的核心）。

MaiBot 在 ``maisaka.planner.before_request`` 与 ``maisaka.replyer.before_model_request``
两个 Hook 上把 ``serialize_prompt_items()`` 的结果交给插件，插件通过
``modified_kwargs["items"]`` 返回新的 Item 列表。Host 随后调用
``deserialize_prompt_items()``，它会执行 ``validate_context_items(REQUEST_CONTEXT)``，
并且 planner 侧还会额外执行 ``_validate_function_call_context_anchors()``。

因此本模块遵守三条硬约束：
    1. **保留 meta**：``item_id`` / ``logical_turn_id`` / ``timestamp`` 原样带回；
    2. **工具调用成对**：FunctionCallItem 与 FunctionCallOutputItem 必须按
       ``logical_turn_id`` 同组删除，且 output 必须在 call 之后；
    3. **锚点完整**：首个 FunctionCallItem 之前必须存在 UserMessageItem 或
       FunctionCallOutputItem。

任何一步校验不通过都会**原样回退**（fail-open）——宁可这一轮不省 token，
也绝不让麦麦报错或不回复。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .budget import item_visible_text, measure_items
from .compressor import compress_text, dedupe_requirements, truncate_smart
from .policy import OptimizePolicy

_MESSAGE_ITEM_TYPES = frozenset(
    {"SystemMessageItem", "UserMessageItem", "AssistantMessageItem"}
)
_INTERNAL_ITEM_TYPES = frozenset(
    {"ReasoningItem", "ProviderActivityItem", "ProviderOpaqueItem"}
)
_TOOL_PAIR_TYPES = frozenset({"FunctionCallItem", "FunctionCallOutputItem"})


@dataclass(slots=True)
class OptimizeReport:
    """一次 Item 优化的结果报告。

    Attributes:
        before_chars: 优化前可见字符数。
        after_chars: 优化后可见字符数（dry_run 时为预计值）。
        removed_items: 被删除的 Item 数量。
        compressed_items: 被改写的 Item 数量。
        actions: 各优化动作命中次数。
        applied: 是否真正应用了改写（dry_run 或回退时为 False）。
        reason: 未应用或回退的原因。
    """

    before_chars: int = 0
    after_chars: int = 0
    removed_items: int = 0
    compressed_items: int = 0
    actions: Dict[str, int] = field(default_factory=dict)
    applied: bool = False
    reason: str = ""

    @property
    def saved_chars(self) -> int:
        """返回节省的字符数（不会为负）。"""

        return max(0, self.before_chars - self.after_chars)

    def bump(self, action: str, count: int = 1) -> None:
        """累加某个优化动作的命中次数。

        Args:
            action: 动作名称。
            count: 本次命中数量。
        """

        self.actions[action] = self.actions.get(action, 0) + count


class ItemProtocolError(ValueError):
    """Context Item 改写后不满足 Host 的关系协议。"""


def _meta_turn_id(item: Dict[str, Any]) -> Optional[str]:
    """读取 Item 的 logical_turn_id。

    Args:
        item: 单个 Item 字典。

    Returns:
        Optional[str]: 存在且非空时返回该 ID，否则 None。
    """

    meta = item.get("meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get("logical_turn_id")
    return value if isinstance(value, str) and value else None


def _call_id(item: Dict[str, Any]) -> Optional[str]:
    """读取工具调用或工具结果的 call_id。

    Args:
        item: 单个 Item 字典。

    Returns:
        Optional[str]: call_id 字符串，缺失时返回 None。
    """

    if item.get("item_type") == "FunctionCallItem":
        tool_call = item.get("tool_call")
        if isinstance(tool_call, dict):
            value = tool_call.get("call_id")
            return value if isinstance(value, str) else None
        return None
    value = item.get("call_id")
    return value if isinstance(value, str) else None


def validate_request_items(items: Sequence[Dict[str, Any]], *, original: Sequence[Dict[str, Any]]) -> None:
    """校验改写后的 Items 能否被 Host 安全反序列化。

    复刻 Host 侧 ``validate_context_items(REQUEST_CONTEXT)`` 与
    ``_validate_function_call_context_anchors`` 的关键约束。

    Args:
        items: 待校验的 Item 列表。
        original: 改写前的 Item 列表，用于确认系统提示词没有被移除。

    Raises:
        ItemProtocolError: 任一约束不满足时抛出。
    """

    if not isinstance(items, list) or not items:
        raise ItemProtocolError("改写结果为空")

    original_system_count = sum(
        1 for item in original if isinstance(item, dict) and item.get("item_type") == "SystemMessageItem"
    )
    new_system_count = sum(
        1 for item in items if isinstance(item, dict) and item.get("item_type") == "SystemMessageItem"
    )
    if original_system_count and new_system_count < original_system_count:
        raise ItemProtocolError("系统提示词被移除")

    ids = [item.get("meta", {}).get("item_id") for item in items if isinstance(item, dict)]
    if any(not isinstance(value, str) or not value for value in ids):
        raise ItemProtocolError("存在缺失 item_id 的 Item")
    if len(set(ids)) != len(ids):
        raise ItemProtocolError("存在重复 item_id")

    calls: Dict[str, Tuple[int, Optional[str]]] = {}
    outputs: Dict[str, Tuple[int, Optional[str]]] = {}
    original_calls: set[str] = set()
    original_outputs: set[str] = set()

    for item in original:
        if not isinstance(item, dict):
            continue
        item_type = item.get("item_type")
        cid = _call_id(item) if item_type in _TOOL_PAIR_TYPES else None
        if not cid:
            continue
        if item_type == "FunctionCallItem":
            original_calls.add(cid)
        else:
            original_outputs.add(cid)

    for index, item in enumerate(items):
        item_type = item.get("item_type")
        if item_type not in _TOOL_PAIR_TYPES:
            continue
        cid = _call_id(item)
        if not cid:
            raise ItemProtocolError("工具 Item 缺少 call_id")
        target = calls if item_type == "FunctionCallItem" else outputs
        if cid in target:
            raise ItemProtocolError(f"重复的工具 Item: {cid}")
        target[cid] = (index, _meta_turn_id(item))

    # 只要求「改写后的关系是原始关系的闭合子集」：Host 允许 pending call，
    # 插件不能因为原样本来就没有 output 而拒绝自己的安全改写。
    for cid in outputs:
        if cid not in original_outputs:
            raise ItemProtocolError(f"新增了来源不明的 function output: {cid}")
        if cid in original_calls and cid not in calls:
            raise ItemProtocolError(f"保留了 output 却删除了对应 call: {cid}")
    for cid, (call_index, call_turn) in calls.items():
        if cid not in original_calls:
            raise ItemProtocolError(f"新增了来源不明的 function call: {cid}")
        if cid not in outputs:
            # 原始请求里本来就没有 output 时保持原状即可
            if cid in original_outputs:
                raise ItemProtocolError(f"保留了 call 却删除了对应 output: {cid}")
            continue
        output_index, output_turn = outputs[cid]
        if output_index <= call_index:
            raise ItemProtocolError(f"function output 位于调用之前: {cid}")
        if call_turn != output_turn:
            raise ItemProtocolError(f"call/output logical_turn_id 不一致: {cid}")

    anchor = False
    for item in items:
        item_type = item.get("item_type")
        if item_type in ("UserMessageItem", "FunctionCallOutputItem"):
            anchor = True
        elif item_type == "FunctionCallItem" and not anchor:
            raise ItemProtocolError("function call 缺少前置 user/output 锚点")


def _compress_item(item: Dict[str, Any], policy: OptimizePolicy, report: OptimizeReport) -> Dict[str, Any]:
    """压缩单个 Item 的可见文本字段，保持结构与 meta 不变。

    Args:
        item: 原始 Item 字典（不会被原地修改）。
        policy: 优化策略。
        report: 用于记录命中动作的统计对象。

    Returns:
        Dict[str, Any]: 压缩后的新 Item 字典。
    """

    item_type = item.get("item_type")
    before = item_visible_text(item)
    result = dict(item)

    if item_type in _MESSAGE_ITEM_TYPES:
        parts = item.get("parts")
        if isinstance(parts, list):
            new_parts: List[Any] = []
            for part in parts:
                if not isinstance(part, dict):
                    new_parts.append(part)
                    continue
                key = "text" if part.get("type") == "text" else "refusal"
                if part.get("type") not in ("text", "refusal") or not isinstance(part.get(key), str):
                    new_parts.append(part)
                    continue
                compressed = compress_text(part[key], policy)
                new_parts.append({**part, key: compressed})
            result["parts"] = new_parts

    elif item_type == "FunctionCallOutputItem":
        output = item.get("output")
        if isinstance(output, str):
            value = compress_text(output, policy)
            if policy.max_tool_output_chars > 0:
                value = truncate_smart(value, policy.max_tool_output_chars)
            result["output"] = value
            report.bump("tool_output_truncated", 1 if len(value) < len(output) else 0)

    elif item_type == "ReasoningItem":
        for key in ("text_parts", "summary_parts"):
            values = item.get(key)
            if not isinstance(values, (list, tuple)):
                continue
            compressed_values: List[str] = []
            for value in values:
                if not isinstance(value, str):
                    compressed_values.append(value)
                    continue
                text = compress_text(value, policy)
                if policy.max_reasoning_chars > 0:
                    text = truncate_smart(text, policy.max_reasoning_chars)
                compressed_values.append(text)
            result[key] = compressed_values

    elif item_type in ("ProviderActivityItem", "ProviderOpaqueItem"):
        summary = item.get("display_summary")
        if isinstance(summary, str):
            result["display_summary"] = compress_text(summary, policy)
        details = item.get("details")
        if isinstance(details, (list, tuple)):
            result["details"] = [
                compress_text(entry, policy) if isinstance(entry, str) else entry for entry in details
            ]

    elif item_type == "FunctionCallItem" and policy.compact_tool_args:
        tool_call = item.get("tool_call")
        if isinstance(tool_call, dict) and isinstance(tool_call.get("args"), dict):
            args = dict(tool_call["args"])
            for key, value in list(args.items()):
                if isinstance(value, str) and policy.max_tool_output_chars > 0:
                    args[key] = truncate_smart(value, policy.max_tool_output_chars)
            result["tool_call"] = {**tool_call, "args": args}

    if item_visible_text(result) != before:
        report.compressed_items += 1
        report.bump("text_compressed")
    return result


def _should_drop_internal(item_type: str, policy: OptimizePolicy) -> bool:
    """判断某个内部 Item 类型是否应从旧历史中删除。

    Args:
        item_type: Item 的 ``item_type`` 字符串。
        policy: 优化策略。

    Returns:
        bool: 应删除时返回 True。
    """

    if item_type == "ReasoningItem":
        return policy.drop_old_reasoning
    if item_type in ("ProviderActivityItem", "ProviderOpaqueItem"):
        return policy.drop_old_provider_activity
    return False


def _fingerprint(item: Dict[str, Any]) -> str:
    """生成用于连续去重的内容指纹。

    Args:
        item: 单个 Item 字典。

    Returns:
        str: 类型 + 可见文本组成的指纹；工具 Item 返回空串（不参与去重）。
    """

    item_type = str(item.get("item_type") or "")
    if item_type in _TOOL_PAIR_TYPES or item_type in _INTERNAL_ITEM_TYPES:
        return ""
    # 用户原话（含连续「哈哈哈」这类语气节奏）是拟人效果的一部分，
    # 不参与跨消息去重；只折叠系统/助手侧的机械性重复内容。
    if item_type == "UserMessageItem":
        return ""
    return f"{item_type}\n{item_visible_text(item)}"


def _group_key(item: Dict[str, Any]) -> str:
    """返回预算裁剪时的最小删除单元。

    同一 ``logical_turn_id`` 的工具调用与结果必须一起删，否则 Host 会
    因为「未回答的 function call」或「孤儿 output」拒绝整个改写结果。

    Args:
        item: 单个 Item 字典。

    Returns:
        str: 分组键。
    """

    item_type = item.get("item_type")
    if item_type in _TOOL_PAIR_TYPES:
        turn_id = _meta_turn_id(item)
        if turn_id:
            return f"turn:{turn_id}"
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    return f"item:{meta.get('item_id')}"


def _trim_to_budget(
    head: List[Dict[str, Any]],
    middle: List[Dict[str, Any]],
    tail: List[Dict[str, Any]],
    budget: int,
    report: OptimizeReport,
    *,
    original: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """按预算从最旧的 Item 开始裁剪，按工具轮次分组删除。

    Args:
        head: 系统提示词等不可动的前缀。
        middle: 可裁剪区域。
        tail: 最近消息等不可动的后缀。
        budget: 可见字符预算；小于等于 0 表示不裁剪。
        report: 统计对象。
        original: 改写前的完整 Item 列表，用于关系闭合校验。

    Returns:
        List[Dict[str, Any]]: 裁剪后的中间区域。
    """

    if budget <= 0:
        return middle
    total = measure_items([*head, *middle, *tail]).chars
    if total <= budget:
        return middle

    kept = list(middle)
    index = 0
    while index < len(kept) and total > budget:
        key = _group_key(kept[index])
        group_len = 1
        while index + group_len < len(kept) and _group_key(kept[index + group_len]) == key:
            group_len += 1
        group = kept[index : index + group_len]
        group_chars = measure_items(group).chars
        # 不删除整组工具调用时跨越分组边界的情况由 key 保证，这里只做二次保险
        candidate = [*head, *kept[:index], *kept[index + group_len :], *tail]
        try:
            validate_request_items(candidate, original=original)
        except ItemProtocolError:
            index += group_len
            continue
        kept = kept[:index] + kept[index + group_len :]
        total -= group_chars
        report.removed_items += group_len
        report.bump("budget_trimmed", group_len)
    return kept


def optimize_request_items(
    items: Any,
    policy: OptimizePolicy,
    *,
    request_kind: str = "planner",
    budget: Optional[int] = None,
) -> Tuple[Any, OptimizeReport]:
    """优化一组发给模型的 Context Items。

    Args:
        items: Hook 传入的 ``items``（``list[dict]``）。
        policy: 优化策略。
        request_kind: ``planner`` / ``replyer``，用于选择默认预算。
        budget: 显式字符预算；为 None 时使用策略中的默认值。

    Returns:
        Tuple[Any, OptimizeReport]: (可安全回传的 items, 优化报告)。
        任何异常、协议校验失败或 ``dry_run`` 都会返回原始 items。
    """

    report = OptimizeReport()
    if not policy.enabled or not isinstance(items, list) or not items:
        report.reason = "disabled_or_empty"
        return items, report

    original = items
    before = measure_items(original)
    report.before_chars = before.chars
    effective_budget = policy.budget_for(request_kind) if budget is None else budget

    try:
        work = copy.deepcopy(items)

        system_end = 1 if work and work[0].get("item_type") == "SystemMessageItem" else 0
        head = work[:system_end]
        tail_start = max(system_end, len(work) - max(0, policy.keep_recent_items))
        tail = work[tail_start:]
        middle_source = work[system_end:tail_start]

        # 1) 删除旧内部 Item、压缩文本、折叠连续重复消息
        middle: List[Dict[str, Any]] = []
        previous_fp = ""
        for item in middle_source:
            if not isinstance(item, dict):
                middle.append(item)
                continue
            item_type = str(item.get("item_type") or "")
            if _should_drop_internal(item_type, policy):
                report.removed_items += 1
                report.bump("internal_dropped")
                continue

            compressed = _compress_item(item, policy, report)
            if policy.dedupe_consecutive:
                fp = _fingerprint(compressed)
                if fp and fp == previous_fp:
                    report.removed_items += 1
                    report.bump("duplicate_dropped")
                    continue
                previous_fp = fp
            else:
                previous_fp = ""
            middle.append(compressed)

        # 2) 超预算时按工具轮次从旧到新裁剪
        middle_before_trim = list(middle)
        if policy.enable_history_trim:
            middle = _trim_to_budget(
                head, middle, tail, effective_budget, report, original=original
            )

        candidate = [*head, *middle, *tail]
        # 3) 协议校验：失败则退回「压缩但未裁剪」，再失败则完全回退
        try:
            validate_request_items(candidate, original=original)
        except ItemProtocolError as exc:
            untrimmed = [*head, *middle_before_trim, *tail]
            validate_request_items(untrimmed, original=original)
            candidate = untrimmed
            report.reason = f"budget_trim_reverted: {exc}"
            report.bump("protocol_fallback")

        after = measure_items(candidate)
        report.after_chars = after.chars

        if policy.dry_run:
            report.reason = report.reason or "dry_run"
            return original, report

        # 完全没变化时不回传 modified_kwargs，避免无谓地让 replay 失效
        if candidate == original:
            report.after_chars = before.chars
            report.reason = "no_change"
            return original, report

        report.applied = True
        return candidate, report

    except Exception as exc:  # noqa: BLE001 - 插件必须 fail-open
        report.after_chars = before.chars
        report.applied = False
        report.reason = f"fallback: {type(exc).__name__}: {exc}"
        return original, report


def optimize_output_items(
    items: Any,
    policy: OptimizePolicy,
) -> Tuple[Any, OptimizeReport]:
    """压缩 Planner 本轮输出的 Items（写回历史之前）。

    ``maisaka.planner.after_response`` 的 ``output_items`` 会成为下一轮
    历史的一部分，因此「压缩一次、多轮受益」。这里只压缩文本，不删除、
    不新增 Item，避免触碰 ``MODEL_OUTPUT`` 协议。

    Args:
        items: Hook 传入的 ``output_items``。
        policy: 优化策略。

    Returns:
        Tuple[Any, OptimizeReport]: (可回传的 output_items, 优化报告)。
    """

    report = OptimizeReport()
    if not policy.enabled or not policy.enable_output_condense or not isinstance(items, list) or not items:
        report.reason = "disabled_or_empty"
        return items, report

    before = measure_items(items)
    report.before_chars = before.chars
    try:
        candidate = [_compress_item(item, policy, report) for item in items if isinstance(item, dict)]
        if len(candidate) != len(items):
            raise ItemProtocolError("输出 Item 结构异常")
        after = measure_items(candidate)
        report.after_chars = after.chars
        if policy.dry_run or candidate == items:
            report.reason = "dry_run" if policy.dry_run else "no_change"
            return items, report
        report.applied = True
        return candidate, report
    except Exception as exc:  # noqa: BLE001
        report.after_chars = before.chars
        report.reason = f"fallback: {type(exc).__name__}: {exc}"
        return items, report


def dedupe_extra_prompt(extra_prompt: Any, policy: OptimizePolicy) -> Tuple[Any, int]:
    """对 replyer 的额外提示词做确定性去重。

    Args:
        extra_prompt: 原始 ``extra_prompt`` 字符串。
        policy: 优化策略。

    Returns:
        Tuple[Any, int]: (处理后的值, 节省的字符数)。
    """

    if not policy.enabled or not policy.enable_extra_prompt_dedupe:
        return extra_prompt, 0
    if not isinstance(extra_prompt, str) or not extra_prompt:
        return extra_prompt, 0
    try:
        result = dedupe_requirements(extra_prompt)
        return result, max(0, len(extra_prompt) - len(result))
    except Exception:  # noqa: BLE001
        return extra_prompt, 0
