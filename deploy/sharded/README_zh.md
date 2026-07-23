# 分片多实例部署

当单实例 CPU 打满、或需要故障隔离时用这套。nginx 按 `X-Reme-Tenant` 一致性哈希,把**每个
租户固定路由到同一个 shard**,因此所有 shard 可安全共享同一份 `tenant-data`。后端仍然只连一个
入口 `:2333`,分片对调用方透明。

## 前置

1. 从仓库根目录构建镜像一次:
   ```bash
   docker build -t reme-multitenant:latest .
   ```
2. 仓库根目录有 `.env`(和单实例部署共用同一个)。

## 启动 / 停止

```bash
# 启动(4 个 shard + 1 个 nginx 网关,对外 :2333)
docker compose -f deploy/sharded/docker-compose.yml up -d

# 状态 / 日志
docker compose -f deploy/sharded/docker-compose.yml ps
docker compose -f deploy/sharded/docker-compose.yml logs -f gateway

# 停止
docker compose -f deploy/sharded/docker-compose.yml down
```

> 分片部署**替代**单实例的根 `docker-compose.yml`(两者都占 `:2333`,只跑其一)。

## 可调项(环境变量,启动前 export 或写进 shell)

| 变量 | 默认 | 说明 |
|---|---|---|
| `REME_GATEWAY_PORT` | `2333` | 对外端口 |
| `REME_DATA_DIR` | `../../tenant-data` | 宿主记忆数据目录(所有 shard 共享) |

改分片数量:在 `docker-compose.yml` 增删 `remeN` 服务,并在 `nginx.conf` 的 `upstream` 里同步
增删 `server remeN:2333;`。

## 验证(已实测通过)

```bash
# 粘性:同一租户两次调用命中同一 shard(看响应头 X-Reme-Upstream)
curl -si localhost:2333/version -H 'Content-Type: application/json' -H 'X-Reme-Tenant: emp001' -d '{}' | grep -i x-reme-upstream
# 隔离:emp001 写,emp002 搜同内容应搜不到
```

实测结果:5 个租户各自稳定命中同一 shard 且分散在不同 shard;跨租户隔离经网关依然成立。

## 重要注意

- **每个租户在同一时刻只能被一个 shard 服务**——这由一致性哈希保证,是共享 `tenant-data` 安全
  的前提。
- **不要在运行中随意增减 shard 数量**:一致性哈希会对约 1/N 的租户重新映射,若旧 shard 仍持有
  该租户、请求又被路由到新 shard,可能出现短暂的跨进程并发写。增减分片请在**维护窗口**(先停
  服务或先让空闲租户被逐出)进行。
- 每个 shard 有独立的 `max_active_tenants`(LRU 上限):按 `单机内存 ÷ shard 数 ÷ 每租户内存`
  估算。
- 分片是用满多核的手段;单机核心不多、负载不高时不必分片,单实例更简单。

详见:`docs/proposals/multi_tenant_deploy_manual_zh.md`。
