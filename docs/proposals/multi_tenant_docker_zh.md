# 多租户 ReMe 的 Docker 部署

- 关联:[原生多租户 Workspace 提案](multi_tenant_workspaces_zh.md)
- 产物:仓库根目录的 `Dockerfile`、`docker-compose.yml`、`.dockerignore`

## 快速开始

```bash
cp example.env .env         # 填入 LLM_API_KEY / LLM_BASE_URL(auto_memory/auto_dream 需要)
docker compose up -d --build
```

服务监听 `2333`,租户记忆持久化在宿主目录 `./tenant-data`(每个租户一个子目录)。调用必须带
`X-Reme-Tenant` 头:

```bash
curl -s localhost:2333/write -H 'Content-Type: application/json' -H 'X-Reme-Tenant: alice' \
  -d '{"path":"digest/wiki/coffee.md","name":"Coffee","description":"pref","content":"# Coffee\nAlice loves espresso."}'

curl -s localhost:2333/search -H 'Content-Type: application/json' -H 'X-Reme-Tenant: alice' \
  -d '{"query":"espresso","limit":5}'
```

`version` / `health_check` / `help` 是运维端点,豁免认证(无需租户头)。

## 向量检索(embedding,可选)

默认检索是 **BM25 + wikilink**,不调用任何 embedding 服务——这是 ReMe 的默认哲学(开箱不强制
依赖向量模型),也是为什么裸 BM25 部署 `.env` 里那两个 `EMBEDDING_*` 用不上。要开启向量化:

1. 在 `.env` 里设 `REME_EMBEDDING_STORE=default`,并填好 `EMBEDDING_API_KEY`(及需要时的
   `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL_NAME`);
2. `docker compose up -d`(无需改配置文件或重建镜像——开关是环境变量)。

开启后:

- **embedding 客户端(`as_embedding`)是全局共享**的(无状态推理客户端,一份即可);
- **每个租户有自己独立的向量索引(`embedding_store`)**,存在该租户 workspace 的 `metadata/`
  下,与其它租户完全隔离;
- 写入时对新 chunk 同步向量化;检索走 **向量 + BM25 的 RRF 融合**,再按 wikilink 扩展;
- embedding 服务不可达时会自动降级为 BM25(不阻断写入/检索),已验证。

关闭时(默认,`REME_EMBEDDING_STORE` 为空):`file_store` 不绑定 embedding_store,不产生任何
embedding 调用。

> 已验证:开关关闭且完全不配 `EMBEDDING_*` 时服务正常启动、写入、检索(纯 BM25);开关打开时
> `as_embedding` 全局共享、各租户 `embedding_store` 互相独立、`file_store` 绑定到本租户的向量库。

## 关键设计

- **数据即文件,必须挂卷**:租户 workspace 在容器内 `/data/tenants/<tenant>/`,compose 把
  `./tenant-data` 挂到 `/data`。删容器不丢数据;备份/迁移某租户 = 拷贝其目录。已验证:写入落到
  卷上,容器重启后冷加载仍可检索。
- **绑定所有网卡**:容器内以 `service.host=0.0.0.0` 启动(单机默认 `127.0.0.1` 在容器里外部不可达)。
- **精简镜像**:只装多租户模板实际用到的核心依赖;`codex` / `claude_code` 子进程后端在多租户
  模板中禁用,其 SDK(`openai-codex`、`claude-agent-sdk`)不打进镜像。
- **镜像内置配置**:`ENV REME_WORKSPACES_ROOT=/data/tenants`,启动命令
  `reme start config=multi_tenant service.host=0.0.0.0 service.port=2333`。

## 安全(重要)

默认认证是 `trusted_header`——**谁能设置 `X-Reme-Tenant` 头,谁就能冒充任意租户**。因此:

- 该端口必须置于内网,由你已认证的后端/网关访问(即 agentscope-java 这类调用方作为可信后端,
  把它认证过的 `userId` 写进 `X-Reme-Tenant`);**不要**把裸端口暴露给不可信客户端。
- 若确需对外直连,改用 `static_token_map`(或后续的 JWT/DB resolver):在 `.env` 里配 token→租户
  映射并覆盖 `multi_tenant.auth.backend`。
- 请求体里自带 `tenant`/`tenant_id` 会被拒绝(400);解析不到租户返回 401。

## 常用运维

```bash
docker compose logs -f reme                       # 看日志
docker compose exec reme reme status              # 需带租户;运维统计建议走宿主侧
docker compose up -d --build                       # 更新后重建
# 覆盖参数示例:自定义并发上限 / 巩固间隔
#   在 docker-compose.yml 的 command 追加:
#   multi_tenant.max_active_tenants=500 multi_tenant.consolidation_interval_seconds=1800
```

## 扩展到多进程分片(可选)

单进程即可承载数千注册租户(常驻内存只随活跃租户数增长)。当 CPU(GIL)到顶或需要故障隔离时,
起多个副本、在前面加一层按 `hash(tenant_id)` 的 sticky 路由,让每个租户目录只归一个副本;因租户
即目录,无需跨进程协调。这是可选的横向扩展,不是架构前提。
