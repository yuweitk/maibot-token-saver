"""用量统计与持久化。

统计落在 Host 注入的插件专属目录 ``self.ctx.paths.data_dir``，不写到别处，
符合插件运行边界约定。数据只用于向用户展示「省了多少」，不上传、不联网。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

# 换算系数与 budget.py 保持一致，仅用于趋势展示
_TOKEN_PER_CHAR = 0.55
_FLUSH_EVERY = 20


@dataclass(slots=True)
class HookCounter:
    """单个 Hook 的累计用量。

    Attributes:
        requests: 处理次数。
        applied: 实际发生改写的次数。
        before_chars: 累计优化前字符数。
        after_chars: 累计优化后（或预计）字符数。
        fallbacks: fail-open 回退次数。
    """

    requests: int = 0
    applied: int = 0
    before_chars: int = 0
    after_chars: int = 0
    fallbacks: int = 0

    def to_dict(self) -> Dict[str, int]:
        """转换为可序列化字典。

        Returns:
            Dict[str, int]: 统计字段。
        """

        return {
            "requests": self.requests,
            "applied": self.applied,
            "before_chars": self.before_chars,
            "after_chars": self.after_chars,
            "fallbacks": self.fallbacks,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "HookCounter":
        """从字典恢复计数器。

        Args:
            raw: 持久化字典。

        Returns:
            HookCounter: 恢复后的计数器。
        """

        return cls(
            requests=int(raw.get("requests", 0)),
            applied=int(raw.get("applied", 0)),
            before_chars=int(raw.get("before_chars", 0)),
            after_chars=int(raw.get("after_chars", 0)),
            fallbacks=int(raw.get("fallbacks", 0)),
        )


class TokenSaverStats:
    """线程/协程安全的用量统计器。

    Attributes:
        enabled: 是否记录与落盘。
    """

    def __init__(self, data_dir: Optional[Path], enabled: bool = True) -> None:
        """初始化统计器并尝试载入历史数据。

        Args:
            data_dir: 插件持久化目录；为 None 时退化为纯内存统计。
            enabled: 是否启用统计。
        """

        self.enabled = enabled
        self._path: Optional[Path] = (data_dir / "stats.json") if data_dir else None
        self._lock = asyncio.Lock()
        self._dirty = 0
        self._started_at = time.time()
        self.by_hook: Dict[str, HookCounter] = {}
        self.total_actions: Dict[str, int] = {}
        if self._path and self._path.exists():
            self._load_sync()

    def _load_sync(self) -> None:
        """同步读取历史统计文件，损坏时静默重置。"""

        assert self._path is not None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self.by_hook = {
                str(key): HookCounter.from_dict(value)
                for key, value in (raw.get("by_hook") or {}).items()
                if isinstance(value, dict)
            }
            actions = raw.get("total_actions") or {}
            self.total_actions = {str(k): int(v) for k, v in actions.items() if isinstance(v, (int, float))}
            self._started_at = float(raw.get("started_at", self._started_at))
        except (OSError, ValueError, TypeError, AttributeError):
            self.by_hook = {}
            self.total_actions = {}

    async def record(
        self,
        hook: str,
        *,
        before_chars: int,
        after_chars: int,
        applied: bool,
        fallback: bool = False,
        actions: Optional[Dict[str, int]] = None,
    ) -> None:
        """记录一次 Hook 处理结果。

        Args:
            hook: Hook 名称。
            before_chars: 优化前字符数。
            after_chars: 优化后（或 dry_run 预计）字符数。
            applied: 是否真正改写。
            fallback: 是否发生了 fail-open 回退。
            actions: 本次各优化动作命中次数。
        """

        if not self.enabled:
            return
        async with self._lock:
            counter = self.by_hook.setdefault(hook, HookCounter())
            counter.requests += 1
            counter.applied += int(bool(applied))
            counter.before_chars += max(0, before_chars)
            counter.after_chars += max(0, after_chars)
            counter.fallbacks += int(bool(fallback))
            for key, count in (actions or {}).items():
                self.total_actions[key] = self.total_actions.get(key, 0) + int(count)
            self._dirty += 1
            should_flush = self._dirty >= _FLUSH_EVERY
        if should_flush:
            await self.flush()

    async def flush(self) -> None:
        """把累计统计原子写入磁盘。

        Raises:
            OSError: 目录不可写时抛出，由调用方记录日志，不影响主流程。
        """

        if not self.enabled or not self._path:
            return
        async with self._lock:
            payload = {
                "started_at": self._started_at,
                "updated_at": time.time(),
                "by_hook": {key: value.to_dict() for key, value in self.by_hook.items()},
                "total_actions": dict(self.total_actions),
            }
            self._dirty = 0
            data = json.dumps(payload, ensure_ascii=False, indent=2)
        await asyncio.to_thread(self._atomic_write, data)

    def _atomic_write(self, data: str) -> None:
        """以临时文件 + os.replace 的方式原子写入。

        Args:
            data: 已序列化的 JSON 文本。
        """

        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(self._path.parent), suffix=".tmp", delete=False
        )
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        finally:
            handle.close()
        os.replace(temp_name, self._path)

    async def reset(self) -> None:
        """清空所有统计（包括磁盘文件）。"""

        async with self._lock:
            self.by_hook = {}
            self.total_actions = {}
            self._started_at = time.time()
            self._dirty = 0
            data = json.dumps(
                {"started_at": self._started_at, "updated_at": self._started_at, "by_hook": {}, "total_actions": {}},
                ensure_ascii=False,
                indent=2,
            )
        if self._path:
            await asyncio.to_thread(self._atomic_write, data)

    def summary(self, *, plugin_version: str = "1.0.1") -> str:
        """生成可直接发送给用户的统计摘要。

        Args:
            plugin_version: 插件版本号，用于展示。

        Returns:
            str: 多行中文摘要。
        """

        total_before = sum(item.before_chars for item in self.by_hook.values())
        total_after = sum(item.after_chars for item in self.by_hook.values())
        total_requests = sum(item.requests for item in self.by_hook.values())
        total_applied = sum(item.applied for item in self.by_hook.values())
        total_fallback = sum(item.fallbacks for item in self.by_hook.values())
        saved = max(0, total_before - total_after)
        ratio = (saved / total_before * 100) if total_before else 0.0
        saved_tokens = int(saved * _TOKEN_PER_CHAR)
        hours = max(0.001, (time.time() - self._started_at) / 3600)

        lines = [
            f"Token Saver v{plugin_version} 统计",
            f"运行 {hours:.1f} 小时 | 处理 {total_requests} 次请求 | 实际改写 {total_applied} 次 | 回退 {total_fallback} 次",
            f"可见字符: {total_before} -> {total_after} (省 {saved} 字, {ratio:.1f}%)",
            f"粗估输入 token 节省: ~{saved_tokens}（真实值以模型 usage 为准）",
        ]
        if self.by_hook:
            lines.append("分链路:")
            for hook in sorted(self.by_hook):
                counter = self.by_hook[hook]
                hook_saved = max(0, counter.before_chars - counter.after_chars)
                lines.append(
                    f"  {hook}: {counter.requests} 次, 省 {hook_saved} 字, 回退 {counter.fallbacks}"
                )
        if self.total_actions:
            top = sorted(self.total_actions.items(), key=lambda pair: -pair[1])[:5]
            lines.append("主要命中: " + ", ".join(f"{key}×{value}" for key, value in top))
        return "\n".join(lines)
