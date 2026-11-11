"""确定性、低损的文本压缩原语。

这里**不做摘要、不改写语义、不调用任何模型**：所有函数都是纯字符串变换，
保证相同输入永远得到相同输出。这一点对 prompt prefix cache 至关重要——
如果压缩结果随轮次漂移，DeepSeek 的上下文缓存会从头失效，省下的 token
还不够赔偿缓存未命中的损失。

拟人效果保护：
    - 不动用户原话的用词与语气，只删「空白」和「完全重复」的内容；
    - 截断工具结果/内部推理时保留头尾，因为结论常出现在末尾；
    - 系统提示词、最近 N 条消息默认完全不经过本模块。
"""

from __future__ import annotations

import json
import re
from typing import List, Optional

from .policy import OptimizePolicy

# 3 个及以上连续换行（允许中间有空白）折叠为 2 个
_MULTI_BLANK_LINES = re.compile(r"(?:[ \t]*\n){3,}")
_TRAILING_SPACES = re.compile(r"[ \t]+\n")
# 只折叠「紧跟在非空白字符之后」的连续空格/制表，保留行首缩进，
# 避免把 prompt 里的 Markdown 嵌套列表或对齐结构压平。
_MULTI_SPACES = re.compile(r"(?<=\S)[ \t]{2,}")
_TOOL_LIST_LINE = re.compile(r"^(\s*\d+\.\s*[^\s:：]+)\s*[:：]\s*(.+)$")
_FENCED_JSON = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def normalize_whitespace(text: str) -> str:
    """折叠冗余空白，保持换行结构与缩进层级不变。

    只处理四类无语义空白：全角空格、行尾空格、连续空行、行内连续空格/制表。
    不会把单个换行合并成空格，也不会折叠行首缩进，因此 Markdown 列表、
    XML 标签、代码块结构都不会被破坏。

    Args:
        text: 原始文本。

    Returns:
        str: 折叠后的文本。
    """

    if not text:
        return text
    result = text.replace("\u3000", " ")
    result = _TRAILING_SPACES.sub("\n", result)
    result = _MULTI_BLANK_LINES.sub("\n\n", result)
    result = _MULTI_SPACES.sub(" ", result)
    return result


def dedupe_consecutive_lines(text: str) -> str:
    """删除连续出现的完全重复行。

    记忆注入、工具结果、system-reminder 常把同一段内容连续重复多遍，
    这是最安全的一类压缩：删除后语义完全不变。

    Args:
        text: 原始文本。

    Returns:
        str: 去重后的文本。
    """

    if not text:
        return text
    output: List[str] = []
    previous: Optional[str] = None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and stripped == previous:
            continue
        output.append(line)
        previous = stripped if stripped else None
    return "\n".join(output)


def compact_json_block(match: re.Match) -> str:
    """压缩一个 Markdown JSON 代码块。

    Args:
        match: ``_FENCED_JSON`` 的匹配对象。

    Returns:
        str: 压缩后的代码块；内容不是合法 JSON 时原样返回。
    """

    raw = match.group(1)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return match.group(0)
    compact = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if len(compact) >= len(raw):
        return match.group(0)
    return f"```json\n{compact}\n```"


def compact_json_text(text: str) -> str:
    """压缩整段 JSON 或文本内的 JSON 代码块。

    Args:
        text: 原始文本。

    Returns:
        str: 压缩后的文本；非 JSON 内容原样返回。
    """

    if not text:
        return text
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
            compact = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
            if len(compact) < len(stripped):
                return text.replace(stripped, compact, 1)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return _FENCED_JSON.sub(compact_json_block, text)


def truncate_smart(text: str, limit: int, *, head_ratio: float = 0.7) -> str:
    """超长文本保留头尾、省略中间。

    工具结果与检索结论往往「开头是结构、结尾是答案」，只留开头会丢结论。
    省略标记里带上原始长度，方便模型判断信息被裁掉多少，也方便排查。

    Args:
        text: 原始文本。
        limit: 允许保留的最大字符数；小于等于 0 表示不限制。
        head_ratio: 头部占比，取值 (0, 1)。

    Returns:
        str: 截断后的文本；未超限时原样返回。
    """

    if limit <= 0 or len(text) <= limit:
        return text
    marker = f"\n…[已省略中间 {len(text) - limit} 字]…\n"
    available = max(0, limit - len(marker))
    head = max(1, int(available * head_ratio))
    tail = max(0, available - head)
    if tail == 0:
        return text[:head] + marker
    return text[:head] + marker + text[-tail:]


def compact_deferred_tools_reminder(text: str, max_desc_chars: int) -> str:
    """压缩 planner 每轮注入的 deferred tools 清单描述。

    源码 ``src/maisaka/runtime.py::build_deferred_tools_reminder`` 会把所有
    未发现工具按 ``N. name: description`` 逐条注入。装了大量 MCP 工具时，
    这段清单每轮都重复占用几百到几千字符。这里只截断单条描述、保留工具名
    与序号——模型仍能用 ``tool_search`` 发现完整信息，功能不受影响。

    Args:
        text: 注入消息全文。
        max_desc_chars: 单条描述允许的最大字符数；0 表示不处理。

    Returns:
        str: 压缩后的文本；不是 deferred 清单时原样返回。
    """

    if max_desc_chars <= 0 or "<system-reminder>" not in text:
        return text
    output: List[str] = []
    changed = False
    for line in text.split("\n"):
        match = _TOOL_LIST_LINE.match(line)
        if not match:
            output.append(line)
            continue
        prefix, description = match.group(1), match.group(2)
        if len(description) > max_desc_chars:
            description = description[:max_desc_chars].rstrip() + "…"
            changed = True
        output.append(f"{prefix}: {description}")
    return "\n".join(output) if changed else text


def compress_text(text: str, policy: OptimizePolicy) -> str:
    """对单段文本执行全部已启用的低损压缩。

    Args:
        text: 原始文本。
        policy: 优化策略。

    Returns:
        str: 压缩后的文本。任何一步异常都会退回上一步结果，绝不抛错。
    """

    if not text or not policy.enable_text_compression:
        return text
    result = text
    try:
        if policy.collapse_whitespace:
            result = normalize_whitespace(result)
        if policy.dedupe_lines:
            result = dedupe_consecutive_lines(result)
        if policy.compact_json:
            result = compact_json_text(result)
        # deferred 清单的裁剪独立于总开关，因为它属于结构性冗余
        result = compact_deferred_tools_reminder(result, policy.max_deferred_tool_desc_chars)
        result = result.strip()
    except Exception:
        return text
    # 压缩后变空说明原文只有空白，保留一个空格避免 ContextTextPart 校验失败
    return result or (text[:1] if text else text)


def dedupe_requirements(text: str) -> str:
    """对 replyer 的 ``extra_prompt`` 去除重复要求段落。

    多个回复扩展可能追加语义相近的指令。这里只删除**完全相同**的段落，
    不做模糊合并，避免改变回复风格。

    Args:
        text: 原始额外提示词。

    Returns:
        str: 去重后的文本。
    """

    if not text:
        return text
    seen: set[str] = set()
    output: List[str] = []
    for block in re.split(r"\n+", text):
        normalized = block.strip()
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return "\n".join(output)
