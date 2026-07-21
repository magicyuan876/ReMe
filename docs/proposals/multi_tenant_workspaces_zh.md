# 提案:原生多租户 Workspace

- 状态:讨论稿
- 关联 issue:[#368](https://github.com/agentscope-ai/ReMe/issues/368)
- 范围:`reme` 服务层、应用装配与组件生命周期。不改变 workspace 的磁盘布局与记忆文件格式。

## 1. 概要

当前一个 `Application` 进程绑定一个 workspace。需要服务大量终端用户的调用方(issue #368
的场景)只有两条路:每个用户起一个 ReMe 进程(进程固定成本随用户数成倍复制),或让所有用
户共享一个 workspace(检索路径与记忆巩固管道都会把不同用户的记忆混在一起)。

本提案把租户变成 ReMe **内核**的一等请求维度,同时保持每个租户的记忆仍是一个普通、自包含
的 workspace 目录:

- 一个服务进程承载多个租户。
- 每个租户对应 `workspaces_root/<tenant_id>/`,就是一个普通的 ReMe workspace,所有者依然
  可以直接阅读、编辑、备份、整体带走。
- 有状态数据组件按租户惰性实例化,常驻数量有 LRU 上限;无状态组件全局共享。
- 租户身份在服务边界解析并注入请求上下文,**不接受**请求负载中自报的租户字段。

本提案刻意**不**引入共享数据面(一个 store 存所有租户的 chunk),原因见第 4 节。

## 2. 动机

Issue #368 问的是"召回时如何限定当前用户"。现有选项:

| 方案 | 问题 |
| --- | --- |
| 每用户一个 ReMe 进程 | 解释器+import、端口、事件循环等固定成本按用户数复制,几十个用户以上不可行。 |
| 共享 workspace + frontmatter 标签过滤 | 只覆盖 `search` 读路径。`read`/`list`/`traverse` 无约束,链接扩展不过滤邻居,写管道(`auto_memory` → daily → `auto_dream` → digest)仍会把不同用户的事实合并进共享的 daily 笔记、digest 节点和 `interests.yaml`。 |
| SDK 嵌入,宿主进程自管 N 个 `Application` | 今天可行(QwenPaw 集成路径),但每个采用者都要在 ReMe 之外重复实现路由、生命周期与安全细节,且没有明示的兼容性契约。 |

数千终端用户的部署需要第三种方案的经济性,加上官方一等支持:单一服务、协议内租户、覆盖
*所有* job(含巩固管道)的按租户隔离。

## 3. 目标与非目标

目标:

1. 一个 ReMe 服务进程服务 N 个租户,所有 job(检索、文件 I/O、frontmatter、
   auto_memory/auto_dream 管道)按租户隔离。
2. 保持 file-native 契约:每个租户的记忆是普通 workspace 目录;索引与缓存按租户可重建;
   租户内 `reme reindex` 语义不变。
3. 内存有界:常驻租户状态有上限、可驱逐;不活跃租户只占磁盘。
4. 完全向后兼容:单 workspace 部署行为与今天完全一致,多租户模式显式开启。

非目标:

1. 跨租户共享检索索引(见第 4 节)。
2. 计费、配额、按租户凭据管理——这些是宿主的职责,本设计只预留空间。
3. 跨租户搜索与共享,不在本提案范围内。

## 4. 为什么不做共享数据面

教科书式多租户——一个 store、chunk 打租户标签、查询时过滤——适合 ReMe 0.2.x 的
Elasticsearch/ChromaDB 后端,但与 v4 的存储模型在三处冲突:

1. **持久化与内存。** `LocalFileStore` 启动时从 zstd JSONL 快照全量加载 chunk
   (`reme/components/file_store/local_file_store.py`)。共享 store 意味着启动即加载全部
   租户,常驻内存正比于注册租户数而非活跃租户数;把快照拆成按租户分段的惰性加载,实质上
   就是换了名字的按租户 store。
2. **BM25 统计量。** 共享关键词索引让文档频率统计跨租户混杂:一个租户的词汇分布会扭曲另
   一个租户的打分。这是检索**质量**缺陷,不只是过滤成本问题。按租户维护统计量等价于按租
   户的索引实例。
3. **Wikilink 图。** `[[链接]]` 的解析与游走必须限定在租户内;按租户分区的图等价于按租户
   的 `file_graph` 实例。

结论:在 file-native + 内存驻留索引的设计下,租户的自然单位就是*一个目录加一组有状态组件
实例*。因此本提案保留按租户组件集,把它们的管理下沉进内核——正确性只需实现一次,而不是
由每个宿主应用各自重建。

## 5. 设计

### 5.1 租户模型

```
<workspaces_root>/
├── <tenant_id>/            # 普通 ReMe workspace,布局不变
│   ├── metadata/
│   ├── session/
│   ├── resource/
│   ├── daily/
│   └── digest/
└── ...
```

`tenant_id` 是经校验的不透明标识(安全字符集、不含路径分隔符)。workspace 的所有既有保证
——用户拥有的文件是唯一事实源、索引可重建、可迁移——按目录逐一成立。租户迁入迁出多租户
部署就是移动目录。

### 5.2 组件分类

组件分为两组:

- **共享、对 workspace 数据无状态**:`as_llm`、`tokenizer`、`file_chunker`、
  `agent_wrapper`。与今天一样各一份实例。
- **租户作用域、有状态**:`file_store`、`keyword_index`、`file_graph`、`file_catalog`
  (全部具名实例)、`embedding_store`。按活跃租户用现有组件配置实例化。

这个分类是改造可控的主要原因:只有第二组需要生命周期管理,而它们本来就是 workspace 作用
域的对象,持久化都在 `<workspace>/metadata/` 之下。

### 5.3 TenantManager

`ApplicationContext` 持有的新对象:

- `get(tenant_id) -> TenantContext`:返回该租户的组件集,首次使用时实例化并
  `start()`(冷启动即加载该租户的 zstd 快照;个人规模数据为亚秒级)。
- LRU 驱逐:`max_active_tenants` 限制常驻租户数;驱逐时等待该租户在途 job 结束,然后
  `close()` 组件集(与正常关停一样持久化 chunk store)。`tenant_idle_close_seconds`
  提前关闭空闲租户。
- 并发:同一租户的实例化串行;不同租户的 job 与今天一样在共享事件循环上并发。

`TenantContext` 除组件实例外,还持有当前 `ApplicationContext.metadata` 中全局可变状态
(如 `tool_contexts`)的按租户切片,杜绝跨租户的跨调用状态泄漏。

### 5.4 请求流与身份

- 租户在服务边界解析——HTTP 与 MCP 服务上的认证钩子——并注入 job 调用;请求负载中出现
  租户字段将被**拒绝**。这是把 `MCPService.add_job` 中现有的 `injected_job_kwargs` 冲突
  规则(`reme/components/service/mcp_service.py`)从"每服务静态"推广为"每请求"。
- `RuntimeContext` 携带解析后的 `TenantContext`;`BaseJob.__call__` 在构建 step 前完成一
  次解析。
- 认证钩子可插拔(API key / bearer token / mTLS 身份映射到 `tenant_id`);第一版内置一个
  静态 token 映射即可。

### 5.5 实现咽喉点

两个现有的间接层让数据面改动很小:

1. **路径解析。** 所有组件与 step 的文件路径都来自唯一的
   `BaseComponent.workspace_path` 属性(`reme/components/base_component.py`),它读取
   `app_context.app_config.workspace_dir`。多租户模式下,它返回当前调用绑定租户的
   workspace。全部文件 I/O、chunker 路径、metadata/持久化路径随之生效。
2. **组件解析。** step 获取组件全部经由 `Ref` 描述符的三级回退(kwargs → context → 应用
   注册表,`reme/steps/base_step.py`)。对租户作用域的组件类型,第三级改为查本次调用的
   `TenantContext`;共享组件类型不变。

job 代码、step 代码、schema、workspace 文件格式均不变。

### 5.6 多租户模式下的 job

- **Watch 循环**(`index_update_loop`、`resource_watch_loop`、`digest_watch_loop`)禁
  用:N 个租户不可能各挂操作系统文件监视器,且服务化部署下所有写入本来就经过 API。写路径
  改为显式索引更新:`update_index_step` 本就从请求上下文消费 `changes` 批次
  (`reme/steps/index/update_changes.py`),与对外的 `auto_resource` job 同一模式,因此
  "write 后接索引更新"是配置级组合。
- **`dream_cron`** 改为按租户排队的 job:只把当天有新 daily 笔记的租户入队,并设全局并发
  上限——既约束 LLM 开销,也避免所有租户在同一时刻被唤醒。
- 全部请求/响应类 job(`search`、`read`、`write`、`edit`、`traverse`、`auto_memory`、
  `auto_dream`、`proactive` 等)在租户组件集上原样工作——包括链接扩展,因为图本身就是租
  户作用域的,天然不越界。

### 5.7 配置面

```yaml
service:
  backend: http
  multi_tenant:
    enabled: true            # 默认 false —— 单 workspace 模式不受影响
    workspaces_root: /var/lib/reme/workspaces
    max_active_tenants: 300
    tenant_idle_close_seconds: 1800
    auth: static_token_map   # 可插拔解析器:凭据 -> tenant_id
```

`enabled: false`(默认)时一切照旧:单 workspace、watch 循环开启、行为与今天一致。

## 6. 向后兼容与迁移

- 单租户模式为默认,其代码路径不动。
- 租户 workspace 与单用户 workspace 逐字节同构;双向迁移就是 `mv` 加 `reme reindex`。
- 公共 schema 不新增必填字段;租户身份存在于服务边界,不进入 job 参数 schema。

## 7. 容量预期

按个人规模租户估算(数千个 markdown 文件、不开 embedding):

- 每活跃租户常驻成本约几十 MB(chunk 文本 + BM25 + 图;可用 `status` job 按部署实测)。
- 5,000 注册租户、约 5% 并发常驻 ≈ 250 活跃租户 ≈ 单分片进程个位数 GB。按
  `hash(tenant_id)` 分片到少量进程即可水平扩展,且因每个租户目录只属于一个分片,无跨进程
  协调。
- 不活跃租户只占磁盘。

## 8. 分阶段计划

1. **Phase 0 —— 嵌入契约。** 文档化并测试"同进程多 `Application` 实例可共存"(QwenPaw
   嵌入路径目前只用单实例;注册表在导入后只读,实例状态都在 `ApplicationContext` 上)。这
   一步立即解锁宿主层路由方案,且独立于本提案其余部分都有价值。
2. **Phase 1 —— 内核租户。** `TenantManager`、`TenantContext`、两处咽喉点改造(5.5)、服
   务边界身份注入(5.4)、多租户配置(5.7)、watch 循环与 dream 调整(5.6)。
3. **Phase 2 —— 运营。** LLM 重任务的按租户调度公平性、配额钩子、更丰富的认证解析器、按
   租户的 `status` 报告。

## 9. 已考虑的替代方案

- **Frontmatter 标签过滤**(#368 中初步给出的方向):可作为轻量的*合作式*过滤,但只约束
  `search`;`read`/`list`/`traverse` 与链接扩展不受约束;巩固管道没有用户维度,不同用户
  的记忆仍会合并进共享的 daily/digest 文件。且它需要的包含语义在现有
  `_matches_search_filter` / `_value_matches`
  (`reme/components/file_store/local_file_store.py`)中并不存在。
- **每租户一个进程**:隔离正确,规模化后固定成本不可承受。
- **宿主层路由 N 个嵌入式 Application**:与本提案经济性等价、今天即可实施,但每个采用者
  都要在无兼容契约的前提下重复实现路由、LRU 生命周期、身份与写路径索引。本提案就是把同一
  架构下沉进内核,只实现一次。
- **共享数据面 + 租户标签 chunk**:因第 4 节的原因不采纳。

## 10. 安全考量

- 租户身份由服务边界断言,永不采信调用方负载。
- 现有路径处理已归一化为 workspace 相对形式;按租户的 `workspace_path` 把所有 job(含
  `move`/`delete`)约束在租户目录内。
- 按租户的 `TenantContext` 状态消除了经由 `tool_contexts` 这类共享内存桶的跨租户泄漏。
- 多租户模式移除 watch 循环,把攻击面收敛到 API。

## 11. 待讨论问题

1. `tenant_idle_close_seconds` 驱逐时,BM25 状态是否需要增量持久化,还是关停时持久化(现
   行为)已足够?
2. `dream` 调度应进内核(新的队列型 job 类型),还是留在 Phase 2 作为宿主职责?
3. Phase 0 的多实例保证是否应成为 `docs/` 中公开 SDK 契约的一部分?
