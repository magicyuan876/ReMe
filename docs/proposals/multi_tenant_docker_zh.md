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
