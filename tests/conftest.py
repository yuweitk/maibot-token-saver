"""pytest 路径配置：让测试可以 import 插件目录里的 token_saver_core。"""

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_TESTS_DIR = Path(__file__).resolve().parent
for _path in (_PLUGIN_ROOT, _TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
