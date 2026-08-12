# 部署与运维

## 单机 Compose

```bash
cp .env.example .env
openssl rand -hex 32
docker compose up -d --build
docker compose logs -f web
```

Compose 默认以主机 UID/GID `1000:1000` 运行容器。若部署账号不同，先在 `.env` 中设置 `PUID=$(id -u)` 和 `PGID=$(id -g)` 对应的数值，确保绑定挂载的 `data/` 可写且容器仍保持非 root。

上线前至少修改以下值，并把 `.env` 权限设为仅部署账号可读：

```dotenv
APP_SECRET=<openssl rand -hex 32 的输出>
ADMIN_USERNAME=kay
ADMIN_PASSWORD=<独立的高强度初始密码>
APP_BASE_URL=https://invoice.example.com
SESSION_HTTPS_ONLY=true
```

```bash
chmod 600 .env
mkdir -p data
chmod 700 data
```

容器默认以非 root 用户运行，根文件系统只读，只有 `/data` 绑定目录和带容量限制的 `/tmp` 可写；同时启用 `no-new-privileges`、移除 Linux capabilities、PID/CPU/内存上限和 Docker 日志轮转。可在 `.env` 中按机器容量调整：

```dotenv
CONTAINER_PIDS_LIMIT=256
CONTAINER_CPU_LIMIT=2.0
CONTAINER_MEMORY_LIMIT=2g
CONTAINER_TMPFS_SIZE=256m
CONTAINER_LOG_MAX_SIZE=10m
CONTAINER_LOG_MAX_FILES=3
```

健康检查：

```bash
curl -fsS http://127.0.0.1:8765/healthz
```

## 反向代理

Compose 默认以 `APP_BIND_IP=127.0.0.1` 发布端口，适用于运行在宿主机上的 Nginx/Caddy。Nginx 示例：

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    client_max_body_size 25m;
}
```

若反向代理运行在另一个容器，有两种安全部署方式：

1. 首选：把代理和 InvoiceDock 接入同一个受控 Docker 网络，代理直接访问 `web:8000`，且不向其他网络发布 8000。
2. 兼容现有部署：设置 `APP_BIND_IP=0.0.0.0`，代理访问宿主机 `8765`，同时用主机防火墙只允许代理地址访问该端口。不能在无防火墙的公网主机上直接这样发布。

转发头只在请求的直接对端可信时使用。Uvicorn 与应用分别读取以下配置，均不得设置为 `*`：

```dotenv
# Uvicorn 接受 Forwarded/X-Forwarded-* 的直接代理对端
FORWARDED_ALLOW_IPS=127.0.0.1
# 应用解析客户端 IP 链时认可的直接代理/代理网段
TRUSTED_PROXY_IPS=127.0.0.1,::1,172.16.0.0/12
```

容器代理场景应在容器内查看请求的实际直接对端，并尽量把 `172.16.0.0/12` 收窄为共享网络的确切网关或代理容器地址。应用从右向左剥离可信代理；若直接对端不可信，会忽略客户端提供的 `X-Forwarded-For`，防止通过伪造头绕过登录/注册限流。

使用 HTTPS 后设置：

```dotenv
APP_BASE_URL=https://invoice.example.com
SESSION_HTTPS_ONLY=true
```

应用默认关闭 `/openapi.json` 与 `/api/docs`，并统一发送 CSP、禁止 MIME 嗅探和页面嵌入、严格 Referrer Policy 等安全头；登录、管理、文件和导出响应使用 `Cache-Control: no-store`。若确需临时查看 API 文档，只在受控网络中设置 `ENABLE_API_DOCS=true`，完成后关闭。

## OIDC 上线清单

1. 在 IdP 创建 confidential web client。
2. 回调地址精确设置为 `${APP_BASE_URL}/auth/oidc/callback`。
3. 允许 Authorization Code Flow，关闭 Implicit Flow。
4. 至少返回 `sub`；建议返回 `email`、`name`、`preferred_username` 和组声明。
5. 确认 IdP 返回布尔值（或字符串）`email_verified=true`；未验证邮箱不会被保存，也不能通过允许域检查。
6. 使用 `OIDC_ALLOWED_DOMAINS` 限制邮箱域；用 `OIDC_ADMIN_GROUP` 映射管理员。
7. OIDC 账号只按 `issuer + sub` 绑定，不会凭同邮箱自动绑定已有本地账号。同邮箱冲突时应先拒绝登录，再由管理员核验身份后显式处理。
8. OIDC 每次登录会同步管理员组角色，并清除该 OIDC 身份遗留的本地密码。重启后用普通成员和管理员各测试一次，再保留一个强密码本地管理员作应急入口。

## 外连与资源限额

用户可提供的 IMAP 和 LLM 地址会拒绝私网、回环、链路本地及保留地址。确需访问内部邮箱或自建模型时，由管理员精确放行；此配置不能替代防火墙和内部服务自身认证：

```dotenv
# 逗号分隔的域名、IP 或 CIDR；默认空
OUTBOUND_PRIVATE_HOST_ALLOWLIST=mail.corp.example,10.20.30.40,10.30.0.0/24
```

公开注册部署还应按主机容量设置应用级上限：

```dotenv
MAX_UPLOAD_MB=25
MAX_USER_STORAGE_MB=2048
MAX_USER_DAILY_UPLOAD_FILES=200
MAX_USER_DAILY_OCR=200
MAX_USER_DAILY_LLM=100
MAX_CONCURRENT_JOBS_PER_USER=2
MAX_CONCURRENT_PROCESSING_JOBS=4
MAX_ARCHIVE_FILES=30
MAX_ARCHIVE_UNCOMPRESSED_MB=80
MAX_OFD_FILES=200
MAX_OFD_UNCOMPRESSED_MB=100
```

应用级上限保护公平使用，容器级上限保护主机，两者都要保留。监控 `data/` 磁盘占用、容器 OOM/重启次数、任务延迟及被拒绝的请求；达到容量的 70% 前安排扩容或归档。

## 备份

备份必须同时包含 SQLite、原始发票、加密配置和原 `APP_SECRET`。最简单可靠的单机备份方式是短暂停止写入，使用严格默认权限生成一致快照：

```bash
umask 077
docker compose stop web
tar -czf "invoicedock-backup-$(date +%Y%m%d-%H%M%S).tgz" data .env
docker compose start web
```

不要让普通应用进程获得备份目录写权限；备份账号只需要读取项目 `data/` 和 `.env`、写入独立备份目标。压缩包包含发票和密钥，必须存入加密目标并限制访问。恢复时保持原 `APP_SECRET`。

每月至少做一次隔离恢复演练，而不是只检查压缩命令退出码。以下步骤不覆盖生产数据：

1. 在隔离主机或临时目录检验 `tar -tzf <backup>`，再以 `umask 077` 解压。
2. 对解压出的数据库执行 SQLite `PRAGMA quick_check` 和 `PRAGMA foreign_key_check`，必须分别返回 `ok` 和空结果。
3. 核对 `uploads/` 中被数据库引用的原文件均存在；抽样计算 SHA-256 并与数据库记录比较。
4. 使用恢复出的 `.env` 和 `data/` 启动独立 Compose 项目，绑定不同的本机端口，并在网络层阻断外连，避免扫描生产邮箱或调用第三方服务。
5. 登录、打开抽样发票、生成一次 Excel/PDF，再销毁演练环境并记录日期、备份编号和结果。

可用 Python 标准库对隔离副本做最小数据库校验：

```bash
python3 -c 'import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); print(db.execute("PRAGMA quick_check").fetchall()); print(db.execute("PRAGMA foreign_key_check").fetchall())' /path/to/restored/data/invoicedock.db
```

## 更新

```bash
git pull --ff-only
docker compose up -d --build
```

更新前先备份并查看变更说明；更新后检查健康状态、日志、数据库和抽样文件。当前版本在启动时使用 SQLAlchemy `create_all` 并执行兼容性迁移；任何索引或字段变更都应先在备份副本验证。镜像清理属于独立维护动作，确认没有回滚依赖后再执行。

## 日志与排障

```bash
docker compose ps
docker compose logs --tail=200 web
docker inspect --format '{{json .State.Health}}' invoicedock-web-1
```

- 邮箱问题：先在网页测试连接，确认授权码、IMAP 已启用、文件夹名和服务器时间。
- 税务问题：确认环境地址包含租户路径，凭据来自同一环境，服务器能访问该 HTTPS 地址。
- LLM 问题：Base URL 应包含兼容 API 的版本前缀（常见为 `/v1`），模型需支持 JSON；若不支持视觉输入，关闭“发送票面预览”。
- 文件识别问题：PDF 若已有文本层会优先提取；扫描 PDF 才调用 Tesseract。
- 代理后所有访问显示同一 IP：确认 `FORWARDED_ALLOW_IPS` 与 `TRUSTED_PROXY_IPS` 包含实际直接代理对端，而不是公网客户端；不要用 `*` 临时绕过。
- 容器只读错误：确认写入路径仅为 `/data` 或 `/tmp`，并检查 `PUID`/`PGID` 对 `data/` 的权限，不要关闭只读根文件系统作为长期修复。

## 容量规划

应用容器包含中英文 Tesseract，构建镜像通常数百 MB。数据容量主要由原始发票决定。以每张 500 KB 估算，10 万张约 50 GB，另预留数据库、缩略图和备份空间。
