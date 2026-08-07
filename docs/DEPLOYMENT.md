# 部署与运维

## 单机 Compose

```bash
cp .env.example .env
openssl rand -hex 32
docker compose up -d --build
docker compose logs -f web
```

Compose 默认以主机 UID/GID `1000:1000` 运行容器。若部署账号不同，先在 `.env` 中设置 `PUID=$(id -u)` 和 `PGID=$(id -g)` 对应的数值，确保绑定挂载的 `data/` 可写且容器仍保持非 root。

健康检查：

```bash
curl -fsS http://127.0.0.1:8765/healthz
```

## 反向代理

推荐只在内网监听或置于 HTTPS 反向代理后。Nginx 示例：

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    client_max_body_size 25m;
}
```

使用 HTTPS 后设置：

```dotenv
APP_BASE_URL=https://invoice.example.com
SESSION_HTTPS_ONLY=true
```

## OIDC 上线清单

1. 在 IdP 创建 confidential web client。
2. 回调地址精确设置为 `${APP_BASE_URL}/auth/oidc/callback`。
3. 允许 Authorization Code Flow，关闭 Implicit Flow。
4. 至少返回 `sub`；建议返回 `email`、`name`、`preferred_username` 和组声明。
5. 使用 `OIDC_ALLOWED_DOMAINS` 限制邮箱域；用 `OIDC_ADMIN_GROUP` 映射管理员。
6. 重启后用普通成员和管理员各测试一次，再保留一个强密码本地管理员作应急入口。

## 备份

建议先暂停容器或使用 SQLite 在线备份，再复制数据：

```bash
docker compose stop web
tar -czf invoicedock-backup-$(date +%Y%m%d).tgz data .env
docker compose start web
```

备份包含发票和密钥，应加密并限制访问。恢复时保持原 `APP_SECRET`。

## 更新

```bash
git pull --ff-only
docker compose up -d --build
docker image prune
```

更新前先备份。当前版本在启动时使用 SQLAlchemy `create_all` 创建缺失表，不执行破坏性迁移；涉及字段变更的未来版本会附迁移说明。

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

## 容量规划

应用容器包含中英文 Tesseract，构建镜像通常数百 MB。数据容量主要由原始发票决定。以每张 500 KB 估算，10 万张约 50 GB，另预留数据库、缩略图和备份空间。
