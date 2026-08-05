# 方案调研与取舍

调研日期：2026-08-05。目标是寻找“邮箱收票 + 中国发票查验/双源复核 + 打印排版 + OIDC + Docker”的可复用开源基线。

## 结论

没有发现同时覆盖全部目标且许可清晰、仍提供完整源码的项目。最接近的开源基线是 `ke4king/invoice_system`，但它使用百度 OCR 和本地 JWT，不含金蝶优先查验、OIDC 和 OCR/LLM 证据对照。本项目因此采用原创实现，并只借鉴公开产品思路。

## 项目对比

| 项目 | 可参考能力 | 主要缺口 / 许可判断 |
| --- | --- | --- |
| [EthanYoQ/Invoice-Downloader](https://github.com/EthanYoQ/Invoice-Downloader) | IMAP 筛选、附件/链接恢复、归档漏斗 | 桌面应用；Apache-2.0。只参考流程，没有复制代码 |
| [ke4king/invoice_system](https://github.com/ke4king/invoice_system) | FastAPI + Vue、邮箱、百度 OCR、1/2/4 联、Docker | MIT，功能最接近；缺金蝶、OIDC、双源证据语义 |
| [stone16/Invoice-Manager](https://github.com/stone16/Invoice-Manager) | OCR/LLM 并排冲突复核的交互 | README 声称 MIT，但调研时仓库根目录没有许可证文件，不复用源码 |
| [EnjoyWT/invoice-pdf-printer](https://github.com/EnjoyWT/invoice-pdf-printer) | 前端打印和 1/2 联排版思路 | 完整 Web 源码已迁至私有子模块；公开仓库不能作为源码基线 |
| [ikunalpha/fapiaodashi](https://github.com/ikunalpha/fapiaodashi) | 合并打印、附件后置、Excel 的产品描述 | 仓库仅 README/截图且无许可证，不复用 |
| [Paperless-ngx](https://docs.paperless-ngx.com/) | 成熟的邮件消费、OCR、全文检索和长期档案 | 通用 DMS，缺中国发票查验和专用拼版；部署明显更重 |
| [TaxHacker](https://github.com/vas3k/TaxHacker) | 自托管 LLM 票据提取、OpenAI/本地模型兼容 | MIT；偏通用记账，缺邮箱收票、官方查验和中国发票打印 |
| [Docspell](https://docspell.org/) | 邮件导入、OCR、文档组织 | 通用文档系统，AGPL；不提供专用发票查验链路 |
| [发票盒子](https://fapiaohezi.com/) | 商业产品对邮箱/微信归集、查重、报销集、自由排版的完整体验 | 非开源、云端产品；作为市场需求验证，不作为实现来源 |

## 金蝶接口判断

金蝶发票云旗舰版文档明确提供：

- 申请沙箱所需的环境地址、App ID、App Secret、Account ID 等信息：[快速开始](https://open-ultimate.piaozone.com/doc-3655357)。
- app token 与 access token 两阶段授权：[1.01](https://open-ultimate.piaozone.com/api-145421044)、[1.02](https://open-ultimate.piaozone.com/api-145421045)。
- 上传 Base64 文件并识别查验，覆盖常见增值税和数电发票：[2.02 发票识别查验](https://open-ultimate.piaozone.com/api-145421073)。
- 识别错误时可按票号、日期、金额/校验码再次查验：[2.01 发票查验](https://open-ultimate.piaozone.com/api-145421072)。

当前版本实现 2.02 主路径。2.01 的字段纠错后二次查验列入路线图，因为它需要在人工复核界面明确区分“编辑字段”和“重新请求官方查验”。

## 关键取舍

- 选择 FastAPI 服务端渲染 + 原生 JS，而非独立 SPA：部署只有一个容器，降低公开项目维护和供应链复杂度。
- 选择 SQLite WAL，而非默认 MySQL/PostgreSQL：符合单实例、小团队目标；明确不支持横向扩容。
- 选择 Tesseract + PDF/XML/OFD 文本层，而非默认 PaddleOCR：镜像更小，CPU 环境更容易部署。适配器边界允许以后增加 PaddleOCR/云 OCR。
- 选择 `pypdf` + `pypdfium2`，避免引入会改变项目许可证义务的 PDF 组件。
- 选择“金蝶已查验 / 双源一致 / 人工已复核”三种不同状态，不使用单一“验证成功”。

