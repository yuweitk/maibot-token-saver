"""插件配置模型的回归测试：WebUI 表单、旧配置迁移、Host 保存链路。

需要 ``maibot-plugin-sdk``；未安装时自动跳过，不影响纯逻辑单测。
"""

from __future__ import annotations

import pytest

pytest.importorskip("maibot_sdk")

from maibot_sdk import MaiBotPlugin  # noqa: E402

from token_saver_core.config import (  # noqa: E402
    TokenSaverConfig,
    _SECTION_FIELD_MAP,
    flatten_config_data,
)


class _DemoPlugin(MaiBotPlugin):
    config_model = TokenSaverConfig


def _schema() -> dict:
    """构造插件配置表单 Schema。

    Returns:
        dict: SDK 生成的配置 Schema。
    """
    return _DemoPlugin.build_config_schema(
        plugin_id="com.yuwei.maibot-token-saver", plugin_name="Token Saver"
    )


# --------------------------------------------------------------------------- WebUI 表单


def test_labels_are_plain_strings_not_dict_repr() -> None:
    """label 必须是纯字符串：SDK 会对字典 label 做 str() 并在界面上显示 Python repr。"""
    for section_name, section in _schema()["sections"].items():
        for field_name, field in section["fields"].items():
            label = field["label"]
            assert isinstance(label, str), (section_name, field_name)
            assert "{" not in label and "zh_CN" not in label, (section_name, field_name, label)
            assert label.strip(), (section_name, field_name)


def test_every_field_has_visible_hint() -> None:
    """前端 FieldRenderer 只渲染 hint，不渲染 description。"""
    for section_name, section in _schema()["sections"].items():
        for field_name, field in section["fields"].items():
            assert field.get("hint"), f"{section_name}.{field_name} 缺少 hint"
            assert field["hint"] == field["description"]


def test_plugin_metadata_field_is_hidden() -> None:
    """[plugin] config_version 不能作为 object 输入框渲染成 [object Object]。"""
    general = _schema()["sections"]["general"]["fields"]
    assert general["plugin"]["hidden"] is True


def test_sections_are_grouped_and_general_comes_first() -> None:
    """常用项留在通用设置，其余按逻辑分区，手机端会自动变成横向 Tab。"""
    sections = _schema()["sections"]
    assert set(sections) == {"general", *_SECTION_FIELD_MAP}
    ordered = sorted(sections.items(), key=lambda item: item[1]["order"])
    assert ordered[0][0] == "general"
    assert [name for name, _ in ordered[1:]] == list(_SECTION_FIELD_MAP)
    assert sections["compression"]["title"] == "文本压缩"
    assert sections["compression"]["description"].startswith("低损文本压缩")


def test_widget_types_are_meaningful() -> None:
    """布尔用开关、数字用数字输入、长文本用多行文本框。"""
    general = _schema()["sections"]["general"]["fields"]
    assert general["enabled"]["ui_type"] == "switch"
    assert general["planner_char_budget"]["ui_type"] == "number"
    assert _schema()["sections"]["tools"]["fields"]["tool_keep_names"]["ui_type"] == "textarea"


def test_default_config_keeps_config_version() -> None:
    """SDK 2.21+ 要求默认配置含 [plugin] config_version。"""
    default = _DemoPlugin.build_default_config()
    assert default["plugin"]["config_version"] == "1"
    assert all(name in default for name in _SECTION_FIELD_MAP)


# --------------------------------------------------------------------------- 展平与迁移


def test_flatten_handles_new_old_and_mixed_shapes() -> None:
    """展平函数必须同时吃下新版分区、旧版扁平、以及两者混合的配置。"""
    default = _DemoPlugin.build_default_config()
    flat = flatten_config_data(default)
    assert flat["planner_char_budget"] == 26000
    assert flat["max_tool_output_chars"] == 1200
    assert flat["verbose_log"] is False
    assert "compression" not in flat
    assert flatten_config_data(flat) == flat, "旧版扁平输入应幂等"

    mixed = dict(default)
    mixed["max_tool_output_chars"] = 999  # Host 合并 patch 后可能残留旧根级键
    assert flatten_config_data(mixed)["max_tool_output_chars"] == 1200, "分区值应优先于根级残留"


def test_legacy_flat_config_is_migrated_without_losing_values() -> None:
    """v1.0.0 的扁平 config.toml 必须自动迁移，用户已调好的参数不能丢。"""
    legacy = flatten_config_data(_DemoPlugin.build_default_config())
    legacy["planner_char_budget"] = 18000
    legacy["max_tool_output_chars"] = 800
    legacy["verbose_log"] = True

    config = TokenSaverConfig.model_validate(legacy)
    policy = config.to_policy()
    assert policy.planner_char_budget == 18000
    assert policy.max_tool_output_chars == 800
    assert policy.enabled is True
    assert flatten_config_data(config.model_dump())["verbose_log"] is True
    assert "max_tool_output_chars" not in config.model_dump(), "旧根级键应被收进分区"


def test_to_policy_matches_flatten_mapping() -> None:
    """to_policy 与展平字典对同一配置的解释必须一致。"""
    config = TokenSaverConfig()
    policy = config.to_policy()
    flat = config.to_flat_mapping()
    assert policy.planner_char_budget == flat["planner_char_budget"]
    assert policy.tool_keep_names == tuple(
        part.strip() for part in flat["tool_keep_names"].split(",") if part.strip()
    )


def test_host_virtual_general_roundtrip_preserves_edits() -> None:
    """复刻 Host 的 fold/unfold 逻辑，确认 WebUI 保存后配置能正确回写与校验。"""
    schema = _schema()
    general_fields = list(schema["sections"]["general"]["fields"])
    default = _DemoPlugin.build_default_config()
    assert all(name in default for name in general_fields), "Host 才会把 general 视为虚构节"

    # Host 读取时：把根级扁平字段折进 general，plugin 保留节只复制不移动。
    folded = dict(default)
    general: dict = {}
    for name in general_fields:
        if name not in folded:
            continue
        if name == "plugin":
            general[name] = folded[name]
        else:
            general[name] = folded.pop(name)
    folded["general"] = general

    # 模拟用户在 WebUI 里改了一个根级项和一个分区项。
    folded["general"]["planner_char_budget"] = 18000
    folded["compression"]["collapse_whitespace"] = False

    # Host 保存时：把 general 下的改动还原到根级。
    patch = dict(folded)
    general_patch = patch.pop("general")
    patch.update(general_patch)

    config = TokenSaverConfig.model_validate(patch)
    assert config.planner_char_budget == 18000
    assert config.compression.collapse_whitespace is False
    assert config.to_policy().planner_char_budget == 18000
