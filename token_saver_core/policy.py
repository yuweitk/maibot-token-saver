"""Token Saver 的优化策略对象。

本模块刻意不依赖 ``maibot_sdk``，使核心压缩逻辑可以在纯 Python 环境下被
pytest 直接测试。运行时的强类型配置（``config.py``）负责把用户配置转换成
这里的 :class:`OptimizePolicy`。

设计约束（重要）：
    所有压缩都必须是**确定性纯函数**——相同输入永远得到相同输出。
    这样连续多轮 Planner 请求的前缀才会保持稳定，DeepSeek / OpenAI 的
    prompt prefix cache 才能持续命中；任何带时间戳、随机数或计数器的
    改写都会让缓存失效，反而更费钱。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True, slots=True)
class OptimizePolicy:
    """一次优化所需的全部策略参数（不可变，避免跨请求状态污染）。

    Attributes:
        enabled: 总开关；关闭时所有 Hook 直接原样放行。
        dry_run: 只统计预计节省量，不改写任何数据，用于 A/B 验收。
        planner_char_budget: Planner 请求的可见文本字符预算。
        replyer_char_budget: Replyer 请求的可见文本字符预算。
        keep_recent_items: 请求尾部完全不动的 Item 数量，保护当下对话语感。
        keep_system_untouched: 是否保持系统提示词原样（拟人效果的核心）。
        enable_text_compression: 是否启用文本无损压缩。
        collapse_whitespace: 折叠多余空白、连续空行、行尾空格。
        dedupe_lines: 删除连续重复的段落/行（记忆与工具结果高频重复）。
        compact_json: 压缩整段 JSON 的缩进与冗余空白。
        max_tool_output_chars: 单条历史工具结果保留的最大字符数（0 = 不限制）。
        max_reasoning_chars: 单条推理文本保留的最大字符数（0 = 不限制）。
        max_deferred_tool_desc_chars: deferred tools 清单中单条描述的上限。
        enable_history_trim: 是否允许按预算裁剪旧历史。
        drop_old_reasoning: 是否删除旧的内部推理 Item。
        drop_old_provider_activity: 是否删除旧的 Provider 活动/不透明 Item。
        dedupe_consecutive: 是否折叠连续重复的消息文本。
        compact_tool_args: 是否压缩历史工具调用的 args（高风险，默认关）。
        enable_tool_slimming: 是否精简工具定义描述。
        max_tool_description_chars: 单条工具描述的最大字符数（0 = 不限制）。
        aggressive_tool_pruning: 是否按白名单裁剪整条工具定义（默认关）。
        max_tool_definitions: 激进模式下保留的最大工具数。
        tool_keep_names: 永不裁剪的工具名（逗号分隔的配置会被拆成元组）。
        enable_output_condense: 是否压缩 Planner 本轮输出（写入历史前）。
        enable_extra_prompt_dedupe: 是否对 replyer extra_prompt 去重。
        simple_reply_model_name: 简单回复路由到的具体模型名，空 = 不启用。
        simple_reply_task_name: 简单回复路由到的任务名。
        simple_reply_max_chars: 判定「简单回复」的目标消息字符阈值。
        enable_expression_trim: 是否裁剪表达方式选择的上下文。
        max_expression_chat_info_chars: 表达方式选择上下文字符上限。
        enable_emoji_trim: 是否裁剪表情选择的上下文。
        max_emoji_context_texts: 表情选择保留的最近上下文条数。
        max_emoji_sample_size: 表情候选采样数上限（0 = 不限制）。
        enable_stats: 是否记录用量统计。
    """

    enabled: bool = True
    dry_run: bool = False

    planner_char_budget: int = 26000
    replyer_char_budget: int = 16000
    keep_recent_items: int = 14
    keep_system_untouched: bool = True

    enable_text_compression: bool = True
    collapse_whitespace: bool = True
    dedupe_lines: bool = True
    compact_json: bool = True
    max_tool_output_chars: int = 1200
    max_reasoning_chars: int = 700
    max_deferred_tool_desc_chars: int = 64

    enable_history_trim: bool = True
    drop_old_reasoning: bool = True
    drop_old_provider_activity: bool = True
    dedupe_consecutive: bool = True
    compact_tool_args: bool = False

    enable_tool_slimming: bool = True
    max_tool_description_chars: int = 360
    aggressive_tool_pruning: bool = False
    max_tool_definitions: int = 24
    tool_keep_names: Tuple[str, ...] = field(
        default_factory=lambda: (
            "reply", "stop", "wait", "tool_search", "query_memory",
            "read_tool_result", "send_emoji", "view_forward_message",
        )
    )

    enable_output_condense: bool = True
    enable_extra_prompt_dedupe: bool = True
    simple_reply_model_name: str = ""
    simple_reply_task_name: str = "utils"
    simple_reply_max_chars: int = 40

    enable_expression_trim: bool = True
    max_expression_chat_info_chars: int = 2600
    enable_emoji_trim: bool = True
    max_emoji_context_texts: int = 8
    max_emoji_sample_size: int = 12

    enable_stats: bool = True

    def budget_for(self, request_kind: str) -> int:
        """返回对应请求链路的字符预算。

        Args:
            request_kind: ``planner`` / ``replyer`` / 其他自定义名称。

        Returns:
            int: 该链路允许的最大可见文本字符数；非正数表示不限制。
        """

        if request_kind == "planner":
            return self.planner_char_budget
        if request_kind == "replyer":
            return self.replyer_char_budget
        return min(self.planner_char_budget, self.replyer_char_budget)

    @classmethod
    def from_config_mapping(cls, raw: dict) -> "OptimizePolicy":
        """从插件配置字典构造策略对象，忽略未知键并做基本类型收敛。

        Args:
            raw: ``self.get_plugin_config_data()`` 或 ``self.config.model_dump()``
                返回的原始配置字典。

        Returns:
            OptimizePolicy: 可直接传给优化函数的策略实例。

        Raises:
            TypeError: ``raw`` 不是字典时抛出。
        """

        if not isinstance(raw, dict):
            raise TypeError("配置数据必须是字典")
        known = {f.name for f in cls.__dataclass_fields__.values()}
        cleaned: dict = {}
        for key, value in raw.items():
            if key not in known:
                continue
            if key == "tool_keep_names":
                if isinstance(value, str):
                    cleaned[key] = tuple(
                        part.strip() for part in value.split(",") if part.strip()
                    )
                elif isinstance(value, (list, tuple)):
                    cleaned[key] = tuple(str(part).strip() for part in value if str(part).strip())
                continue
            cleaned[key] = value
        return cls(**cleaned)
