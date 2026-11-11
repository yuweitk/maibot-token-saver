"""MaiBot Token Saver —— 核心模块包。

所有压缩逻辑都放在这里且**不依赖 MaiBot 主程序或插件 SDK 的运行时能力**
（只有 ``config.py`` 需要 SDK 的配置基类），因此可以独立用 pytest 测试。
"""

from .budget import MeasureResult, estimate_tokens, measure_items, measure_tool_definitions
from .items import OptimizeReport, optimize_output_items, optimize_request_items
from .policy import OptimizePolicy
from .tools import ToolOptimizeReport, optimize_tool_definitions

__all__ = [
    "OptimizePolicy",
    "OptimizeReport",
    "ToolOptimizeReport",
    "MeasureResult",
    "estimate_tokens",
    "measure_items",
    "measure_tool_definitions",
    "optimize_request_items",
    "optimize_output_items",
    "optimize_tool_definitions",
]
