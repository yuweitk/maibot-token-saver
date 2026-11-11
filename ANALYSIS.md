# MaiBot token 消耗源码分析

> 分析基线：`Mai-with-u/MaiBot` 主分支浅克隆，`pyproject.toml` 版本 **1.3.19**，分析日期 2026-11-06。
> 官方文档：<https://docs.maibot.chat/plugin/> 。插件实现见同目录 `plugin.py` 与 `token_saver_core/`。
> 文中所有 `文件:行号` 均指该版本源码，便于你在升级后复核。

---

## 一、结论先行

MaiBot 的 token 大头**不是系统提示词**（`prompts/zh-CN/maisaka_chat.prompt` 只有 2.7 KB），而是每轮都要重复携带的三样东西：

1. **历史上下文**（默认群聊 50 条 / 私聊 75 条，`official_configs.py:875-905`），且每条都带 XML 元信息前缀；
2. **历史里的工具结果**——官方注释自己承认「会留在 Planner 历史里被后续每一轮重复携带」（`tool_result_condenser.py:3-6`）；
3. **工具定义 + deferred tools 清单**——每轮重新注入，装了 MCP 工具后增长很快。

Planner 是「每收到一条消息就跑一轮」的循环，上述开销会**乘以轮数**，所以优化 Planner 的边际收益远高于其他模块。

好消息是 1.3.x 已经开放了刚好够用的 Hook：`maisaka.planner.before_request` 允许插件直接改写**发给模型的 Context Items 和工具定义**（`chat_loop_service.py:1254-1293`）。这意味着不用改源码、不用换 Provider，就能在请求发出前把冗余摘掉。

---

## 二、Planner 一次请求的完整构造链

```
chat_loop_service.py::chat_loop_step()                       L1157
  ├─ select_llm_context_messages()                          L1443
  │    └─ 默认取最近 max_context_size(50) / 私聊(75) 条      L1457-1461
  │       + CONTEXT_SELECTION_CACHE_STABILITY_RATIO         L1460  ← 刻意保护 KV cache
  │       + _expand_selected_tool_turns() 补齐工具轮次       L1530
  ├─ _build_request_messages()                              L1023
  │    ├─ [0] SystemMessageItem ← load_prompt(maisaka_chat)  L1044-1061 / L764-779
  │    ├─ [1..n] 历史 Item（每条带 XML 前缀）                 L1066-1095
  │    │        前缀构造见 planner_messages.py:17-74
  │    ├─ history_item_count 边界                            L1101
  │    └─ 尾部即时通知：deferred tools 清单 / 记忆注入 /
  │       当前时间 / 当前聊天注意力 / final reminder          L1103-1129
  ├─ 收集工具定义 all_tools                                  L1224-1252
  ├─ serialize_prompt_items(built_messages)                  L1254
  ├─ ★ invoke_hook("maisaka.planner.before_request")        L1255-1264
  │    └─ 插件可改写 items / tool_definitions               L1266-1293
  ├─ measure_request_sections() 分段统计                     L1299
  ├─ 真正调用 LLM                                            L1327-1336
  ├─ ★ invoke_hook("maisaka.planner.after_response")        L1362-1394
  └─ 统计 prompt_cache_hit / miss                           L1349-1358
```

`measure_request_sections()`（`context/usage.py:96-141`）把请求分成 7 段，正好就是我们要下手的靶子：

| 分段 | 常量 | 是否优化 |
|------|------|----------|
| `system_prompt` | `SECTION_SYSTEM_PROMPT` | ❌ 不动（人设 + cache 前缀锚点） |
| `messages` | `SECTION_MESSAGES` | ✅ 旧历史压缩/裁剪，最近 N 条不动 |
| `instant_notice` | `SECTION_INSTANT_NOTICE` | ✅ 只压 deferred tools 清单，其余保留 |
| `mcp_tools` / `builtin_tools` / `plugin_tools` / `other_tools` | 按 provider_type 分类 | ✅ 精简描述，默认不删工具 |

官方自己也在保护缓存：`chat_loop_service.py:1180-1183` 的注释写着「不因后续图片增加而替换历史图片、**破坏 KV 缓存前缀**」，`L1460` 还专门用 `CONTEXT_SELECTION_CACHE_STABILITY_RATIO` 稳定上下文窗口。所以本插件把**确定性**当成硬要求——压缩结果若随轮次漂移，缓存会从头失效，省下的 token 还不够赔命中率下降。

---

## 三、token 消耗点清单

| # | 位置 | 消耗来源 | 特点 | 插件对策 |
|---|------|----------|------|----------|
| 1 | `chat_loop_service.py:1066-1095` | 历史消息（默认 50/75 条） | 每轮重复携带；每条含 `<message msg_id=... time=... user=... group_card=...>` 前缀（`planner_messages.py:17-74`） | 低损文本压缩 + 按字符预算裁剪旧历史；最近 14 条不动 |
| 2 | `tool_result_condenser.py:3-6` | **历史工具结果** | 官方注释：文档检索/插件列表/图库搜索返回几千字，且留在历史里被每轮重复携带 | 旧结果按 `max_tool_output_chars` 截断（保留头尾） |
| 3 | `context/compaction.py:76-101` | `ReasoningItem` / `ProviderActivityItem` / `ProviderOpaqueItem` | 官方压缩逻辑 `message_text_for_compaction` 对这三类**直接返回空串** → 官方也认为它们不该进上下文 | 从旧历史中删除（有官方依据） |
| 4 | `chat_loop_service.py:1224-1252` | 工具定义 schema | 每轮全量注入；description 常带长篇说明 | 精简描述、移除 `title`/`$schema` 等展示字段；**默认不删工具** |
| 5 | `runtime.py:1528-1556` | deferred tools 清单 | 每条 `N. tool_name: description`，每轮作为 user 消息注入；MCP 多时可达数千字符 | 单条描述截断到 64 字，工具名完整保留，仍可 `tool_search` |
| 6 | `chat_loop_service.py:1103-1129` | 即时通知（时间、注意力、final reminder） | 每轮固定 | **保留**（属于拟人行为约束，且占比小） |
| 7 | `chat_loop_service.py:1362-1394` | 本轮 `output_items` 写入历史 | 一次冗长，后续每轮都要背 | `after_response` 压缩文本，一次压缩多轮受益 |
| 8 | `maisaka_generator_base.py:1149-1158` | Replyer 请求（表达方式、习惯、要求） | 每次回复一轮 | `before_model_request` 同样压缩 Context Items |
| 9 | `expression_learner.py:95-128` | 表达方式选择 `chat_info` | 单次 LLM 调用，上下文可能很长 | `before_select` 裁剪，保留尾部最近内容 |
| 10 | `emoji_manager.py:62-81` | 表情选择 `context_texts` / `sample_size` | 单次 LLM 调用 | 限制条数与候选数 |
| 11 | `official_configs.py:907-921` | 滚动状态摘要（上限 6000 字符） | 由 `utils` 模型生成，**始终随 Planner 上下文发送** | 未动（属于核心长期记忆，改动风险高） |
| 12 | `default_model_config.py:20-57` | 模型选择 | 默认 `planner`/`utils` = `deepseek-v4-flash`，`replyer` = `deepseek-v4-pro-think/nonthink`（12元/24元 每 M） | 可选 `simple_reply_model_name` 把简单回复降级；**默认关闭** |

---

## 四、Hook 能力边界（为什么选这几个点）

`chat_loop_service.py:180-458` 注册了 Maisaka 相关 HookSpec，全部 `allow_kwargs_mutation=True`：

| Hook | 关键参数 | 能否 abort | 插件用途 |
|------|----------|-----------|----------|
| `maisaka.planner.before_request` | `items`, `item_schema_version`, `tool_definitions`, `selected_history_count`, `built_message_count`, `selection_reason`, `session_id` | ❌ | **主战场**：改写请求 Items 与工具定义 |
| `maisaka.planner.after_response` | `output_items`, `item_schema_version`, `prompt_tokens`, `completion_tokens`, `total_tokens` | ❌ | 压缩本轮输出 |
| `maisaka.replyer.before_request` | `task_name`, `model_name`, `extra_prompt`, `reply_tool_args`, `attempt`, `retry_count` | ❌ | 去重提示词、可选模型降级 |
| `maisaka.replyer.before_model_request` | `items`, `item_schema_version`, `task_name`, `selected_model_name`, `reply_tool_args` | ❌ | 改写 Replyer 实际请求 Items |
| `maisaka.replyer.after_response` | `response`, `output_items` | ✅ | 未使用（与省 token 无关） |
| `maisaka.reply.before_post_process` | `response`, `skip_post_process` | ✅ | 未使用（不省 token） |

Host 的应用逻辑在 `chat_loop_service.py:1266-1293` 和 `maisaka_generator_base.py:790-803`：只有返回的 `items` **与原始序列化结果不同**时才会反序列化并替换；`tool_definitions` 只要返回 list 就直接采用。

### 三条必须遵守的硬约束

1. **`item_schema_version` 必须是 1**（`context_item.py:17`）。`hook_payloads.py:160-164` 会拒绝其他值，整个改写结果被忽略。插件从不修改这个字段，天然满足。
2. **工具调用必须成对**。`context_protocol.py:142-179` 的 `validate_context_items()` 检查重复 `item_id`/`call_id`、孤儿 output、未回答 call、output 位置、以及 `logical_turn_id` 一致性；`chat_loop_service.py:1144-1155` 还要求首个 `FunctionCallItem` 之前存在 `UserMessageItem` 或 `FunctionCallOutputItem` 锚点。插件按 `logical_turn_id` 分组删除，并在返回前自行完整校验，失败即回退。
3. **`meta` 不能动**。`context_snapshot.py:37-61` 反序列化时强校验 `item_id` / `logical_turn_id` / `timestamp`。插件只改 `parts` / `output` / `text_parts` 等文本字段。

### 一个容易踩的坑：provider replay

`hook_payloads.py:166-178`：插件改写过的 Item 会用新对象替代，**丢失 provider replay fragment**（用于 Responses API 原生续接的 `encrypted_content` / `thought_signature` 等）。对普通 OpenAI 兼容接口（DeepSeek chat completions）影响可忽略；对原生 reasoning 续接模型可能略降命中率。这就是插件默认**不动 system 和最近 N 条消息**的第二个理由——把 replay 失效范围压到最小的旧历史区。

另外 `hook_payloads.py:123-133` 明确「不暴露 replay payload」，`context_snapshot.py:23-34` 会剥离 `encrypted_content` / `thought_signature`：插件拿到的本来就是无 replay 的投影，无需也无法伪造这些字段。

---

## 五、为什么没有用另外几种方案

| 方案 | 未采用的原因 |
|------|-------------|
| **改 MaiBot 源码**（直接改 `max_context_size`、改 prompt 模板） | 用户偏好零侵入 + 可回滚；升级会冲掉改动。插件停用即回滚 |
| **`@LLMProvider` 全局代理** | 需要用户改 `model_config.toml` 的 `api_providers[].client_type`，部署门槛高；文档明确插件 Provider 不支持 Host 侧流式处理器/响应解析器；还会绕过 Host 的 prompt cache 统计与 request snapshot。现有 Hook 覆盖面已足够 |
| **用 LLM 做语义摘要** | 摘要本身要调模型、要花 token。官方 `tool_result_condenser.py` 已经用子代理做了（`utils` 任务，额外一次 LLM 调用）。本插件用纯本地规则，零额外 LLM 开销，可与官方机制叠加 |
| **压缩系统提示词** | 只有 2.7 KB，占比最小；它是 prefix cache 的锚点，改了每轮缓存全失效；人设/风格全在里面，直接影响拟人。性价比最低、风险最高 |
| **删除 MCP / 插件工具定义** | `reasoning_engine.py:224-278` 已实现 deferred tools 按需发现（`tool_search`），全量工具本来就不注入；删 visible 工具会改变 `_active_tool_names`，Planner 需要时调用失败。默认只压描述 |
| **注册新 Tool 给模型查统计** | 多一个工具定义 = 每轮多一份 schema，与省 token 相反。改用 `/tokensave` 命令 |

---

## 六、节省估算

### 估算方法

MaiBot 自己在 `context/usage.py:3-7` 说明：本地只统计**字符数**，真实 token 由模型 `usage` 返回，前端按字符占比换算，避免引入第二套 tokenizer 造成口径不一致。本插件沿用同样口径，并按 0.55 token/字符做**仅供趋势对比**的粗估。

### 一个「活跃群聊 + 若干 MCP 工具」的示意分解

| 分段 | 典型字符 | 优化后 | 手段 |
|------|---------:|-------:|------|
| system_prompt | 1,500 | 1,500 | 不动 |
| 历史聊天 50 条 | 6,000 | 3,200 | 旧消息去空白/去重 + 超预算裁剪 |
| 历史工具结果（3 条常驻） | 8,000 | 2,400 | 单条上限 1200，保留头尾 |
| 旧 Reasoning / Provider Item | 3,000 | 0 | 删除（官方同样忽略） |
| 工具定义（内置 + MCP） | 5,000 | 3,100 | 描述精简、去 `title` |
| deferred tools 清单 | 2,000 | 600 | 单条描述 64 字 |
| 即时通知 + 最近 14 条 | 1,500 | 1,500 | 不动（拟人保护） |
| **合计** | **27,000** | **12,300** | **约 -54%** |

### 保守预期

| 场景 | Planner 输入 token 降幅 | 整体（含 Replyer/辅助链路）降幅 |
|------|------------------------:|-------------------------------:|
| 短私聊、历史少 | 10% ~ 25% | 8% ~ 20% |
| 常规群聊（默认 50 条上下文） | **30% ~ 55%** | **25% ~ 45%** |
| 长群聊 / 大量 MCP 工具 / 频繁工具调用 | 55% ~ 70% | 45% ~ 60% |

> 这些是**结构性估算，不是承诺**。真实值取决于你的 `max_context_size`、实际消息长度、工具数量、模型 tokenizer。
> 上线后请用 `/tokensave` 看插件统计，并用 WebUI 统计里的 `prompt_tokens` 与 `prompt_cache_hit_tokens` 交叉验证。
>
> 额外提醒：DeepSeek 的缓存命中价（约 0.02 元/M）远低于未命中输入价（约 1 元/M）。由于本插件保证确定性压缩、且不改 system 前缀，**缓存命中率不会下降**，实际账单降幅可能比 token 降幅更明显。

### 合成用例实测

插件仓库自带 30 个单测。用一组含 2 个工具轮次、25 个 Item、3 个超长工具定义的合成请求跑完整插件（真实 `maibot-plugin-sdk 2.21.0` 环境）：

```
可见字符: 21,672 -> 10,116（省 11,556 字, 53.3%）
主要命中: text_compressed×7, internal_dropped×4,
         tool_output_truncated×4, tool_desc_slimmed×3
改写后 Items 协议校验: OK    回退: 0
```

---

## 七、拟人效果保护清单

| 保护项 | 实现 |
|--------|------|
| 人设/风格/群聊规则 | `keep_system_untouched=True`，`SystemMessageItem` 永不进入改写区 |
| 当下语气与上下文衔接 | 尾部 `keep_recent_items=14` 条逐字不动 |
| 用户原话 | 只删空白与**完全连续重复**的行，不做摘要、不替换用词 |
| 工具结果里的结论 | `truncate_smart` 保留头尾（结论常在末尾），并标注省略字数 |
| 工具可用性 | 默认不删任何工具，只压描述 |
| prompt cache | 所有压缩为确定性纯函数；不改 system 前缀 |
| 异常安全 | 每个 Hook 独立 try/except；Items 改写前自行跑一遍 Host 同款协议校验；失败原样回退 |
| 可观测/可回滚 | `dry_run` A/B、`/tokensave` 统计、`enabled` 一键关闭、删除目录即彻底回滚 |

---

## 八、后续还能继续挖的方向

1. **自适应预算**：通过 `ctx.statistics.local.token_trend`（需在 manifest 声明该 capability）读取真实 `prompt_tokens`，动态调 `planner_char_budget`，而不是固定阈值。
2. **全局语义缓存**：用 `@LLMProvider` 做一层代理，对完全相同的请求前缀短路复用。需要用户改 `model_config.toml`，适合作为独立的进阶插件。
3. **`chat.receive.before_process`**：压缩合并转发消息 `data[].content` 与图片识别注释（风险较高，需大量实机验证）。
4. **`emoji.register.after_build_description`**：压缩表情描述，减少表情注册链路调用。
5. **黑话/表达方式学习链路**：`jargon.*` 与 `expression.learn.*` Hook 可减少**调用次数**（不是单次 token），对总账单同样有效。
6. **与官方 `tool_result_condense_threshold` 联动**：`official_configs.py:1042`。若把该阈值调低，官方子代理会更早整理超长结果；与本插件的本地截断形成两级压缩，但会多一次 `utils` 模型调用，需算清账。
