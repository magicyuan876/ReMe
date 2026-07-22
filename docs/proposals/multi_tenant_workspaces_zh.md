# 提案:原生多租户 Workspace(v3 · 含完整改造计划)

- 状态:内部评审稿(未发布)
- 关联 issue:[#368](https://github.com/agentscope-ai/ReMe/issues/368)
- 配套文档:[agentscope-java ↔ v4 多租户 ReMe 适配契约](reme_agentscope_java_adapter_zh.md)
- 范围:服务层、应用装配、组件生命周期。**不改变** workspace 磁盘布局、记忆文件格式、
  step/job 的业务代码与公共 schema 的既有字段。
- v3 变更:修正组件"共享/租户"划分轴(`FILE_CHUNKER`/`AGENT_WRAPPER` 归租户作用域,
  见第 4 节);新增记忆写路径的捕获/蒸馏拆分与 digest 巩固触发者(M8);补跨作用域 bind、
  巩固触发、写路径隔离等测试(T11–T14)。

---

## 1. 概要

当前一个 `Application` 进程绑定一个 workspace。需要服务大量终端用户的部署只有两条路:
每用户一个进程(固定成本 × 用户数),或共享 workspace(检索与巩固管道跨用户混合)。

本提案让一个 ReMe 服务进程原生承载 N 个租户:

- 每个租户对应 `workspaces_root/<tenant_id>/`,就是一个普通 ReMe workspace,布局逐字节
  不变,所有者仍可直接阅读、编辑、备份、整体带走;
- 数据/路径相关组件按租户惰性实例化,常驻数量受 LRU 上限约束;仅 workspace 无关的
  推理客户端(`as_llm`/`as_embedding`/`tokenizer`)全局共享;
- 租户身份在服务边界解析注入,拒绝请求负载自报;
- **单进程即可承载全部租户**(常驻内存只与活跃租户数相关);按 `hash(tenant_id)` 分片
  多进程是可选的水平扩展路径,仅用于突破单进程的 CPU(GIL)上限与缩小故障域,不是
  架构的组成部分。

## 2. 设计不变量

两条在方案推导中已被排除的路线,作为不变量固定下来:

1. **不做共享数据面**(一个 store 存所有租户的 chunk、查询时按租户过滤):
   `LocalFileStore` 启动全量加载 zstd 快照进内存,共享 store 使常驻内存正比于注册租户数;
   共享 BM25 的文档频率统计跨租户互相污染,是检索质量缺陷;wikilink 图必须租户内闭合。
   三者共同指向同一结论:**租户的自然物化单位 = 一个目录 + 一组有状态组件实例**。
2. **不把租户塞进 job 参数 schema**:租户身份属于服务边界(认证结果),不属于业务参数。
   调用方在负载中携带租户字段是错误,必须显式拒绝。

## 3. 现状代码走读(改造依据)

改造计划的每一条都建立在以下已核实的代码事实上:

| # | 事实 | 位置 |
|---|---|---|
| F1 | `ApplicationContext` 是被动状态容器:`app_config` / `components`(两级 dict)/ `jobs` / `thread_pool` / `metadata`(应用生命周期共享可变状态) | `reme/components/application_context.py:23-37` |
| F2 | 组件与 step 的**所有**文件路径派生自唯一属性 `workspace_path`,它读 `self.app_context.app_config.workspace_dir`;metadata 持久化路径同源(`workspace_metadata_path` / `component_metadata_path`) | `reme/components/base_component.py:37-50,179-189` |
| F3 | 依赖解析共三条链,全部以 `self.app_context` 为根:① step 用 `Ref` 描述符(kwargs → context → `app_context.components[enum][name]`);② 组件间用 `bind()`/`Dependency`,start 时 `_resolve_from_context` 查 `app_context.components`;③ 装配期 `Application._instantiate` 查注册表 `R` | `reme/steps/base_step.py:73-89`、`reme/components/base_component.py:140-175`、`reme/application.py:100-134` |
| F4 | job 调用链:`BaseJob._start` 把 `params["app_context"] = self.app_context` 烘焙进 `step_specs`;每次调用 `_build_steps()` 拷贝 params 重建全新 step 实例;`__call__` 为每次调用新建 `RuntimeContext` | `reme/components/job/base_job.py:37-70` |
| F5 | HTTP 端点是对 job 的闭包,`Request` schema `extra="allow"`,负载任意字段直达 job kwargs | `reme/components/service/http_service.py:74-84`、`reme/schema/request.py:9` |
| F6 | MCP 服务已有"服务端注入参数、调用方提供同名参数即报错"的先例(`injected_job_kwargs`) | `reme/components/service/mcp_service.py:54-72` |
| F7 | 跨调用可变状态走 `app_context.metadata`(如 search 去重的 `tool_contexts`) | `reme/steps/index/search.py:94-100` |
| F8 | 索引更新 step 从请求上下文消费 `changes` 批次,不依赖 watcher;对外的 `auto_resource` job 就是该模式 | `reme/steps/index/update_changes.py:82-94` |
| F9 | watch 循环是 `BackgroundJob`,构造时强制 `enable_serve=False`,由 `Application._start` 启动 | `reme/components/job/background_job.py:44-45`、`reme/application.py:182-199` |
| F10 | 组件按 Kahn 拓扑序启动、逆序关闭;`Application._setup_workspace_directories` 在构造期建 workspace 子目录 | `reme/application.py:51-65,138-199` |

**由 F2+F3+F7 得出本方案的核心实现决策**:组件和 step 从不直接持有 workspace 路径或
兄弟组件,一切经由 `self.app_context` 派生。因此**不修改** `workspace_path`、`Ref`、
`_resolve_from_context` 的任何逻辑——只需给每个租户一个与 `ApplicationContext` 同形
(duck-typing)的**租户视图对象**,把它作为 `app_context` 注入该租户的组件与 step,
三条解析链、路径派生、状态桶即自动全部租户化。

## 4. 核心机制:`TenantContext` = ApplicationContext 的租户视图

新增类 `TenantContext`,与 `ApplicationContext` 同形,字段语义:

| 字段 | 内容 | 说明 |
|---|---|---|
| `app_config` | 全局 config 的浅拷贝,仅 `workspace_dir` 重写为 `workspaces_root/<tenant_id>` | Pydantic `model_copy(update=...)`;F2 的路径派生随之全部生效 |
| `components` | 两级链式视图:租户作用域的枚举 → 本租户实例;其余枚举 → 透传全局 | F3 的三条解析链无感知生效;组件间 `bind` 也解析到本租户的兄弟实例 |
| `metadata` | 租户私有 dict | F7 的 `tool_contexts` 等状态天然隔离 |
| `jobs` | 透传全局 job 表(经租户绑定包装,见 M6) | step 内 `run_job` 嵌套调用不丢失租户 |
| `thread_pool` / `service` | 透传全局 | 共享资源 |

划分轴是**"是否引用 `workspace_path` / `app_context.jobs`"**,不是"是否持有可变状态"。
凡是直接或间接派生工作目录、相对路径或 job 工具的组件,即便无状态,也必须租户作用域,
否则会在错误的 workspace 上读写(详见 M8 的写路径分析)。

租户作用域的组件枚举(常量 `TENANT_SCOPED_TYPES`):`FILE_STORE`、`KEYWORD_INDEX`、
`FILE_GRAPH`、`FILE_CATALOG`、`EMBEDDING_STORE`、**`FILE_CHUNKER`**、**`AGENT_WRAPPER`**。
全局共享(完全 workspace 无关):`AS_LLM`、`AS_EMBEDDING`、`TOKENIZER`、`SERVICE`、`CLIENT`。

> **为何 `FILE_CHUNKER` 与 `AGENT_WRAPPER` 必须租户作用域**(修正早期"无状态即可共享"
> 的判断,Q1 据此定案):
> - `FILE_CHUNKER.chunk()` 调 `to_workspace_relative(path)`(`markdown_file_chunker.py:122`),
>   共享则相对全局根算路径 → 索引路径错乱;
> - `AGENT_WRAPPER` 被 `auto_memory`/dream 用于蒸馏,其 `cwd` = `workspace_path`
>   (`base_agent_wrapper.py:36-46`),且把记忆落盘交给从 `app_context.jobs` 解析的 job 工具
>   (daily_write/edit/write,`base_agent_wrapper.py:102`)。共享则租户 A 的蒸馏结果会写入
>   全局/其它 workspace —— **记忆写路径串租户**。二者实例化开销≈0(agentscope 内进程
>   wrapper、chunker 均轻量),租户化代价可忽略。

> **跨作用域 bind 必须显式支持**:租户私有的 `embedding_store` 依赖全局共享的 `as_embedding`
> (租户→全局),`file_store` 依赖租户私有的 `keyword_index`/`file_graph`(租户→租户)。
> `TenantContext.components` 的链式视图两向都要能解析(测试见 T11)。

## 5. 逐模块改造计划

### M1 `reme/enumeration/` — 租户作用域常量(0.5 人日)

- 新增 `TENANT_SCOPED_TYPES: frozenset[ComponentEnum]` = {`FILE_STORE`、`KEYWORD_INDEX`、
  `FILE_GRAPH`、`FILE_CATALOG`、`EMBEDDING_STORE`、`FILE_CHUNKER`、`AGENT_WRAPPER`}(见第 4 节
  划分轴)。
- 不改 `ComponentEnum` 既有成员。

### M2 `reme/schema/` — 配置模型(1 人日)

`ApplicationConfig` 新增可选字段(默认关闭,不影响现有配置反序列化,兼容
`tests/unit/test_embedded_consumer_compat.py` 的约束):

```yaml
multi_tenant:
  enabled: false
  workspaces_root: ""            # enabled 时必填
  max_active_tenants: 300        # LRU 上限
  tenant_idle_close_seconds: 1800
  tenant_id_pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"   # 校验,禁路径分隔符
  auth:
    backend: static_token_map    # 见 M7
    tokens: {}                   # ${ENV} 展开由现有 config_parser 提供
```

### M3 `reme/components/tenant_context.py`(新文件,1 人日)

第 4 节的 `TenantContext`。实现要点:

- 不继承 `ApplicationContext`(它构造时会解析 config,见
  `application_context.py:25`),独立类 + 同形属性;
- `components` 用只读 Mapping 视图实现链式查找,禁止运行期向全局表写入;
- 持有 `tenant_id`、`last_used_at`、`in_flight`(在途 job 计数)、`asyncio.Lock`
  (启停互斥),供 M4 使用。

### M4 `reme/components/tenant_manager.py`(新文件,3 人日)

`Application` 持有的租户生命周期管理器:

- `async get(tenant_id) -> TenantContext`:
  1. 校验 `tenant_id_pattern`;
  2. 命中缓存则更新 LRU 并返回;
  3. 未命中:在租户锁内——建目录(复用 `Application._setup_workspace_directories`
     的子目录清单,`application.py:51-65`,提取为函数)→ 按 `TENANT_SCOPED_TYPES`
     从全局组件 config 实例化组件集(复用 `Application._instantiate`,
     `application.py:100-134`,提取为函数)→ 对该子集跑现有 Kahn 拓扑排序
     (`application.py:138-159`,提取为函数)→ 逐个 `start()`(即加载该租户 zstd 快照、
     触发既有的索引自修复逻辑)。
- 驱逐:超过 `max_active_tenants` 时从 LRU 尾部取 `in_flight == 0` 的租户,逆拓扑序
  `close()`(file_store 的 close 即持久化,现有语义,F10);在途租户跳过,等下次驱逐。
  空闲超时驱逐由一个内部维护协程周期执行(它是 Application 自身的任务,不是配置里的
  BackgroundJob)。
- `async close_all()`:接入 `Application._close`(`application.py:212-222`),关停时
  全量落盘。
- 实例化/拓扑/建目录三段逻辑从 `Application` **提取复用而非复制**,这是本模块的主要
  重构内容。

### M5 `reme/application.py` — 装配分支(2 人日)

`multi_tenant.enabled` 时:

- `_init_components` 跳过 `TENANT_SCOPED_TYPES` 中的组件(其 config 保留为模板供 M4 用),
  其余照常实例化;
- 构造 `TenantManager` 挂到 `ApplicationContext`;
- `_setup_workspace_directories` 只确保 `workspaces_root` 存在,子目录延迟到租户首启;
- 配置校验:`jobs` 中存在 `BackgroundJob`/`CronJob` 时**启动即报错**(见 M8 的理由),
  错误信息指引用户使用多租户配置模板;
- `_close` 先 `tenant_manager.close_all()` 再走现有逆序关停。

单租户模式(默认)代码路径零改动。

### M6 `reme/components/job/base_job.py` — 调用期租户注入(1.5 人日)

- `__call__` 增加保留 kwarg(如 `__tenant__`,服务层注入,普通调用方无法命名合法的
  双下划线参数经 JSON schema 进入):存在时经 `tenant_manager.get()` 换取
  `TenantContext`,并在执行期间对其 `in_flight` 计数 +1/-1;
- `_build_steps` 支持按调用覆盖 `params["app_context"]`(F4 中 params 本就逐调用拷贝,
  改动为一行覆盖),`tenant_id` 同时写入 `RuntimeContext.data` 供日志与诊断;
- `TenantContext.jobs` 返回的包装对象在转发 `__call__` 时自动补 `__tenant__`,保证
  step 内 `run_job` 嵌套调用(`base_step.py:185-190`)不丢租户。

### M7 `reme/components/service/` — 边界身份(3 人日)

- 新增 `TenantResolver` 小类族(注册进现有注册表 `R`,新枚举 `TENANT_RESOLVER`):
  - `static_token_map`:`Authorization: Bearer <token>` → tenant_id,token 表来自 M2
    配置(值经 `${ENV}` 展开);
  - `trusted_header`:直读 `X-Reme-Tenant`(部署在可信网关之后的模式);
  - 接口只有 `resolve(headers) -> str | None`,后续扩 JWT/mTLS 不动框架。
- `http_service._add_json_job` 端点:解析失败返回 401;负载中出现 `tenant` /
  `tenant_id` / `__tenant__` 字段返回 400(F5 的 `extra="allow"` 要求显式拒绝,
  语义对齐 F6 的冲突规则);成功则以 `__tenant__=<tenant_id>` 调 job。SSE 端点同理。
- `mcp_service`:streamable-http/sse 传输经 FastMCP 的请求头 API 取每请求 header 走同一
  Resolver;`stdio` 传输在多租户模式下启动即拒绝(单连接单进程,无边界可言,文档写明)。
- 多租户模式下 `help`/`version`/`health_check` 等只读运维 job 是否豁免认证:默认不豁免,
  评审点 Q2。

### M8 记忆写路径、巩固触发与配置模板(2.5 人日)

本模块是多租户与 file-native 记忆管道(session → daily → digest)对齐的核心,分三层。

**(a) 捕获与蒸馏分离(`record` 的粒度对齐)。**
`auto_memory` 一次调用 = 一次 `agent_wrapper.reply` 的 LLM 蒸馏(`auto_memory.py:299`),
且蒸馏的是传入的全量 `format_history(messages)`(`:295`),自身不算增量。消费方(如
agentscope-java 的 `record(List<Msg>)`,每回合触发)若每轮传全量历史,则每轮重蒸馏整段,
成本随会话长度二次增长。对齐做法——利用 ReMe 内部本已分离的两段
(`_save_session_messages` 便宜的会话 append 总是执行;`agent_wrapper.reply` 昂贵蒸馏):

- `session_append`(新增轻量 job,或 `auto_memory` 的 capture-only 模式):仅按 session_id
  把增量追加到 `session/dialog/<id>.jsonl`,不调 LLM;
- `auto_memory`(蒸馏):由会话结束或定时触发,读取该 session 自上次以来的增量蒸馏为 daily。

调用方只传增量;或由 `session_append` 以 uuid/序号去重承担增量语义(复用 `auto_memory_cc`
的 uuid 去重思路)。

**(b) 巩固触发者(digest 层的对齐——本方案此前的悬空点)。**
多租户禁用 `dream_cron` 后,daily→digest 的 `auto_dream` 失去自动触发者;若消费契约(如
agentscope 的 `record`/`retrieve`)也不含巩固钩子,则 **digest 长期层永不形成**——而这正是
v4 分层记忆的价值所在。必须显式补触发,二选一或并用:

- **ReMe 侧(默认)**:新增按租户的巩固调度器(Application 自身的维护协程,非配置
  BackgroundJob),每周期挑"当天产生新 daily 的租户"入队,带全局并发上限离峰执行
  `auto_dream`;
- **宿主侧**:调用方在会话结束/定时显式 `POST /auto_dream`(带租户头)作为可选加速。

评审点见 Q4。

**(c) 配置模板 `reme/config/multi_tenant.yaml`(`config=multi_tenant` 即用):**

- 移除三个 watch 循环与 `dream_cron`(N 租户不能各挂 OS 文件监视器;写入均过 API;
  这也是 M5 启动校验的依据);
- `write` / `daily_write` / `edit` / `move` / `delete` 追加 `update_index_step`(F8:该 step
  从上下文消费 `changes`,纯配置组合),保证写后即可检索;
- `agent_wrapper` 只允许内进程 `agentscope` 后端;`claude_code` / `codex` 等子进程后端在
  多租户模板中**禁用**——它们既无法按租户廉价实例化,`cwd` 也约束不住子进程对服务器全盘
  的访问(与 `auto_memory_cc` 移除同理);
- 移除 `auto_memory_cc`(见 M9)。

### M9 steps 核查(0.5 人日,预期零改动 + 三个例外)

论证:step 只经 F2(路径)、F3(组件)、F7(状态)接触外界,三者已被 `TenantContext`
覆盖(且 `FILE_CHUNKER`/`AGENT_WRAPPER` 已在第 4 节/M1 归入租户作用域,写路径不再串租户),
故 `file_io`/`index`/`evolve` 全目录**零改动**。例外核查:

- `common/status.py`:遍历组件报告内存(`_TRACKED_COMPONENT_TYPES` 恰为租户作用域集合)。
  多租户下改为:report 当前租户组件 + 附 TenantManager 汇总(活跃租户数、进程 RSS);
- `common/health_check.py` / `help.py`:核对其组件遍历路径,预期经 app_context 自然
  租户化,验证即可;
- `evolve/auto_memory_cc.py`:从**服务器本机磁盘**(`~/.claude/projects`,经 `CLAUDE_CONFIG_DIR`)
  按 session_id 解析 Claude Code transcript——数据源在租户 workspace 边界**之外**,
  `TenantContext` 约束不到,既功能不成立又是越界读盘风险,多租户模板中移除该 job。远程租户
  记录 CC 会话改用普通 `auto_memory`(消息由调用方经请求体传入,数据从协议进而非从盘读);
- **越界读盘全量审计**:除 `auto_memory_cc` 外,复核所有 env 驱动的绝对路径
  (`mcp_servers` 配置、agent 子进程注入 env),确认无第二个绕过 `workspace_path` 的读写点。

### M10 CLI / 客户端(1 人日,可后置)

`http_client` / `mcp_client` 支持从环境变量或 `key=value` 传 `Authorization` 头,
使 `reme search ...` 可直接打多租户服务。不阻塞 Phase 1 验收(curl 即可验证)。

## 6. 兼容性

- `multi_tenant.enabled=false`(默认)时:M5 的分支不触发、M6 的保留字段不出现、
  M7 的 Resolver 不装配——现有全部单测语义不变,这本身是验收条件(见第 7 节 T0);
- 租户 workspace 与单机 workspace 逐字节同构,迁入迁出 = `mv` + 租户内 `reindex`;
- 公共 job 参数 schema 零新增字段;`Response` 结构不变。

## 7. 测试计划

| # | 测试 | 验证点 |
|---|---|---|
| T0 | 现有 `tests/unit` 全量 | 单租户路径零回归(M5 分支关闭时) |
| T1 | 双租户写入/检索交叉验证 | A 写入的内容 B 搜不到、读不到、traverse 不到 |
| T2 | `tool_contexts` 去重隔离 | 同一 `tool_context_id` 在两租户互不影响(F7 隔离) |
| T3 | LRU 驱逐 → 重新激活 | 驱逐触发落盘;再次 `get` 后 chunk/索引与驱逐前一致 |
| T4 | 在途保护 | `in_flight>0` 的租户不被驱逐;job 结束后可驱逐 |
| T5 | 负载租户字段拒绝 | payload 带 `tenant_id` → 400;无凭据 → 401;凭据↔租户映射正确 |
| T6 | 嵌套 `run_job` 租户传递 | step 内调 job 仍作用于同租户(M6 包装) |
| T7 | 组件共享性 | 两租户的 `as_llm` 是同一实例;`file_store` 是不同实例 |
| T8 | 并发 | 两租户并发 job 无交叉写(沿用 `test_write_metadata_lock` 的模式) |
| T9 | 启动校验 | 多租户 + BackgroundJob 配置 → 启动报错 |
| T10 | 写路径入索引 | 多租户模板下 write 后立即 search 可命中(M8 组合 job) |
| T11 | 跨作用域 bind | 租户 `embedding_store`→全局 `as_embedding` 解析成功;`file_store`→租户 `keyword_index` 解析成功 |
| T12 | 巩固触发者 | 有新 daily 的租户被调度器入队并生成 digest;无新 daily 的租户不触发(M8b) |
| T13 | 写路径不串租户 | 租户 A 的 `auto_memory` 蒸馏产物只落在 A 的 `daily/`,不出现在全局/其它租户(验证 M1 分类修正) |
| T14 | 会话预热 | 会话开始预热后,首个 `retrieve` 不触发冷加载尖峰 |

集成测试:双租户端到端(HTTP 认证 → session_append → auto_memory → auto_dream → search →
proactive),复用 `tests/integration/_workspace_fixture.py` 的隔离约定。

## 8. 工作量与依赖

```
M1 ─┐
M2 ─┼─→ M3 ─→ M4 ─→ M5 ─→ M6 ─→ M7 ─→ 集成测试
    │                └────────→ M8(与 M6 并行)
    │                           M9(与 M7 并行)
    └──────────────────────────→ M10(后置)
```

| 模块 | 人日 | | 模块 | 人日 |
|---|---|---|---|---|
| M1 常量 | 0.5 | | M6 job 注入 | 1.5 |
| M2 配置 | 1 | | M7 服务边界 | 3 |
| M3 TenantContext | 1 | | M8 写路径/巩固/模板 | 2.5 |
| M4 TenantManager | 3 | | M9 steps 核查 | 0.5 |
| M5 Application | 2 | | M10 客户端 | 1 |
| 单测 T0–T14 | 5 | | 集成/文档 | 2 |

**合计 ≈ 23 人日**(不含评审与上游沟通)。关键路径 M3→M4→M5→M6→M7;M8(写路径与巩固触发)
与 M7 并行,但它是"长期记忆分层"能否真正在多租户下成立的决定性模块,不可省。

## 9. 风险与开放问题

风险:

- **R1 内存毛刺**:租户首启加载 + 大文件分块的瞬时峰值可能挤压 LRU 预算。缓解:
  `ChangeApplyStep` 已有 `batch_memory_*` 背压参数(`update_changes.py:26-37`);
  `max_active_tenants` 留安全余量;`status` job 提供实测依据。
- **R2 file_store 变体**:`faiss_local_file_store` 与 `local_file_store` 均为
  workspace 作用域对象,理论上同样适用,但 faiss 原生句柄的多实例行为需 T3/T8 覆盖。
- **R3 embedding 后台回填**:file_store 启动时可能调度回填任务
  (`local_file_store.py:257`),按租户实例各自持有、close 取消——需在 T3 中显式断言
  驱逐后无游离任务。
- **R4 日志归因**:多租户共享日志流,建议 M6 注入时 `logger.bind(tenant=...)`,
  属低成本增强。
- **R5 LRU 抖动**:活跃工作集 > `max_active_tenants` 时陷入"驱逐→重载"循环,每次重载
  重跑索引自修复/回填扫描。落盘本身原子安全(`local_file_store.py:429` "Atomically
  rewrite"),故非损坏风险而是开销风险。缓解:`max_active_tenants` ≥ 峰值并发工作集;
  暴露抖动率指标;会话开始预热(T14)。
- **R6 巩固缺触发**:若 M8(b)未落地,digest 长期层不形成,多租户下 v4 记忆退化为仅
  daily 层。这是"方案完整性"风险而非性能风险,M8 已作为决定性模块处理。

开放问题(请评审拍板):

- **Q1**〔已定案〕`FILE_CHUNKER`/`AGENT_WRAPPER` 归**租户作用域**——划分轴是"是否引用
  `workspace_path`/`jobs`",非"是否有状态"(见第 4 节)。
- **Q2** 运维类 job(`version`/`health_check`)是否豁免认证?豁免方便探活,不豁免面一致。
- **Q3** 保留 kwarg 命名 `__tenant__` vs 独立调用通道(如 `BaseJob.call_scoped()`)。
  前者改动最小,后者类型更显式。
- **Q4** 巩固触发:ReMe 侧按租户调度器(默认)之外,是否同时开放宿主侧显式 `auto_dream`?
  两者并用时如何避免重复巩固(建议以"当天是否已巩固"的 daily 标记去重)。
- **Q5** `record` 拆分:新增独立 `session_append` job,还是给 `auto_memory` 加 capture-only
  模式?前者契约更清晰,后者复用现有 job。
- **Q6** 默认认证 resolver:鉴于 5000 用户 + 增删,`static_token_map` 仅作演示,生产默认应为
  `trusted_header`(宿主认证,ReMe 不维护凭据)还是 JWT/DB 动态 resolver?
- **Q7** 租户生命周期:是否新增租户级 `memory_export` / `memory_delete` job(GDPR/离网),
  以及是否需要"会话预热"job 供宿主在会话开始时调用。
- **Q8** Phase 0(多 `Application` 同进程共存的契约化测试)是否作为独立 PR 先行?
  它同时为本提案的 M4 提供回归安全网。
