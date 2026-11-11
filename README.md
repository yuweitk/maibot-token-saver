# Token Saver · MaiBot 上下文瘦身插件

给麦麦用的省 token 插件，重点是**每轮都要跑的 Maisaka Planner**，顺带压掉 Replyer、表达方式选择、表情选择等链路的冗余输入。全程不改 MaiBot 一行源码，停用或删掉插件目录即完全回滚。

> 设计红线：**不动人设、不动最近对话、不改写语义**。只删空白、完全重复的内容、旧的内部推理和过长的工具结果。
> 所有压缩都是确定性纯函数，保证 DeepSeek / OpenAI 的 prompt prefix cache 持续命中——否则省下的 token 还不够赔缓存失效。

---

## 一、安装

### 1. 放到插件目录

把整个 `maibot-token-saver/` 目录复制到 MaiBot 的 `plugins/` 下：

```bash
# Linux / 服务器（按你的实际路径改）
cp -r /path/to/maibot-token-saver /path/to/MaiBot/plugins/
```

```powershell
# Windows（Git Bash 或 PowerShell 均可，按你的实际路径改）
Copy-Item -Recurse D:\path\to\maibot-token-saver D:\MaiBot\plugins\
```

最终结构应该是：

```
MaiBot/
└── plugins/
    └── maibot-token-saver/
        ├── _manifest.json
        ├── plugin.py
        ├── token_saver_core/
        └── ...
```

### 2. 重启或重载

重启 MaiBot，或在 WebUI → 插件管理里点「重载」。日志里出现下面这行就说明加载成功：

```
Token Saver v1.0.1 已加载 | 启用=True 试运行=False Planner预算=26000字 Replyer预算=16000字
```

### 3. 先试运行，再正式开

建议按这个顺序验收，避免一上来就改变聊天风格：

| 步骤 | 操作 | 看什么 |
|------|------|--------|
| ① | 在 WebUI 插件配置里把 **试运行（dry_run）** 打开 | 插件只统计、不改写。日志显示「预计节省」 |
| ② | 正常聊 20~30 分钟，发 `/tokensave` | 看各链路预计节省比例，确认没有异常回退 |
| ③ | 关掉 dry_run，再聊 30 分钟 | `/tokensave` 看**实际**节省；同时对比 WebUI 统计里的 `prompt_tokens` 与缓存命中率 |
| ④ | 人工检查回复 | 是否变生硬、是否丢上下文、表情/口癖是否正常 |

不满意就把 `enabled` 关掉，或在 WebUI 里逐项关闭，不需要改任何源码。

---

## 二、命令

| 命令 | 作用 |
|------|------|
| `/tokensave` | 输出累计节省统计（字符数、粗估 token、分链路明细、命中动作、回退次数） |
| `/tokensave reset` | 清空统计 |

输出示例：

```
Token Saver v1.0.1 统计
运行 2.3 小时 | 处理 412 次请求 | 实际改写 398 次 | 回退 0 次
可见字符: 8,921,340 -> 4,106,210 (省 4,815,130 字, 54.0%)
粗估输入 token 节省: ~2,648,321（真实值以模型 usage 为准）
分链路:
  maisaka.planner.before_request: 180 次, 省 3,201,440 字, 回退 0
  ...
主要命中: text_compressed×812, internal_dropped×604, tool_output_truncated×377
```

> 粗估按 0.55 token/字符，只用于看趋势。**真实 token 请以模型返回的 `usage` / WebUI 统计为准**。
> 插件不注册任何 `Tool`——多一个工具定义，Planner 每轮就要多带一份 schema，和省 token 的目标相反。

---

## 三、默认做了什么

| 链路 | Hook | 动作 | 默认 |
|------|------|------|------|
| **Planner** | `maisaka.planner.before_request` | 删除旧内部推理/Provider 活动 Item、截断旧工具结果、折叠重复消息、按字符预算裁剪旧历史、精简工具定义描述 | ✅ |
| **Planner** | `maisaka.planner.after_response` | 压缩本轮输出文本，避免冗长推理写进历史（一次压缩，多轮受益） | ✅ |
| Replyer | `maisaka.replyer.before_model_request` | 压缩最终发给回复模型的 Context Items | ✅ |
| Replyer | `maisaka.replyer.before_request` | 去重 `extra_prompt`；可选把简单短回复路由到低成本模型 | 去重 ✅ / 路由 ❌ |
| 表达方式 | `expression.select.before_select` | 裁剪 `chat_info`，保留最近内容 | ✅ |
| 表情 | `emoji.maisaka.before_select` | 限制上下文条数、候选采样数、推理文本长度 | ✅ |

**明确不做的事**（保护拟人效果与稳定性）：

- 不改系统提示词（人设、性格、行为风格、群聊/私聊规则全部原样）；
- 不动最近 14 条消息（当下语气、口癖、上下文衔接全靠它们）；
- 不删任何工具定义（激进裁剪默认关闭）；
- 不改写用户原话的用词，不做摘要、不调用模型做二次压缩；
- 不碰图片 base64，不碰 `msg_id` / `call_id` / `logical_turn_id`；
- 不注册新工具、不增加任何每轮固定开销。

---

## 四、常用配置

配置文件由 Runner 自动生成在插件目录下的 `config.toml`，推荐直接在 **WebUI → 插件 → Token Saver** 里改（改完自动热重载）。

36 个配置项分成 6 组：**通用设置 / 文本压缩 / 历史裁剪 / 工具定义精简 / 输出与模型路由 / 辅助链路与统计**。桌面端是可折叠卡片，手机端会自动变成顶部横向标签页；每个开关下方都有说明文字。

> **从 v1.0.0 升级**：v1.0.1 起配置按分组存成 TOML 子表。旧版全扁平的 `config.toml` 会被自动迁移，你之前调好的参数不会丢；想让文件也变干净，删掉 `config.toml` 让 Runner 重新生成即可。

键名保持不变，只是按分组归位：

```toml
[plugin]
config_version = "1"

# 根级 = 通用设置
enabled = true
planner_char_budget = 26000

[compression]          # 文本压缩
max_tool_output_chars = 1200

[history]              # 历史裁剪
drop_old_reasoning = true

[tools]                # 工具定义精简
max_tool_description_chars = 360
```

| 配置 | 所在分组 | 默认 | 说明 |
|------|----------|------|------|
| `enabled` | 通用设置 | `true` | 总开关，关掉即完全放行 |
| `dry_run` | 通用设置 | `false` | 只统计不改写，用于 A/B 验收 |
| `planner_char_budget` | 通用设置 | `26000` | Planner 单轮可见文本字符预算，超出才裁旧历史。**调小省得更多，但可能影响长对话连贯性** |
| `replyer_char_budget` | 通用设置 | `16000` | Replyer 预算 |
| `keep_recent_items` | 通用设置 | `14` | 尾部完全不动的消息条数。**越大越保护拟人效果，省得越少** |
| `max_tool_output_chars` | 文本压缩 | `1200` | 旧历史中单条工具结果上限（保留头尾，结论常在后头）。设 `0` 不限制 |
| `max_deferred_tool_desc_chars` | 文本压缩 | `64` | deferred tools 清单每条描述上限。装了大量 MCP 工具时这一项收益明显 |
| `max_tool_description_chars` | 工具定义精简 | `360` | 工具定义描述上限 |
| `drop_old_reasoning` | 历史裁剪 | `true` | 删除旧内部推理 Item |
| `enable_tool_slimming` | 工具定义精简 | `true` | 精简工具描述与展示性 Schema 字段 |
| `enable_output_condense` | 输出与模型路由 | `true` | 压缩本轮输出 |
| `simple_reply_model_name` | 输出与模型路由 | 空 | **可选成本优化**：简单短回复切到便宜模型（如把 Replyer 从 `deepseek-v4-pro-*` 切到 `utils` 的 flash）。留空 = 关闭。开启后可能削弱拟人效果，请自行权衡 |
| `verbose_log` | 辅助链路与统计 | `false` | 打开后逐次打印节省日志 |

### 三档调参建议

| 档位 | 改法 | 适用 |
|------|------|------|
| **保守（默认）** | 保持默认 | 日常使用，优先保证聊天效果 |
| **均衡** | `keep_recent_items=10`、`planner_char_budget=18000`、`max_tool_output_chars=800` | 群聊活跃、历史很长 |
| **激进** | `keep_recent_items=6`、`planner_char_budget=12000`、`max_tool_output_chars=500`、`max_deferred_tool_desc_chars=40` | token 吃紧、愿意牺牲一点长上下文连贯性 |

---

## 五、拟人效果是怎么保住的

1. **分区改写**：请求被切成 `[system] + [可优化的旧历史] + [最近 N 条 + 即时通知]`。头尾完全不碰，只动中间。
2. **只删「非人话」**：旧的内部推理、Provider 活动摘要、超长工具结果、连续重复行、冗余空白。 MaiBot 自己的压缩逻辑 `compaction.py::message_text_for_compaction` 同样把 `reasoning` / `provider_activity` / `provider_opaque` 视为空内容——删它们有官方依据。
3. **保留头尾的截断**：工具结果常被模型当作结论来源，只留开头会丢答案。
4. **确定性压缩**：相同输入永远得到相同输出，连续多轮的请求前缀稳定，prompt cache 命中率不下降。
5. **全链路 fail-open**：任何异常、任何协议校验失败都原样放行。宁可这轮不省，也绝不让麦麦报错或不回话。

---

## 六、排错

| 现象 | 原因与处理 |
|------|-----------|
| 插件没被加载 | 检查 `_manifest.json`：`manifest_version=2`、`id` 全小写且含 `.`/`-`、`capabilities` 与 `i18n` 必填。Runner 日志会给出 `ManifestValidator` 的具体错误 |
| 提示版本不兼容 | 你的 MaiBot 不在 `1.3.0~1.3.99`。按实际版本改 `host_application.max_version`（写「主.次.99」），临时排查可开 `debug.force_plugin_compatibility` |
| 配置页显示 `{'zh_CN': ...}` 或 `[object Object]` | v1.0.0 的 UI bug：插件 SDK 会把字典形式的 `label` 用 `str()` 转成 Python repr，元数据字段被当成 object 渲染。升级到 **v1.0.1** 并重载插件即可 |
| 提示配置缺少 `[plugin] config_version` | 用 SDK 2.21+ 时会出现；本插件已在配置模型里内置该字段，若仍报错说明 `config.toml` 是旧文件，删掉让 Runner 重新生成即可 |
| `/tokensave` 没反应 | 命令需要 `send.text` 能力，manifest 已声明。检查 WebUI 里命令是否被停用；也可直接看插件日志，统计每次都会打印 |
| 节省为 0 | 大概率是 `dry_run` 还开着，或历史太短没超过预算；打开 `verbose_log` 看逐次记录 |
| 日志出现 `budget_trim_reverted` | 裁剪后不满足工具调用配对约束，插件自动回退到「只压缩不裁剪」。属正常保护，不影响聊天 |
| 回复变生硬 / 丢上下文 | 调大 `keep_recent_items` 与 `planner_char_budget`，或关掉 `drop_old_reasoning`；仍不满意直接 `enabled=false` |
| 想彻底回滚 | 停用插件，或删除 `plugins/maibot-token-saver/` 目录并重启。插件没有写入 MaiBot 任何源码或数据库表 |

---

## 七、目录结构与本地测试

```
maibot-token-saver/
├── _manifest.json                 # 插件清单（Host 校验）
├── plugin.py                      # 入口：6 个 Hook + 2 个 Command
├── token_saver_core/
│   ├── config.py                  # SDK 强类型配置模型（分区表单 + 旧配置迁移）
│   ├── policy.py                  # 不可变优化策略（纯 Python）
│   ├── budget.py                  # 字符/token 估算
│   ├── compressor.py              # 确定性文本压缩原语
│   ├── items.py                   # Context Items 安全改写 + 协议校验
│   ├── tools.py                   # 工具定义瘦身
│   └── stats.py                   # 用量统计与原子落盘
├── tests/                         # 45 个测试：31 核心单测 + 10 配置/迁移 + 4 集成
├── pyproject.toml                 # 仅本地测试用，MaiBot 不读取
├── README.md                      # 本文
└── ANALYSIS.md                    # 源码级 token 消耗分析与节省估算
```

在插件目录跑测试（核心逻辑不依赖 SDK，普通 Python 环境即可）：

```bash
cd maibot-token-saver
python -m pytest tests/ -q
# 31 passed（未装 SDK 的环境会跳过 14 个需 SDK 的测试）
# 装了 maibot-plugin-sdk 时：45 passed
```

---

## 八、兼容性与依赖

| 项 | 值 |
|----|----|
| MaiBot Host | `1.3.0 ~ 1.3.99`（在 `1.3.19` 上分析验证） |
| 插件 SDK | `2.0.0 ~ 2.21.99` |
| Python | `>= 3.10` |
| 第三方依赖 | **无**（只用标准库 + `maibot_sdk`） |
| 声明的能力 | 仅 `send.text`（用于 `/tokensave` 回消息） |

零外部依赖意味着不会因为装包失败、镜像源问题或版本冲突导致插件加载不了。
