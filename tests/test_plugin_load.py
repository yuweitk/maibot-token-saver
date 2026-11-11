"""插件在真实 SDK 环境下的加载与 Hook 冒烟测试。

需要 ``maibot-plugin-sdk``；未安装时自动跳过，不影响纯逻辑单测。
在插件目录执行 ``python -m pytest tests/ -q`` 即可。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("maibot_sdk")

from maibot_sdk.context import PluginContext, PluginPaths  # noqa: E402

from fixtures import make_conversation, make_tool_definitions  # noqa: E402
from token_saver_core.items import validate_request_items  # noqa: E402

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_module():
    """按 Host 相同方式动态加载 plugin.py。

    Returns:
        module: 已执行的插件入口模块。
    """
    for path in (str(_PLUGIN_ROOT), str(_PLUGIN_ROOT / "tests")):
        if path not in sys.path:
            sys.path.insert(0, path)
    name = "ts_plugin_integration_test"
    spec = importlib.util.spec_from_file_location(
        name, str(_PLUGIN_ROOT / "plugin.py"), submodule_search_locations=[str(_PLUGIN_ROOT)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def plugin_instance(tmp_path):
    """构造一个已注入上下文与默认配置的插件实例。"""
    module = _load_plugin_module()
    instance = module.create_plugin()
    context = PluginContext(
        "com.yuwei.maibot-token-saver",
        rpc_call=None,
        paths=PluginPaths(data_dir=tmp_path / "data", runtime_dir=tmp_path / "temp"),
    )
    instance._set_context(context)
    # Runner 会用 build_default_config() 生成 config.toml；SDK 2.21+ 要求带
    # [plugin] config_version 节，本插件的配置模型已内置。
    instance.set_plugin_config(instance.build_default_config())
    asyncio.run(instance.on_load())
    yield instance
    asyncio.run(instance.on_unload())


def test_components_registered(plugin_instance) -> None:
    """必须注册 6 个 Hook 与 2 个 Command，且没有额外 Tool。"""
    components = plugin_instance.get_components()
    types = [str(component.get("type", "")).upper() for component in components]
    assert types.count("HOOK_HANDLER") == 6
    assert types.count("COMMAND") == 2
    assert "TOOL" not in types, "注册 Tool 会增加 Planner 每轮的 schema 开销"


def test_lifecycle_methods_implemented(plugin_instance) -> None:
    """三个生命周期方法必须被子类实现。"""
    from maibot_sdk import MaiBotPlugin

    for method in ("on_load", "on_unload", "on_config_update"):
        assert getattr(type(plugin_instance), method) is not getattr(MaiBotPlugin, method)


def test_planner_hook_returns_protocol_safe_items(plugin_instance) -> None:
    """Planner Hook 的改写结果必须能被 Host 安全反序列化。"""
    items = make_conversation(tool_turns=2)
    tools = make_tool_definitions(3)
    kwargs = {
        "items": items,
        "item_schema_version": 1,
        "tool_definitions": tools,
        "selected_history_count": 10,
        "built_message_count": len(items),
        "selection_reason": "test",
        "session_id": "session-1",
    }
    result = asyncio.run(plugin_instance.planner_before_request(**kwargs))
    assert result["action"] == "continue"
    modified = result["modified_kwargs"]
    assert set(kwargs) <= set(modified), "不能丢失原始 kwargs 字段"
    assert modified["item_schema_version"] == 1
    validate_request_items(modified["items"], original=items)
    assert len(modified["tool_definitions"]) == len(tools), "默认不应删除工具"
    assert len(modified["items"]) < len(items)


def test_all_hooks_are_fail_open(plugin_instance) -> None:
    """任何 Hook 遇到异常输入都不能抛错。"""
    hooks = [
        ("planner_after_response", {"output_items": [{"broken": True}], "item_schema_version": 1}),
        ("replyer_before_model_request", {"items": [{"broken": True}], "item_schema_version": 1}),
        ("replyer_before_request", {"extra_prompt": "a\na", "reply_reason": "hi"}),
        ("expression_before_select", {"chat_info": "上下文" * 1000}),
        ("emoji_before_select", {"context_texts": ["x"] * 30, "sample_size": 50}),
    ]
    for name, kwargs in hooks:
        result = asyncio.run(getattr(plugin_instance, name)(**kwargs))
        assert result["action"] == "continue", name
