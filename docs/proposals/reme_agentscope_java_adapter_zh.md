# 适配契约:agentscope-java ↔ v4 多租户 ReMe(长期记忆)

- 状态:内部评审稿(未发布)
- 关联:[原生多租户 Workspace 提案](multi_tenant_workspaces_zh.md) · [agentscope-ai/ReMe #368](https://github.com/agentscope-ai/ReMe/issues/368)
- 目标:让 agentscope-java(2.x)把改造后的多租户 ReMe(v4)作为长期记忆后端,支持约 5000 用户。
- 范围:定义两侧的操作映射、身份/会话对齐、增量与巩固语义、错误与降级。不含模型凭据/计费(宿主职责)。

---

## 1. 结论

可以适配,且两侧模型天然契合:agentscope-java 的 agent 无状态、每次调用经
`RuntimeContext` 携带 `userId`/`sessionId`,这正是多租户 ReMe 需要的 per-request 身份。
但**官方现成的 `agentscope-extensions-reme` 用不了**——它面向老版 ReMe(`workspace_id` body
字段、`add` 端点、端口 8002),与 v4 的 API 不兼容。需自写一个薄适配器,对接 v4 的 HTTP API,
并把 `userId` 经 `X-Reme-Tenant` 头透传为租户身份(对应主提案的拓扑 A / `trusted_header`)。

关键前提:agentscope-java 的 `LongTermMemory` SPI 只有 `record`/`retrieve` 两个钩子,**没有
"巩固"这一步**,方法签名也不带身份。因此本契约必须额外解决三件事:身份如何 per-call 传入、
写入如何避免每回合全量 LLM 蒸馏、digest 长期层由谁触发。

---

## 2. 两侧接口

**agentscope-java 侧(消费者)** —— `io.agentscope.core.memory.LongTermMemory`(已 `@Deprecated`,
但仍是外部长期记忆的接入点):

```java
Mono<Void>   record(List<Msg> msgs);   // 每回合回复后由框架调用
Mono<String> retrieve(Msg msg);        // reasoning 前由框架调用,返回注入上下文的文本
```

- per-call 的 `userId`/`sessionId` 在 `RuntimeContext`,不在方法签名里。
- `longTermMemoryMode`:`STATIC_CONTROL`(框架自动 record/retrieve)/`AGENTIC`(agent 用工具
  自主管理)/`BOTH`。本契约默认 `STATIC_CONTROL`。

**多租户 ReMe 侧(提供者)** —— HTTP job 端点,均需 `X-Reme-Tenant` 头:

| 端点 | 用途 |
|---|---|
| `POST /search` | 混合检索,返回 `answer` + `metadata.results` |
| `POST /session_append` | 【新增,M8a】按 session_id 追加对话增量,不调 LLM |
| `POST /auto_memory` | LLM 蒸馏对话增量为 daily 卡片 |
| `POST /auto_dream` | 巩固 daily → digest(长期层) |
| `POST /proactive` | 读取兴趣话题 |

---

## 3. 操作映射

### 3.1 `retrieve(Msg)` → `POST /search`

```
POST /search
X-Reme-Tenant: <userId>
{"query": "<msg.getTextContent()>", "limit": 5}
→ 取 response.answer 作为 retrieve 的返回字符串
```

- 租户 = `ctx.getUserId()`;查询 = 最新用户消息文本。
- **延迟敏感**:`retrieve` 在 reasoning 前,落在用户可感知延迟上。冷租户首个 `retrieve` 会触发
  workspace 加载尖峰 → 见 §5 会话预热。
- 检索层次可选:长期记忆召回建议偏 `digest`(通过 search 的过滤/权重,或专用范围参数)。

### 3.2 `record(List<Msg>)` → 拆为"捕获 + 延迟蒸馏"(核心对齐)

**不要**把 `record` 直接映射到 `auto_memory`。原因:`auto_memory` 每次调用都是一次
`agent_wrapper.reply` 的 LLM 蒸馏,若每回合触发且传全量历史,成本随会话长度二次增长。对齐为:

**每回合(record 调用时)—— 只做廉价捕获:**
```
POST /session_append
X-Reme-Tenant: <userId>
{"session_id": "<sessionId>", "messages": [<本回合增量,{role,content}>]}
```
- 适配器只传**自上次 record 以来的增量**(见 §4 增量语义),不调 LLM,延迟可忽略。

**会话结束 / 定时 —— 蒸馏为 daily:**
```
POST /auto_memory
X-Reme-Tenant: <userId>
{"session_id": "<sessionId>"}      # 读取该 session 自上次蒸馏以来的增量
```
- 触发时机:agentscope 会话关闭钩子,或宿主定时批处理。

> 若初期想最简实现,可让 `record` 直接调 `auto_memory` 且只传增量——但要接受每回合一次
> LLM 蒸馏的成本;规模化前应切到拆分方案。

### 3.3 巩固 `auto_dream`(digest 长期层——SPI 没有的钩子)

`LongTermMemory` 契约里没有巩固步骤,而多租户 ReMe 又禁用了 `dream_cron`。若不补触发,
**digest 长期层永不形成**,retrieve 只能命中 daily。两种触发方式(对应主提案 M8b / Q4):

- **ReMe 侧(默认)**:ReMe 内建按租户巩固调度器,自动挑"当天有新 daily"的租户离峰跑
  `auto_dream`。适配器无需感知。
- **宿主侧(可选加速)**:适配器/宿主在会话结束或每日定时显式:
  ```
  POST /auto_dream
  X-Reme-Tenant: <userId>
  ```
  两者并用时以"当天是否已巩固"标记去重。

### 3.4 `proactive`(可选)

`POST /proactive` + 租户头 → 取兴趣话题,由宿主决定是否经 `onSystemPrompt` 注入。

---

## 4. 身份、会话与增量语义

| 概念 | agentscope-java | 多租户 ReMe |
|---|---|---|
| 租户 | `RuntimeContext.userId` | `X-Reme-Tenant` 头 → `tenant_id` |
| 会话 | `RuntimeContext.sessionId` | job 的 `session_id` 参数 |
| 身份来源 | 框架/宿主认证后设入 `RuntimeContext` | 服务边界 `trusted_header` resolver 解析头 |

- **身份必须由边界注入,禁止走 body**;适配器绝不把 `tenant_id` 放进 JSON 负载(ReMe 会 400)。
- **增量**:`session_append` 需要"自上次以来的新消息"。两种实现:
  (a) 适配器侧记住每个 sessionId 上次已发送的消息游标,只发增量;
  (b) 全量发送,由 ReMe `session_append` 以 uuid/序号去重(复用 `auto_memory_cc` 的去重思路)。
  推荐 (a),网络与解析成本最低。

---

## 5. 实现:两条落地路径

### 路径 A(推荐)—— 自写 middleware,直连 ReMe v4 HTTP

middleware 的方法签名**显式携带** `RuntimeContext ctx`,可直接 `ctx.getUserId()`,不依赖任何
隐式上下文,也不碰废弃的 SPI。一个 `WebClient`(连接池)服务全部 5000 用户。

```java
public class ReMeMemoryMiddleware implements MiddlewareBase {
    private final WebClient http;   // 单例,连接池共享

    // 召回:注入系统提示
    @Override public Mono<String> onSystemPrompt(Agent a, RuntimeContext ctx, String prompt) {
        return http.post().uri("/search")
            .header("X-Reme-Tenant", ctx.getUserId())
            .bodyValue(Map.of("query", latestUserText(ctx), "limit", 5))
            .retrieve().bodyToMono(ReMeResp.class)
            .map(r -> prompt + "\n\n[相关长期记忆]\n" + r.answer)
            .onErrorReturn(prompt);                 // 记忆故障不阻断对话
    }

    // 捕获:每回合结束后追加会话增量(廉价,不调 LLM)
    @Override public Flux<AgentEvent> onAgent(Agent a, RuntimeContext ctx, AgentInput in,
            Function<AgentInput, Flux<AgentEvent>> next) {
        return next.apply(in).concatWith(Flux.defer(() ->
            http.post().uri("/session_append")
                .header("X-Reme-Tenant", ctx.getUserId())
                .bodyValue(Map.of("session_id", ctx.getSessionId(), "messages", deltaOf(ctx, in)))
                .retrieve().bodyToMono(Void.class).thenMany(Flux.empty())
                .onErrorResume(e -> { log.warn("session_append failed", e); return Flux.empty(); })));
    }
}
```
蒸馏(`auto_memory`)与巩固(`auto_dream`)由会话结束钩子或宿主定时任务触发,不在热路径。

优点:租户传递显式、零猜测;不绑废弃 SPI;单适配器 + 单连接池。
代价:框架自带的 `longTermMemoryMode` 编排用不上,召回注入/写入时机自己定(即上面这段)。

### 路径 B —— 自写 `LongTermMemory`,从 Reactor Context 取 userId

要吃框架自带编排(尤其 `AGENTIC` 模式自动注册记忆工具)时用。SPI 方法不带 userId,需从
Reactor Context 取:
```java
public Mono<String> retrieve(Msg msg) {
    return Mono.deferContextual(rc -> {
        String tenant = rc.get("userId");   // 需先验证 key 名与可见性
        return httpSearch(tenant, msg.getTextContent());
    });
}
```
**前置验证(阻塞性)**:框架调用 `record`/`retrieve` 时,Reactor Context 是否确实带 `userId`
且 key 名正确——SPI 不保证,须先用 `StepVerifier` 探针确认;否则退回路径 A。且该 SPI 已废弃。

### 路径 C(不推荐)—— MCP 工具

把 ReMe MCP server 注册为 toolkit。问题:共享 MCP client 难以按请求注入租户头,与路径 B 同源
的"共享客户端 + per-call 身份"矛盾,还多一层开销。仅当需要 agent 自主搜记忆时考虑。

---

## 6. 错误与降级

- `retrieve` 失败:`onErrorReturn(prompt)`,不阻断对话(记忆是增强,非必需)。
- `session_append` 失败:记录日志、丢弃该回合增量(下回合仍发后续增量;或适配器缓冲重试)。
- `auto_memory`/`auto_dream` 失败:属离线路径,重试/告警,不影响在线对话。
- 401/400:401 = 租户解析失败(检查头/凭据);400 = body 误带租户字段(适配器 bug,修正)。

---

## 7. 待验证 / 待与 ReMe 侧对齐

- **V1** 路径 B 的 Reactor Context 是否暴露 `userId`(key 名、可见性)——决定 A/B 选型。
- **V2** ReMe 是否新增 `session_append`(独立 job vs `auto_memory` capture-only 模式)——主提案 Q5。
- **V3** 巩固触发默认 ReMe 侧调度还是宿主显式——主提案 Q4;适配器据此决定是否实现会话结束钩子。
- **V4** `search` 是否支持"偏 digest 长期层"的检索范围/权重参数。
- **V5** 会话预热接口:是否需要 ReMe 提供轻量"预热租户"入口,供宿主在会话开始时先行调用,
  把 LRU 冷加载移出 `retrieve` 热路径(主提案 Q7 / T14)。
- **V6** 对上游的价值:`agentscope-extensions-reme` 目前仅覆盖老版 API,**v4 需要新的官方
  Java 适配器**——本契约可作为推动上游的具体切入点。
