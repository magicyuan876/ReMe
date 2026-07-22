# 适配契约:agentscope-java ↔ v4 多租户 ReMe(长期记忆)

- 状态:内部评审稿(未发布)
- 关联:[原生多租户 Workspace 提案](multi_tenant_workspaces_zh.md) · [agentscope-ai/ReMe #368](https://github.com/agentscope-ai/ReMe/issues/368)
- 目标:让 agentscope-java(2.x)把改造后的多租户 ReMe(v4)作为长期记忆后端,支持约 5000 用户。
- 范围:定义两侧的操作映射、身份/会话对齐、增量与巩固语义、错误与降级。不含模型凭据/计费(运营方职责)。

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
| `POST /auto_memory` | 落盘会话 + LLM 蒸馏为 daily 卡片(内部已分捕获/蒸馏两段) |
| `POST /auto_dream` | 巩固 daily → digest(长期层);常规由 ReMe 侧调度器触发 |
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

### 3.2 `record(List<Msg>)` → `POST /auto_memory`(沿用现有 job)

直接映射到现有 `auto_memory`,**不新增 job**。`auto_memory` 内部本就分两段:先廉价落盘会话
(`_save_session_messages`),再由 `agent_wrapper.reply` 蒸馏为 daily 卡片,并按 (day, session_id)
合并到同一张笔记——这已是 ReMe 的成熟设计,适配器不另造捕获/蒸馏机制。

```
POST /auto_memory
X-Reme-Tenant: <userId>
{"session_id": "<sessionId>", "messages": [<本回合增量,{role,content}>]}
```

- 租户 = `ctx.getUserId()`;会话 = `ctx.getSessionId()`。
- **调用节奏是适配器的选择,不改 ReMe**:`auto_memory` 每次调用会跑一次蒸馏,故适配器应传
  **本回合增量**而非全量历史(见 §4);若要压低蒸馏频次,可选择在会话结束时调用而非每回合。
- 增量/去重语义沿用 `auto_memory` / `auto_memory_cc` 的现有行为,不在适配器侧另造。

### 3.3 巩固 `auto_dream`:由 ReMe 侧负责,消费方不触发

`LongTermMemory` 契约没有巩固步骤,多租户又禁用了 `dream_cron`。而 agentscope-java **只是这个
记忆服务的调用方,不承担巩固职责**。因此 daily→digest 的巩固由 **ReMe 服务自身的按租户调度器**
负责(主提案 M8b):自动挑"当天有新 daily"的租户离峰跑 `auto_dream`,适配器无需感知、也不
依赖适配器触发。否则 **digest 长期层永不形成**,retrieve 只能命中 daily。

`auto_dream` 作为既有 job 仍可被运维手动调用做回补,但不属于常规集成路径。

### 3.4 `proactive`(可选)

`POST /proactive` + 租户头 → 取兴趣话题,由调用方决定是否经 `onSystemPrompt` 注入。

---

## 4. 身份、会话与增量语义

| 概念 | agentscope-java | 多租户 ReMe |
|---|---|---|
| 租户 | `RuntimeContext.userId` | `X-Reme-Tenant` 头 → `tenant_id` |
| 会话 | `RuntimeContext.sessionId` | job 的 `session_id` 参数 |
| 身份来源 | 框架/调用方认证后设入 `RuntimeContext` | 服务边界 `trusted_header` resolver 解析头 |

- **身份必须由边界注入,禁止走 body**;适配器绝不把 `tenant_id` 放进 JSON 负载(ReMe 会 400)。
- **增量**:`auto_memory` 蒸馏传入的消息,适配器应传"自上次以来的新消息"。首选适配器侧记住每个
  sessionId 上次已发送的游标只发增量;去重语义沿用 `auto_memory` / `auto_memory_cc` 的现有行为,
  不在适配器侧另造。

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

    // 记忆写入:回合结束后调用现有 auto_memory,只传本回合增量
    @Override public Flux<AgentEvent> onAgent(Agent a, RuntimeContext ctx, AgentInput in,
            Function<AgentInput, Flux<AgentEvent>> next) {
        return next.apply(in).concatWith(Flux.defer(() ->
            http.post().uri("/auto_memory")
                .header("X-Reme-Tenant", ctx.getUserId())
                .bodyValue(Map.of("session_id", ctx.getSessionId(), "messages", deltaOf(ctx, in)))
                .retrieve().bodyToMono(Void.class).thenMany(Flux.empty())
                .onErrorResume(e -> { log.warn("auto_memory failed", e); return Flux.empty(); })));
    }
}
```
`auto_memory` 每次调用含一次蒸馏;若要压低频次,可改为在会话结束时调用而非每回合(调用方
选择,不改 ReMe)。巩固(`auto_dream`)由 ReMe 侧调度器负责,不在此适配器内触发。

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
- `auto_memory` 失败(写入路径):记录日志、丢弃该回合增量(下回合仍发后续增量;或适配器缓冲重试),
  不阻断在线对话。
- `auto_dream`(巩固,ReMe 侧调度):失败在 ReMe 侧重试/告警,与在线对话无关。
- 401/400:401 = 租户解析失败(检查头/凭据);400 = body 误带租户字段(适配器 bug,修正)。

---

## 7. 待验证 / 待与 ReMe 侧对齐

- **V1** 路径 B 的 Reactor Context 是否暴露 `userId`(key 名、可见性)——决定 A/B 选型。
- **V2** `auto_memory` 对"仅传本回合增量"的处理与去重现状——确认无需适配器额外补偿(主提案 M8a)。
- **V3**〔已定案〕巩固由 ReMe 侧调度器负责,消费方不触发(主提案 Q4);适配器不实现巩固触发。
- **V4** `search` 是否支持"偏 digest 长期层"的检索范围/权重参数。
- **V5** 会话预热接口:是否需要 ReMe 提供轻量"预热租户"入口,供调用方在会话开始时先行调用,
  把 LRU 冷加载移出 `retrieve` 热路径(主提案 Q7 / T14)。
- **V6** 对上游的价值:`agentscope-extensions-reme` 目前仅覆盖老版 API,**v4 需要新的官方
  Java 适配器**——本契约可作为推动上游的具体切入点。
