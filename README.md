# InvoiceDock · 票舱

一套面向中国电子发票的自托管网页版工作台：从 IMAP 邮箱或手动上传归集发票，优先通过税务发票云识别查验；不可用时使用本地 OCR/文件文本层与 OpenAI 兼容 LLM 做双源一致性检查；最后生成 A4 省纸打印 PDF 和 Excel 台账。

> 当前版本：`0.1.0`。项目可用于日常归集与复核，但不是会计档案系统，也不以 OCR/LLM 结果替代税务机关或授权服务商的真伪查验。

## 为什么做这个项目

电子发票通常散落在多个邮箱和供应商下载链接中。下载后还要人工命名、录入、查重、打印。通用文档管理系统擅长归档，却不了解中国发票字段和查验路径；开票软件又通常不处理收票。本项目把“收、验、审、印”放在一条可审计的网页流水线上。

## 已实现能力

- 多 IMAP 邮箱定时收取，支持 PDF/OFD/XML/图片附件、受限 ZIP 展开，以及公网直接下载型发票链接。
- 手动批量上传，限制格式、文件体积、压缩包展开大小，以 SHA-256 去除完全相同文件。
- 税务发票云旗舰版优先：自动获取 `app_token` / `access_token`，调用 `recognitionCheck`。
- 税务发票云标准版（Piaozone）：`client_id`/`client_secret` 签名授权，调用 `img/Check/info` 识别查验；配置完整时优先于旗舰版。
- 本地 PDF 文本层、XML/OFD 结构和 Tesseract OCR 提取；可连接任意 OpenAI Chat Completions 兼容 LLM。
- OCR 与 LLM 逐字段对比；关键字段冲突进入人工复核，不把“双源一致”标成“官方验真”。
- 票号/代码/日期/金额业务去重提醒，原文件、处理结果、冲突和人工修改均留痕。
- 发票列表、搜索筛选、详情预览、人工复核、Excel 台账导出。
- A4 每页 1/2/4 张打印排版；服务端生成新 PDF，不改写原件。
- 邮箱/密码账号与自助注册（可关闭），可选 OIDC 登录，登录与注册限流，角色控制，CSRF 防护，敏感配置加密存储，审计日志。
- 税务 / LLM 凭据支持环境变量覆盖：环境变量优先于数据库配置且不落库；普通用户可配置自己的邮箱与查验凭据，未自定义时回退到管理员统一配置。
- 单容器 Docker Compose 部署，SQLite WAL 数据库，健康检查和只读能力收敛。

## 快速开始

要求 Docker 24+ 和 Docker Compose v2。

```bash
git clone git@github.com:coobin/InvoiceDock.git
cd InvoiceDock
cp .env.example .env
```

编辑 `.env`，至少替换：

```dotenv
APP_SECRET=<运行 openssl rand -hex 32 生成>
ADMIN_PASSWORD=<高强度初始密码>
APP_BASE_URL=http://localhost:8765
```

启动：

```bash
docker compose up -d --build
docker compose ps
```

打开 [http://localhost:8765](http://localhost:8765)，使用 `.env` 中的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录。首次启动只在用户表为空时创建管理员。

## 配置查验路径

管理员登录后打开“查验集成”。

1. 税务发票云：填写环境地址、App ID、App Secret、Account ID、登录用户及可选组织/企业信息，保存后测试连接。
2. LLM：填写 OpenAI 兼容 Base URL、API Key、模型。可关闭“发送票面预览”只发送文本层/OCR 文本。
3. 都不配置时，系统仍可本地提取，但会把结果放入“待人工复核”。

税务沙箱需向服务商申请环境和凭据，详见[税务发票云快速开始](https://open-ultimate.piaozone.com/doc-3655357)。

## 配置收票邮箱

管理员打开“邮箱收票”，填写 IMAP 服务器、账号和授权码。QQ、163 等邮箱通常要求专用授权码而不是网页登录密码。

- QQ：`imap.qq.com:993`
- 163：`imap.163.com:993`
- 企业邮箱：使用邮件服务商提供的 IMAP SSL 地址

扫描器只读打开邮箱，不删除邮件、不修改已读状态。它使用 UID 游标只处理新增邮件；如需从历史重新导入，可在数据库备份后重置对应邮箱的 `last_uid`。

当税务发票云已配置启用时，邮件入口只保留发票云查验通过（税务已查验）的附件和下载链接；无法通过发票云校验的文档（收据、回单、报价单、查验失败等）会被自动过滤，不进入台账和人工复核队列。未配置发票云的部署仍使用本地 OCR/LLM 提取后进入复核。

邮件导入判定为业务重复的发票会折叠为单条 PDF 记录：非 PDF 的重复附件直接丢弃；PDF 重复版本会替换掉之前保存的非 PDF 记录（旧的图片/OFD/XML 记录连同文件一并移除），同一张票只保留一份 PDF。

## OIDC

OIDC 在 `.env` 中配置并重启服务：

```dotenv
APP_BASE_URL=https://invoice.example.com
SESSION_HTTPS_ONLY=true
OIDC_ENABLED=true
OIDC_ISSUER=https://id.example.com/realms/company
OIDC_CLIENT_ID=invoicedock
OIDC_CLIENT_SECRET=replace-me
OIDC_SCOPES=openid email profile
OIDC_ALLOWED_DOMAINS=example.com
OIDC_ADMIN_GROUP=invoicedock-admins
OIDC_GROUP_CLAIM=groups
```

在身份提供商登记回调地址：`https://invoice.example.com/auth/oidc/callback`。客户端参数保持由环境变量提供（避免密钥入库）；管理员可在“查验集成”页用开关随时启停 OIDC 登录入口与自动跳转，无需重启。本地账号登录入口始终保留，便于身份服务故障时恢复。

## 注册账号

默认开启邮箱 + 密码自助注册（新用户为普通成员）。关闭后仅保留管理员分配账号与 OIDC：

```dotenv
REGISTRATION_ENABLED=false
REGISTRATION_MIN_PASSWORD_LENGTH=8
```

注册与登录的安全基线：邮箱全局唯一（大小写不敏感）、密码强度策略（最短长度 + 字母 + 数字）、Argon2 哈希存储、按 IP 的登录/注册限流、全表单 CSRF 校验、注册与登录写入审计日志。注册后的邮箱作为登录标识，暂不要求邮件验证（系统默认不发送外发邮件）；如需邮箱验证，可后续接入 SMTP 后补充验证码流程。

## Bark 管理员通知

管理员可在“查验集成”页粘贴 Bark App 提供的完整推送地址，并分别选择注册、登录和使用通知。推送地址中的设备密钥会加密保存；通知仅包含脱敏账号与操作摘要，推送失败不会中断注册、登录或发票处理。

## 数据目录与备份

持久数据全部位于主机的 `./data`：

```text
data/
├── invoicedock.db       # 业务数据、审计日志、加密配置
├── uploads/          # 原始发票
├── previews/         # 可重建缩略图
└── exports/          # 预留导出目录
```

备份必须同时包含 `data/` 和 `.env` 中的 `APP_SECRET`。若丢失或更换 `APP_SECRET`，已保存的邮箱授权码和 API Key 将无法解密。

## 文档

- [架构与数据流](docs/ARCHITECTURE.md)
- [部署与运维](docs/DEPLOYMENT.md)
- [方案调研与取舍](docs/RESEARCH.md)
- [安全模型](SECURITY.md)
- [路线图](docs/ROADMAP.md)
- [贡献指南](CONTRIBUTING.md)

## 开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

测试和静态检查：

```bash
pytest
ruff check app tests
```

## 开源与致谢

项目采用 [Apache License 2.0](LICENSE)。当前代码为原创实现，没有复制无许可证或闭源子模块的源代码。设计与功能调研参考项目列于 [NOTICE](NOTICE) 和 [docs/RESEARCH.md](docs/RESEARCH.md)。第三方依赖保留各自许可证。

## 当前边界

- 邮件链接恢复只处理无需登录、无需验证码且可直接返回支持文件类型的公网 URL；复杂供应商网页自动化在路线图中。
- OFD 可归集并读取其中 XML 文本，但当前浏览器预览和 A4 拼版仅支持 PDF/JPG/PNG。
- SQLite 部署目标是个人、小团队和单实例。多副本/高并发部署需要迁移到 PostgreSQL 和独立任务队列。
- 中国发票版式和服务商接口持续变化；上线财务流程前请用组织自己的样本与沙箱验收。
