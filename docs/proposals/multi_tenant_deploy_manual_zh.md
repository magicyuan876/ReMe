# 多租户 ReMe 内部单机部署手册

面向:在公司内网单台服务器上,用 Docker 部署多租户 ReMe 记忆服务,供内部 agent 应用
(如 agentscope-java 后端)对接。

---

## 0. 先读:关于"多 worker"和性能(重要)

ReMe 多租户是**单进程有状态**架构:

- 每个活跃租户的索引(chunk / BM25 / 向量 / wikilink 图)**常驻在该进程的内存**里;
- 写文件的并发锁**只在进程内生效**(跨进程不保护)。

因此 **不要用 `uvicorn --workers N` 那种单进程多 worker**:多个 worker 会各自加载同一租户
的内存索引副本,并可能同时写同一份 workspace 文件 → **数据损坏 + 缓存翻倍**。而且 ReMe 以
应用实例方式启动 uvicorn,本就不支持 fork 多 worker。

**正确的性能扩展方式**取决于瓶颈:

- **绝大多数情况:单实例就够。** 主要耗时是 LLM/embedding 的网络调用(I/O),异步单进程能
  轻松扛住高并发;CPU 只在 BM25/分块/向量计算时短暂占用。内部几十~几百用户,单实例足够。
- **确实 CPU 打满 / 要故障隔离时:分片多实例**——起 N 个容器,前面加一层**按租户粘性路由**
  (`hash(X-Reme-Tenant)`),保证**每个租户只被一个实例服务**。见 §7。

一句话:要多核就"分片",不是"多 worker"。先按单实例部署,不够再分片。

---

## 1. 前置条件

- 一台 Linux/Windows 服务器,装好 **Docker + Docker Compose**。
- 一个 **OpenAI 兼容的 LLM 端点**(用于 `auto_memory`/`auto_dream`),内网可达。
- (可选,推荐)一个 **OpenAI 兼容的多语 embedding 端点**(用于向量检索,跨语言/语义召回)。
- 本仓库代码(`proposal/multi-tenant-workspaces` 分支)。

---

## 2. 快速部署(三步)

```bash
# 1) 准备环境变量
cp example.env .env
#    编辑 .env,至少填 LLM_API_KEY / LLM_BASE_URL(见 §4)

# 2) 构建并启动
docker compose up -d --build

# 3) 确认健康
docker compose ps          # STATUS 应为 Up ... (healthy)
```

服务监听 `2333`。所有业务请求必须带 `X-Reme-Tenant: <用户id>` 头(见 §6)。

---

## 3. 记忆数据持久化(挂载到宿主机)

**是的,记忆文件必须挂到宿主机,否则删容器就丢。** `docker-compose.yml` 已配置:

```yaml
volumes:
  - ./tenant-data:/data      # 宿主 ./tenant-data  ←→  容器 /data
```

- 每个租户是 `./tenant-data/tenants/<租户id>/` 下的一个**普通目录**(daily / digest /
  session / metadata 等),可直接用编辑器打开查看。
- **备份 = 打包这个目录**:`tar czf reme-backup-$(date +%F).tgz tenant-data`(在维护窗口或
  服务停止时做最一致)。
- **迁移某个用户** = 拷贝其子目录到目标机的 `tenant-data/tenants/` 下即可。
- 想换存储位置:把 `./tenant-data` 改成你的数据盘路径,如
  `/data/reme/tenants:/data`(注意容器内固定是 `/data`,`REME_WORKSPACES_ROOT=/data/tenants`)。

> `.env` 和 `tenant-data/` 都在 `.gitignore` 里,不会误提交。

---

## 4. `.env` 配置详解

| 变量 | 说明 | 必填 |
|---|---|---|
| `LLM_API_KEY` | LLM 端点密钥 | 是(用记忆蒸馏时) |
| `LLM_BASE_URL` | LLM 端点,如 `http://your-gateway/v1` | 是 |
| `LLM_MODEL_NAME` | 高频任务(auto_memory)用的模型,建议用快模型 | 否(有默认) |
| `LLM_DREAM_MODEL_NAME` | 巩固任务(auto_dream)用的模型,建议用强模型 | 否(有默认) |
| `REME_NO_PROXY` | 需**绕过代理直连**的地址(见 §9 代理坑),如 `localhost,127.0.0.1,<你的模型内网IP>` | 视情况 |
| `REME_EMBEDDING_STORE` | 设 `default` 开启向量检索;留空 = 仅 BM25 | 否 |
| `EMBEDDING_API_KEY` | embedding 端点密钥 | 开向量时 |
| `EMBEDDING_BASE_URL` | embedding 端点,如 `http://your-gateway/v1` | 开向量时 |
| `EMBEDDING_MODEL_NAME` | 多语 embedding 模型名 | 开向量时 |
| `REME_WORKSPACES_ROOT` | 容器内租户根目录(默认 `/data/tenants`,一般不改) | 否 |

**双模型**:高频的 `auto_memory` 用便宜快模型,低频的 `auto_dream`(建长期 digest)用强模型,
分别由 `LLM_MODEL_NAME` / `LLM_DREAM_MODEL_NAME` 控制。

**向量检索(强烈建议开)**:开启后跨语言 + 语义召回("换个说法也能想起来"),需多语 embedding
模型 + 1024 维。开启后已有笔记会在下次加载时补算向量。不开则只有 BM25 关键词匹配,对中文和
换措辞的查询召回差。

---

## 5. 语言与检索行为(内部中文用户注意)

- 记忆蒸馏会**跟随对话原文语言**:中文对话 → 中文笔记(可读,BM25 也能用)。
- 开了向量后,即使个别笔记是英文,中文提问也能靠多语 embedding 跨语言召回。
- 建议:**开向量 + 保持默认的语言跟随**,这是中文场景召回质量最好的组合。

---

## 6. 安全(内网部署必读)

默认认证是 `trusted_header`:**谁能设置 `X-Reme-Tenant` 头,谁就是那个租户**。因此:

- **`2333` 端口只在内网开放**,由你已认证用户的后端/网关访问,后端把它认证过的用户 id
  (工号等)填进 `X-Reme-Tenant`。**不要把裸端口暴露给不可信客户端或公网**。
- 请求体里带 `tenant`/`tenant_id` 会被拒绝(400);解析不到租户返回 401。
- 运维端点 `version`/`health_check`/`help` 免认证,可用于探活。
- 若必须对不可信方直连,改用带鉴权的 resolver(如 token 映射),而非 `trusted_header`。

---

## 7. 需要更高吞吐:分片多实例(可选)

当单实例 CPU 打满,或想要故障隔离时,用分片。**核心规则:每个租户在同一时刻只能被一个实例
服务**——靠"按 `X-Reme-Tenant` 一致性哈希"的路由保证。

### 7.1 起 N 个实例(共享同一份 tenant-data)

因为路由保证同一租户永远命中同一实例,N 个实例可安全共用同一份 `./tenant-data`。示例
`docker-compose.yml` 起 4 个,分别映射到 2333~2336:

```yaml
services:
  reme0: { build: ., image: reme-multitenant:latest, env_file: [.env], ports: ["2333:2333"], volumes: ["./tenant-data:/data"], restart: unless-stopped }
  reme1: { image: reme-multitenant:latest, env_file: [.env], ports: ["2334:2333"], volumes: ["./tenant-data:/data"], restart: unless-stopped }
  reme2: { image: reme-multitenant:latest, env_file: [.env], ports: ["2335:2333"], volumes: ["./tenant-data:/data"], restart: unless-stopped }
  reme3: { image: reme-multitenant:latest, env_file: [.env], ports: ["2336:2333"], volumes: ["./tenant-data:/data"], restart: unless-stopped }
```

### 7.2 前置 nginx,按租户粘性路由

```nginx
upstream reme_shards {
    hash $http_x_reme_tenant consistent;   # 按租户头一致性哈希 → 同租户永远同一实例
    server 127.0.0.1:2333;
    server 127.0.0.1:2334;
    server 127.0.0.1:2335;
    server 127.0.0.1:2336;
}
server {
    listen 2333;                            # 对外统一入口
    location / {
        proxy_pass http://reme_shards;
        proxy_set_header X-Reme-Tenant $http_x_reme_tenant;
        proxy_read_timeout 300s;            # auto_dream 等 LLM 长任务
    }
}
```

后端仍然只连这一个入口 `:2333`,无感知分片。

### 7.3 分片注意

- **分片数尽量稳定**:运行中增减实例会让一致性哈希对约 1/N 的租户重新映射;若旧实例仍持有
  该租户、请求又被路由到新实例,可能出现短暂的跨进程并发写。**增减分片请在维护窗口做**。
- 每个实例有自己的 `max_active_tenants`(LRU 上限),按"单机内存 ÷ 分片数 ÷ 每租户内存"估算。
- 分片是用满多核的手段;单机核心不多、负载不高时不必分片。

---

## 8. 运维

```bash
docker compose logs -f reme            # 看日志
docker compose ps                      # 健康状态
docker compose restart reme            # 重启(数据在卷上,不丢)
docker compose down                    # 停止删容器(tenant-data 保留)
docker compose up -d --build           # 更新代码后重建

# 覆盖运行参数(示例):调大活跃租户上限 / 缩短巩固间隔
#   在 docker-compose.yml 的 command 追加:
#   multi_tenant.max_active_tenants=500 multi_tenant.consolidation_interval_seconds=1800
```

- **巩固调度**:`auto_dream`(daily→长期 digest)由 ReMe 内部按租户调度器自动跑,后端**不用**
  主动调用。
- **升级**:`git pull` 后 `docker compose up -d --build`;数据在宿主卷,升级不丢。
- **备份**:定期 `tar` 打包 `tenant-data`;重要变更前先备份。

---

## 9. 验证与排障

### 9.1 冒烟验证

```bash
# 写入(租户 emp001)
curl -s localhost:2333/write -H "Content-Type: application/json" -H "X-Reme-Tenant: emp001" \
  -d '{"path":"digest/wiki/food.md","name":"food","description":"饮食偏好","content":"## 偏好\n- 喜欢吃螺蛳粉"}'
# 检索
curl -s localhost:2333/search -H "Content-Type: application/json" -H "X-Reme-Tenant: emp001" \
  -d '{"query":"我喜欢吃什么","limit":5}'
# 隔离:换 emp002 查同样内容应搜不到
```

### 9.2 常见问题

- **容器访问内网模型返回防火墙/代理拦截页(如 fw-notify)**:Docker Desktop/企业环境常把
  代理注入容器,导致访问内网模型也走代理被拦。解决:在 `.env` 设
  `REME_NO_PROXY=localhost,127.0.0.1,<模型内网IP或网段>`,让容器对该地址直连。
- **中文查询召回差**:确认已开向量(`REME_EMBEDDING_STORE=default` + embedding 端点),
  BM25 单独对中文/换措辞召回本就弱。
- **401**:请求缺 `X-Reme-Tenant` 头。**400**:请求体里误带了 `tenant`/`tenant_id`。
- **auto_memory 报校验错误(缺 name)**:传给它的每条 message 需含 `name` 字段(可等于 role)。

---

## 10. 一页速查

| 目的 | 命令/配置 |
|---|---|
| 启动 | `docker compose up -d --build` |
| 数据位置 | 宿主 `./tenant-data/tenants/<租户>/` |
| 开向量 | `.env` 设 `REME_EMBEDDING_STORE=default` + EMBEDDING_* |
| 调用 | `POST :2333/{search,write,auto_memory,...}` + 头 `X-Reme-Tenant` |
| 多核扩展 | 分片多实例 + nginx `hash $http_x_reme_tenant consistent`(§7),**不是**多 worker |
| 备份 | `tar czf backup.tgz tenant-data` |
