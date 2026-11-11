"""插件运行时配置模型（由 Runner 自动生成 config.toml 与 WebUI 表单）。

字段名与 :class:`token_saver_core.policy.OptimizePolicy` 一一对应；
``to_policy()`` 负责把用户配置转换成核心逻辑使用的不可变策略对象。

.. warning::
    MaiBot 有两套**互不相通**的配置表单生成器，切勿混用写法：

    * 主程序 ``src/config`` 支持 ``{"label": {"zh_CN": "..."}}`` 字典；
    * 插件 SDK ``maibot_sdk.config._build_field_schema`` 却写死了
      ``"label": str(json_extra.get("label") or field_name)``，字典会被
      Python ``str()`` 成 ``"{'zh_CN': '...'}"`` 直接显示在界面上。

    因此插件侧 ``label`` **必须是纯字符串**，多语言要放进 ``i18n`` 键。
    另外前端 ``FieldRenderer`` 只渲染 ``hint``，不渲染 ``description``，
    所以面向用户的说明统一写进 ``hint``（``description`` 仅作 TOML/兼容保留）。
    SDK 插件表单不支持 ``advanced`` 折叠，分类请用嵌套配置节实现。
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from maibot_sdk import Field, PluginConfigBase
from pydantic import model_validator

from .policy import OptimizePolicy


def _ui(
    label: str,
    hint: str,
    *,
    ge: int | None = None,
    le: int | None = None,
    widget: str = "",
    placeholder: str = "",
    hidden: bool = False,
) -> Dict[str, Any]:
    """构造一组同时兼容 SDK schema 生成器与 WebUI 的 ``Field`` 关键字参数。

    Args:
        label: 纯字符串中文标签（不能是字典，否则界面会显示 Python repr）。
        hint: 显示在控件下方的说明文字（前端只渲染 hint）。
        ge: 数值下界，可选。
        le: 数值上界，可选。
        widget: ``x-widget`` 控件类型，留空则由字段类型自动推导。
        placeholder: 输入框占位文本。
        hidden: 是否从 WebUI 隐藏该字段。

    Returns:
        Dict[str, Any]: 可直接 ``**`` 展开进 :func:`Field` 的关键字参数。
    """

    extra: Dict[str, Any] = {"label": label, "hint": hint}
    if widget:
        extra["x-widget"] = widget
    if placeholder:
        extra["placeholder"] = placeholder
    if hidden:
        extra["hidden"] = True

    kwargs: Dict[str, Any] = {
        "description": hint,
        "json_schema_extra": extra,
    }
    if ge is not None:
        kwargs["ge"] = ge
    if le is not None:
        kwargs["le"] = le
    return kwargs


# --------------------------------------------------------------------------- 配置分区


class CompressionSection(PluginConfigBase):
    """低损文本压缩：只删冗余空白与重复行，不改动任何用词。"""

    __ui_label__ = "文本压缩"
    __ui_order__ = 10

    enable_text_compression: bool = Field(
        default=True,
        **_ui("启用文本无损压缩", "启用低损文本压缩（空白折叠、连续重复行去重、JSON 紧凑化）。"),
    )
    collapse_whitespace: bool = Field(
        default=True,
        **_ui("折叠冗余空白", "折叠连续空行、行尾空格与重复空格，不改动用词。"),
    )
    dedupe_lines: bool = Field(
        default=True,
        **_ui("去除连续重复行", "删除连续出现的完全重复行。"),
    )
    compact_json: bool = Field(
        default=True,
        **_ui("紧凑化 JSON", "压缩整段 JSON 或 JSON 代码块的缩进。"),
    )
    max_tool_output_chars: int = Field(
        default=1200, ge=0, le=20000,
        **_ui("历史工具结果字符上限", "旧历史中单条工具结果保留的最大字符数（保留头尾）。0 = 不限制。"),
    )
    max_reasoning_chars: int = Field(
        default=700, ge=0, le=20000,
        **_ui("旧推理文本字符上限", "保留的旧推理文本字符上限。0 = 不限制。"),
    )
    max_deferred_tool_desc_chars: int = Field(
        default=64, ge=0, le=500,
        **_ui("deferred tools 描述上限", "deferred tools 清单中单条工具描述的最大字符数。0 = 不处理。"),
    )


class HistorySection(PluginConfigBase):
    """历史裁剪：按字符预算从最旧的整轮工具交互开始删除。"""

    __ui_label__ = "历史裁剪"
    __ui_order__ = 20

    enable_history_trim: bool = Field(
        default=True,
        **_ui("启用历史预算裁剪", "超出字符预算时按工具轮次从旧到新裁剪历史。"),
    )
    drop_old_reasoning: bool = Field(
        default=True,
        **_ui("删除旧内部推理", "删除旧历史中的内部推理 Item（官方压缩逻辑同样忽略它们）。"),
    )
    drop_old_provider_activity: bool = Field(
        default=True,
        **_ui("删除旧 Provider 活动", "删除旧历史中的 Provider 活动/不透明摘要 Item。"),
    )
    dedupe_consecutive: bool = Field(
        default=True,
        **_ui("折叠连续重复消息", "折叠连续重复的普通消息文本；工具与内部结构不参与去重。"),
    )
    compact_tool_args: bool = Field(
        default=False,
        **_ui("压缩历史工具参数（高风险）", "压缩历史工具调用参数中的超长字符串。可能影响回放，默认关闭。"),
    )


class ToolsSection(PluginConfigBase):
    """工具定义精简：缩短每轮都要重复携带的 tool schema 描述。"""

    __ui_label__ = "工具定义精简"
    __ui_order__ = 30

    enable_tool_slimming: bool = Field(
        default=True,
        **_ui("精简工具定义", "精简工具定义的冗长描述与展示性 Schema 字段。默认不删除任何工具。"),
    )
    max_tool_description_chars: int = Field(
        default=360, ge=0, le=5000,
        **_ui("工具描述字符上限", "单条工具描述的最大字符数（保留头尾）。0 = 不限制。"),
    )
    aggressive_tool_pruning: bool = Field(
        default=False,
        **_ui("激进裁剪工具（不推荐）", "工具数超限时按白名单裁剪整条工具定义。可能导致 Planner 无法调用某些工具，默认关闭。"),
    )
    max_tool_definitions: int = Field(
        default=24, ge=4, le=200,
        **_ui("激进模式工具数上限", "激进模式下保留的最大工具定义数量。"),
    )
    tool_keep_names: str = Field(
        default="reply,stop,wait,tool_search,query_memory,read_tool_result,send_emoji,view_forward_message",
        **_ui(
            "工具白名单",
            "激进模式下永不裁剪的工具名，英文逗号分隔。",
            widget="textarea",
            placeholder="reply,stop,wait,tool_search",
        ),
    )


class OutputSection(PluginConfigBase):
    """输出压缩与模型路由：一次压缩多轮受益；路由属于省钱不省 token。"""

    __ui_label__ = "输出与模型路由"
    __ui_order__ = 40

    enable_output_condense: bool = Field(
        default=True,
        **_ui("压缩本轮输出", "压缩 Planner 本轮输出（写入历史前），一次压缩多轮受益。"),
    )
    enable_extra_prompt_dedupe: bool = Field(
        default=True,
        **_ui("去重额外提示词", "去除 replyer 额外提示词中完全重复的要求段落。"),
    )
    simple_reply_model_name: str = Field(
        default="",
        **_ui(
            "简单回复降级模型名",
            "可选：简单短回复路由到的具体模型名（需存在于 model_config.toml）。留空 = 不启用。",
            placeholder="留空即不启用模型路由",
        ),
    )
    simple_reply_task_name: str = Field(
        default="utils",
        **_ui("简单回复降级任务名", "简单回复路由到的模型任务名。"),
    )
    simple_reply_max_chars: int = Field(
        default=40, ge=0, le=2000,
        **_ui("简单回复判定阈值", "目标消息不超过该字符数且无引用/图片时判定为简单回复。0 = 不启用。"),
    )


class AuxiliarySection(PluginConfigBase):
    """辅助链路与统计：表达方式、表情选择及用量观测。"""

    __ui_label__ = "辅助链路与统计"
    __ui_order__ = 50

    enable_expression_trim: bool = Field(
        default=True,
        **_ui("裁剪表达方式上下文", "裁剪表达方式选择链路的聊天上下文。"),
    )
    max_expression_chat_info_chars: int = Field(
        default=2600, ge=500, le=50000,
        **_ui("表达方式上下文上限", "表达方式选择上下文保留的最大字符数（保留尾部最近内容）。"),
    )
    enable_emoji_trim: bool = Field(
        default=True,
        **_ui("裁剪表情选择上下文", "裁剪表情选择链路的上下文条数与候选采样数。"),
    )
    max_emoji_context_texts: int = Field(
        default=8, ge=1, le=100,
        **_ui("表情上下文条数", "表情选择保留的最近上下文文本条数。"),
    )
    max_emoji_sample_size: int = Field(
        default=12, ge=0, le=200,
        **_ui("表情候选采样上限", "表情候选采样数上限。0 = 不限制。"),
    )
    enable_stats: bool = Field(
        default=True,
        **_ui("启用用量统计", "记录各链路的字符节省统计，可用 /tokensave 查看。"),
    )
    verbose_log: bool = Field(
        default=False,
        **_ui("详细日志", "逐次请求打印节省日志。默认关闭以免刷屏。"),
    )


# 分区名 -> 该分区包含的根级扁平键。既是迁移映射，也是展平白名单。
_SECTION_MODELS: Dict[str, type[PluginConfigBase]] = {
    "compression": CompressionSection,
    "history": HistorySection,
    "tools": ToolsSection,
    "output": OutputSection,
    "auxiliary": AuxiliarySection,
}
_SECTION_FIELD_MAP: Dict[str, Tuple[str, ...]] = {
    name: tuple(model.model_fields) for name, model in _SECTION_MODELS.items()
}


def flatten_config_data(raw: Mapping[str, Any] | None) -> Dict[str, Any]:
    """把分区后的配置字典展平成核心策略层使用的扁平字典。

    同时兼容三种输入：新版分区结构、旧版全扁平结构、以及两者混合
    （Host 保存 patch 时可能残留旧根级键）。分区内的值优先于根级残留。

    Args:
        raw: ``get_plugin_config_data()`` 返回的运行时配置。

    Returns:
        Dict[str, Any]: 展平后的配置；非字典输入返回空字典。
    """

    if not isinstance(raw, Mapping):
        return {}
    flat: Dict[str, Any] = {
        key: value for key, value in raw.items() if key not in _SECTION_FIELD_MAP
    }
    for section_name, field_names in _SECTION_FIELD_MAP.items():
        section = raw.get(section_name)
        if not isinstance(section, Mapping):
            continue
        flat.update({key: value for key, value in section.items() if key in field_names})
    return flat


# --------------------------------------------------------------------------- 根模型


class TokenSaverConfig(PluginConfigBase):
    """Token Saver 插件配置。默认值为「保守档」：只动旧历史与内部结构。"""

    # SDK 2.21+ 要求配置字典带 [plugin] config_version 节，否则 Runner
    # 生成默认 config.toml 时会拒绝加载。保持普通 dict 以兼容 Host 对
    # 「虚构 general 节」中 plugin 保留节的复制逻辑；hidden 让它不出现在 UI。
    plugin: Dict[str, Any] = Field(
        default_factory=lambda: {"config_version": "1"},
        **_ui("插件配置元数据", "SDK 要求的 [plugin] config_version，请勿修改。", hidden=True),
    )

    # 以下 6 项最常用，保留在根级，由 SDK 生成「通用设置」分区。
    enabled: bool = Field(
        default=True,
        **_ui("启用 Token 优化", "总开关。关闭后所有 Hook 原样放行，可随时一键回滚。"),
    )
    dry_run: bool = Field(
        default=False,
        **_ui("试运行（只统计不改写）", "只统计预计节省量、不改写任何数据，用于 A/B 对比验收。"),
    )
    planner_char_budget: int = Field(
        default=26000, ge=4000, le=200000,
        **_ui("Planner 字符预算", "Planner 单轮请求的可见文本字符预算。超出时从最旧历史开始裁剪。"),
    )
    replyer_char_budget: int = Field(
        default=16000, ge=4000, le=200000,
        **_ui("Replyer 字符预算", "Replyer 单轮请求的可见文本字符预算。"),
    )
    keep_recent_items: int = Field(
        default=14, ge=4, le=100,
        **_ui("保护最近 N 条消息", "请求尾部完全不改动的消息条数。越大越保护当下对话语感，省得越少。"),
    )
    keep_system_untouched: bool = Field(
        default=True,
        **_ui("不改动系统提示词", "保持系统提示词（人设、风格、规则）完全不动。强烈建议开启。"),
    )

    compression: CompressionSection = Field(default_factory=CompressionSection)
    history: HistorySection = Field(default_factory=HistorySection)
    tools: ToolsSection = Field(default_factory=ToolsSection)
    output: OutputSection = Field(default_factory=OutputSection)
    auxiliary: AuxiliarySection = Field(default_factory=AuxiliarySection)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_flat_config(cls, data: Any) -> Any:
        """把 v1.0.0 的扁平 config.toml 自动迁移到分区结构。

        老配置里所有键都在根级；这里按 :data:`_SECTION_FIELD_MAP` 把它们
        收进对应分区，用户无需手动改文件，也不会丢失已调好的参数。

        Args:
            data: Pydantic 校验前的原始配置数据。

        Returns:
            Any: 迁移后的配置数据；非字典输入原样返回。
        """

        if data is None:
            return {}
        if not isinstance(data, Mapping):
            return data
        merged: Dict[str, Any] = dict(data)
        for section_name, field_names in _SECTION_FIELD_MAP.items():
            existing = merged.get(section_name)
            section: Dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}
            for key in field_names:
                if key in merged:
                    if key not in section:
                        section[key] = merged[key]
                    merged.pop(key)
            if section:
                merged[section_name] = section
        return merged

    def to_flat_mapping(self) -> Dict[str, Any]:
        """导出扁平配置字典（供核心策略层消费）。

        Returns:
            Dict[str, Any]: 展平后的配置。
        """

        return flatten_config_data(self.model_dump())

    def to_policy(self) -> OptimizePolicy:
        """把运行时配置转换成核心策略对象。

        Returns:
            OptimizePolicy: 不可变策略实例。
        """

        return OptimizePolicy.from_config_mapping(self.to_flat_mapping())
