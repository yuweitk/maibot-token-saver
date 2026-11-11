"""MaiBot Token Saver 插件入口。

目标：在**不影响拟人聊天效果**的前提下，尽最大可能降低 MaiBot 的 token 消耗，
重点是每轮都要跑的 Maisaka Planner。

接入点（全部来自 MaiBot 1.3.x 实际注册的 Hook，见插件目录 ANALYSIS.md）：
    - ``maisaka.planner.before_request``        → 改写发给 Planner 的 items + tool_definitions（主战场）
    - ``maisaka.planner.after_response``        → 压缩本轮输出，避免把冗长推理写进历史
    - ``maisaka.replyer.before_model_request``  → 改写 Replyer 的请求 items
    - ``maisaka.replyer.before_request``        → 去重 extra_prompt、可选简单回复模型降级
    - ``expression.select.before_select``       → 裁剪表达方式选择的聊天上下文
    - ``emoji.maisaka.before_select``           → 裁剪表情选择的上下文与候选数

零侵入：不改 MaiBot 任何源码；停用或删除插件目录即完全回滚。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Host 的 PluginLoader 用 spec_from_file_location 加载 plugin.py，并把 plugins/
# 父目录临时加入 sys.path，但不一定包含插件目录本身。显式注入一次，保证
# 同目录下的 token_saver_core 包在任何加载方式下都能 import。
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from maibot_sdk import Command, HookHandler, MaiBotPlugin  # noqa: E402
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder  # noqa: E402

from token_saver_core.budget import measure_items, measure_tool_definitions  # noqa: E402
from token_saver_core.compressor import (  # noqa: E402
    dedupe_consecutive_lines,
    normalize_whitespace,
    truncate_smart,
)
from token_saver_core.config import TokenSaverConfig, flatten_config_data  # noqa: E402
from token_saver_core.items import (  # noqa: E402
    dedupe_extra_prompt,
    optimize_output_items,
    optimize_request_items,
)
from token_saver_core.policy import OptimizePolicy  # noqa: E402
from token_saver_core.stats import TokenSaverStats  # noqa: E402
from token_saver_core.tools import optimize_tool_definitions  # noqa: E402

PLUGIN_VERSION = "1.0.1"

# 所有 Hook 统一使用 LATE 顺序：等其他插件注入完内容后再压缩，才能覆盖全部冗余。
# error_policy=SKIP 保证本插件抛异常时被跳过而不是中断主流程。
_HOOK_KWARGS: Dict[str, Any] = {
    "mode": HookMode.BLOCKING,
    "order": HookOrder.LATE,
    "error_policy": ErrorPolicy.SKIP,
}


class TokenSaverPlugin(MaiBotPlugin):
    """Token Saver 插件主体。

    插件**不注册任何 Tool**：每多一个工具定义，Planner 每轮都要多携带一份
    schema，与省 token 的目标相反。用户查询统计请用 ``/tokensave`` 命令。
    """

    config_model = TokenSaverConfig

    def __init__(self) -> None:
        """初始化插件实例。"""
        super().__init__()
        self._stats: Optional[TokenSaverStats] = None

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        """插件加载：初始化统计目录与日志。"""
        policy = self._get_policy()
        data_dir: Optional[Path] = None
        try:
            data_dir = Path(self.ctx.paths.data_dir)
        except Exception:  # noqa: BLE001 - 老版本 SDK 可能没有 paths
            data_dir = None
        self._stats = TokenSaverStats(data_dir, enabled=policy.enable_stats)
        self.ctx.logger.info(
            "Token Saver v%s 已加载 | 启用=%s 试运行=%s Planner预算=%s字 Replyer预算=%s字",
            PLUGIN_VERSION, policy.enabled, policy.dry_run,
            policy.planner_char_budget, policy.replyer_char_budget,
        )

    async def on_unload(self) -> None:
        """插件卸载：落盘统计。"""
        if self._stats is not None:
            try:
                await self._stats.flush()
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("Token Saver 统计落盘失败: %s", exc)
        self.ctx.logger.info("Token Saver 已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热更新：只记录日志，策略对象每次请求都会重新读取。

        Args:
            scope: 更新范围（``self`` / ``bot`` / ``model``）。
            config_data: 更新后的配置。
            version: 配置版本。
        """
        if self._stats is not None:
            self._stats.enabled = self._get_policy().enable_stats
        self.ctx.logger.info("Token Saver 配置已更新: scope=%s version=%s", scope, version)

    # ------------------------------------------------------------------ 内部工具

    def _get_policy(self) -> OptimizePolicy:
        """读取当前运行时配置并转换成策略对象。

        Returns:
            OptimizePolicy: 当前策略；配置不可用时回退到保守默认值。
        """
        try:
            # WebUI 为了折叠分组把配置存成 [section] 子表，核心策略层吃的是
            # 扁平字典；展平函数同时兼容旧版全扁平 config.toml。
            raw = flatten_config_data(self.get_plugin_config_data())
            if raw:
                return OptimizePolicy.from_config_mapping(raw)
        except Exception:  # noqa: BLE001
            pass
        return OptimizePolicy()

    @staticmethod
    def _continue(kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """构造 Hook 的「继续」返回值，保留全部原始 kwargs。

        Args:
            kwargs: 当前 Hook 参数。

        Returns:
            Dict[str, Any]: ``{"action": "continue", "modified_kwargs": kwargs}``。
        """
        # 宿主会在派发参数里注入 hook_name；原样退回会导致核心代码把这份 kwargs
        # 再泼给下一个 hook 调用时与位置参数撞车（TypeError: multiple values）。
        clean_kwargs = {k: v for k, v in kwargs.items() if k != "hook_name"}
        return {"action": "continue", "modified_kwargs": clean_kwargs}

    async def _record(
        self,
        hook: str,
        *,
        before_chars: int,
        after_chars: int,
        applied: bool,
        fallback: bool = False,
        actions: Optional[Dict[str, int]] = None,
        verbose: bool = False,
    ) -> None:
        """记录一次 Hook 的用量并按需输出日志。

        Args:
            hook: Hook 名称。
            before_chars: 优化前字符数。
            after_chars: 优化后（或预计）字符数。
            applied: 是否真正改写。
            fallback: 是否发生 fail-open 回退。
            actions: 优化动作命中次数。
            verbose: 是否逐次打印日志。
        """
        if self._stats is not None:
            try:
                await self._stats.record(
                    hook, before_chars=before_chars, after_chars=after_chars,
                    applied=applied, fallback=fallback, actions=actions,
                )
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug("Token Saver 统计记录失败: %s", exc)
        if verbose or fallback:
            saved = max(0, before_chars - after_chars)
            self.ctx.logger.info(
                "Token Saver [%s] %s -> %s 字 (省 %s) applied=%s fallback=%s",
                hook, before_chars, after_chars, saved, applied, fallback,
            )

    def _verbose(self, policy: OptimizePolicy) -> bool:
        """读取详细日志开关。

        Args:
            policy: 当前策略。

        Returns:
            bool: 是否打印逐次日志。
        """
        try:
            return bool(flatten_config_data(self.get_plugin_config_data()).get("verbose_log", False))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _extract_stream_id(kwargs: Dict[str, Any]) -> str:
        """从 Command 的 kwargs 中尽力提取聊天流 ID。

        Args:
            kwargs: 命令处理器参数。

        Returns:
            str: 聊天流 ID，提取不到时为空串。
        """
        for key in ("stream_id", "session_id", "chat_id"):
            value = kwargs.get(key)
            if isinstance(value, str) and value:
                return value
        message = kwargs.get("message")
        if isinstance(message, dict):
            for key in ("session_id", "stream_id", "chat_id"):
                value = message.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    # ------------------------------------------------------------------ Planner

    @HookHandler(
        "maisaka.planner.before_request",
        name="planner_context_saver",
        description="Planner 请求前压缩历史上下文、内部 Item 与工具定义，降低输入 token",
        **_HOOK_KWARGS,
    )
    async def planner_before_request(self, **kwargs: Any) -> Dict[str, Any]:
        """Planner 主优化点：改写 items 与 tool_definitions。

        Args:
            **kwargs: Hook 参数，包含 ``items`` / ``item_schema_version`` /
                ``tool_definitions`` / ``session_id`` 等。

        Returns:
            Dict[str, Any]: Hook 返回值，异常时原样放行。
        """
        policy = self._get_policy()
        if not policy.enabled:
            return self._continue(kwargs)
        try:
            raw_items = kwargs.get("items")
            raw_tools = kwargs.get("tool_definitions")
            before = measure_items(raw_items or []).chars + measure_tool_definitions(raw_tools or [])

            new_items, item_report = optimize_request_items(raw_items, policy, request_kind="planner")
            new_tools, tool_report = optimize_tool_definitions(raw_tools, policy)

            if item_report.applied:
                kwargs["items"] = new_items
            if tool_report.applied:
                kwargs["tool_definitions"] = new_tools
            # item_schema_version 必须原样回传，否则 Host 会拒绝整个改写结果
            # （此处未改动 kwargs 中该字段，天然满足）

            after = (
                measure_items(new_items or []).chars
                + measure_tool_definitions(new_tools or [])
            )
            if policy.dry_run:
                after = before - item_report.saved_chars - tool_report.saved_chars
            actions = dict(item_report.actions)
            if tool_report.slimmed_count:
                actions["tool_desc_slimmed"] = tool_report.slimmed_count
            if tool_report.removed_count:
                actions["tool_definition_removed"] = tool_report.removed_count

            fallback = "fallback" in (item_report.reason + tool_report.reason)
            await self._record(
                "maisaka.planner.before_request",
                before_chars=before, after_chars=after,
                applied=item_report.applied or tool_report.applied,
                fallback=fallback, actions=actions, verbose=self._verbose(policy),
            )
        except Exception as exc:  # noqa: BLE001 - 绝不影响聊天主流程
            self.ctx.logger.warning("Token Saver planner.before_request 失败，已原样放行: %s", exc)
        return self._continue(kwargs)

    @HookHandler(
        "maisaka.planner.after_response",
        name="planner_output_condenser",
        description="压缩 Planner 本轮写入历史的文本内容，一次压缩多轮受益",
        **_HOOK_KWARGS,
    )
    async def planner_after_response(self, **kwargs: Any) -> Dict[str, Any]:
        """压缩 Planner 本轮输出（不删 Item，避免触碰 MODEL_OUTPUT 协议）。

        Args:
            **kwargs: Hook 参数，包含 ``output_items`` 与 token 用量字段。

        Returns:
            Dict[str, Any]: Hook 返回值，异常时原样放行。
        """
        policy = self._get_policy()
        if not policy.enabled:
            return self._continue(kwargs)
        try:
            raw = kwargs.get("output_items")
            before = measure_items(raw or []).chars
            new_items, report = optimize_output_items(raw, policy)
            if report.applied:
                kwargs["output_items"] = new_items
            after = measure_items(new_items or []).chars if not policy.dry_run else before - report.saved_chars
            await self._record(
                "maisaka.planner.after_response",
                before_chars=before, after_chars=after, applied=report.applied,
                fallback="fallback" in report.reason, actions=report.actions,
                verbose=self._verbose(policy),
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("Token Saver planner.after_response 失败，已原样放行: %s", exc)
        return self._continue(kwargs)

    # ------------------------------------------------------------------ Replyer

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="replyer_context_saver",
        description="Replyer 请求前压缩最终发给模型的 Context Items",
        **_HOOK_KWARGS,
    )
    async def replyer_before_model_request(self, **kwargs: Any) -> Dict[str, Any]:
        """压缩 Replyer 的请求上下文。

        Args:
            **kwargs: Hook 参数，包含 ``items`` / ``item_schema_version`` 等。

        Returns:
            Dict[str, Any]: Hook 返回值，异常时原样放行。
        """
        policy = self._get_policy()
        if not policy.enabled:
            return self._continue(kwargs)
        try:
            raw = kwargs.get("items")
            before = measure_items(raw or []).chars
            new_items, report = optimize_request_items(raw, policy, request_kind="replyer")
            if report.applied:
                kwargs["items"] = new_items
            after = measure_items(new_items or []).chars if not policy.dry_run else before - report.saved_chars
            await self._record(
                "maisaka.replyer.before_model_request",
                before_chars=before, after_chars=after, applied=report.applied,
                fallback="fallback" in report.reason, actions=report.actions,
                verbose=self._verbose(policy),
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("Token Saver replyer.before_model_request 失败，已原样放行: %s", exc)
        return self._continue(kwargs)

    @HookHandler(
        "maisaka.replyer.before_request",
        name="replyer_prompt_saver",
        description="去重 replyer 额外提示词，并按需把简单回复路由到低成本模型",
        **_HOOK_KWARGS,
    )
    async def replyer_before_request(self, **kwargs: Any) -> Dict[str, Any]:
        """去重 ``extra_prompt``；可选地把简单回复路由到便宜模型。

        模型降级默认关闭（``simple_reply_model_name`` 为空）。它会降低单条
        成本但可能削弱拟人效果，用户需自行权衡后显式开启。

        Args:
            **kwargs: Hook 参数，含 ``extra_prompt`` / ``model_name`` / ``task_name`` 等。

        Returns:
            Dict[str, Any]: Hook 返回值，异常时原样放行。
        """
        policy = self._get_policy()
        if not policy.enabled:
            return self._continue(kwargs)
        try:
            extra = kwargs.get("extra_prompt")
            before = len(extra) if isinstance(extra, str) else 0
            new_extra, saved = dedupe_extra_prompt(extra, policy)
            if new_extra != extra:
                kwargs["extra_prompt"] = new_extra

            routed = False
            if policy.simple_reply_model_name and policy.simple_reply_max_chars > 0:
                reason = str(kwargs.get("reply_reason") or "")
                tool_args = kwargs.get("reply_tool_args") or {}
                if len(reason) <= policy.simple_reply_max_chars and not tool_args:
                    kwargs["model_name"] = policy.simple_reply_model_name
                    if policy.simple_reply_task_name:
                        kwargs["task_name"] = policy.simple_reply_task_name
                    routed = True

            await self._record(
                "maisaka.replyer.before_request",
                before_chars=before, after_chars=max(0, before - saved),
                applied=(new_extra != extra) or routed,
                actions={"model_downgraded": 1} if routed else {},
                verbose=self._verbose(policy),
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("Token Saver replyer.before_request 失败，已原样放行: %s", exc)
        return self._continue(kwargs)

    # ------------------------------------------------------------------ 其他链路

    @HookHandler(
        "expression.select.before_select",
        name="expression_context_saver",
        description="裁剪表达方式选择的聊天上下文，保留最近内容",
        **_HOOK_KWARGS,
    )
    async def expression_before_select(self, **kwargs: Any) -> Dict[str, Any]:
        """压缩表达方式选择的 ``chat_info``。

        Args:
            **kwargs: Hook 参数，含 ``chat_info`` / ``target_message`` 等。

        Returns:
            Dict[str, Any]: Hook 返回值，异常时原样放行。
        """
        policy = self._get_policy()
        if not policy.enabled or not policy.enable_expression_trim:
            return self._continue(kwargs)
        try:
            text = kwargs.get("chat_info")
            if isinstance(text, str) and text:
                before = len(text)
                value = normalize_whitespace(text)
                value = dedupe_consecutive_lines(value)
                value = truncate_smart(value, policy.max_expression_chat_info_chars, head_ratio=0.3)
                if value != text:
                    kwargs["chat_info"] = value
                await self._record(
                    "expression.select.before_select",
                    before_chars=before, after_chars=len(value),
                    applied=value != text, verbose=self._verbose(policy),
                )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("Token Saver expression.before_select 失败，已原样放行: %s", exc)
        return self._continue(kwargs)

    @HookHandler(
        "emoji.maisaka.before_select",
        name="emoji_context_saver",
        description="裁剪表情选择的上下文条数与候选采样数",
        **_HOOK_KWARGS,
    )
    async def emoji_before_select(self, **kwargs: Any) -> Dict[str, Any]:
        """限制表情选择的上下文条数与候选数量。

        Args:
            **kwargs: Hook 参数，含 ``context_texts`` / ``sample_size`` / ``reasoning``。

        Returns:
            Dict[str, Any]: Hook 返回值，异常时原样放行。
        """
        policy = self._get_policy()
        if not policy.enabled or not policy.enable_emoji_trim:
            return self._continue(kwargs)
        try:
            texts = kwargs.get("context_texts")
            before = sum(len(entry) for entry in texts) if isinstance(texts, list) else 0
            applied = False
            if isinstance(texts, list) and len(texts) > policy.max_emoji_context_texts:
                kwargs["context_texts"] = texts[-policy.max_emoji_context_texts :]
                applied = True
            sample_size = kwargs.get("sample_size")
            if (
                policy.max_emoji_sample_size > 0
                and isinstance(sample_size, int)
                and sample_size > policy.max_emoji_sample_size
            ):
                kwargs["sample_size"] = policy.max_emoji_sample_size
                applied = True
            reasoning = kwargs.get("reasoning")
            if isinstance(reasoning, str) and len(reasoning) > 300:
                kwargs["reasoning"] = truncate_smart(reasoning, 300, head_ratio=0.7)
                applied = True
            new_texts = kwargs.get("context_texts")
            after = sum(len(entry) for entry in new_texts) if isinstance(new_texts, list) else before
            await self._record(
                "emoji.maisaka.before_select",
                before_chars=before, after_chars=after, applied=applied,
                verbose=self._verbose(policy),
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("Token Saver emoji.before_select 失败，已原样放行: %s", exc)
        return self._continue(kwargs)

    # ------------------------------------------------------------------ 命令

    @Command(
        "tokensave",
        description="查看 Token Saver 的 token 节省统计",
        pattern=r"^/tokensave\s*$",
    )
    async def cmd_tokensave(self, **kwargs: Any):
        """输出当前节省统计。

        Args:
            **kwargs: 命令参数，尽力从中提取 ``stream_id``。

        Returns:
            tuple: ``(True, 日志文本, 2)``，与 SDK Command 约定一致。
        """
        summary = (
            self._stats.summary(plugin_version=PLUGIN_VERSION)
            if self._stats is not None else "Token Saver 统计尚未初始化"
        )
        stream_id = self._extract_stream_id(kwargs)
        if stream_id:
            try:
                await self.ctx.send.text(summary, stream_id)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("Token Saver 发送统计失败: %s", exc)
        else:
            self.ctx.logger.info("Token Saver 统计:\n%s", summary)
        return True, "tokensave report", 2

    @Command(
        "tokensave_reset",
        description="重置 Token Saver 的节省统计",
        pattern=r"^/tokensave\s+reset\s*$",
    )
    async def cmd_tokensave_reset(self, **kwargs: Any):
        """清空统计。

        Args:
            **kwargs: 命令参数。

        Returns:
            tuple: ``(True, 日志文本, 2)``。
        """
        if self._stats is not None:
            await self._stats.reset()
        stream_id = self._extract_stream_id(kwargs)
        if stream_id:
            try:
                await self.ctx.send.text("Token Saver 统计已重置。", stream_id)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("Token Saver 发送重置结果失败: %s", exc)
        return True, "tokensave reset", 2


def create_plugin() -> TokenSaverPlugin:
    """插件工厂函数，Runner 通过它创建插件实例。

    Returns:
        TokenSaverPlugin: 插件实例。
    """
    return TokenSaverPlugin()
