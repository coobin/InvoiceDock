from __future__ import annotations

import logging
import secrets
import smtplib
from datetime import timedelta
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.models import EmailVerification, utcnow

logger = logging.getLogger(__name__)


def generate_6digit_code() -> str:
    """生成 6 位随机数字验证码。"""
    return f"{secrets.randbelow(900000) + 100000}"


def send_smtp_email(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str = "",
) -> None:
    """通过配置的 SMTP 服务器发送邮件（支持 QQ 邮箱、163 邮箱、通用 SMTP）。"""
    settings = config.get_settings()
    if not settings.smtp_configured:
        raise RuntimeError("SMTP 邮件服务尚未配置，无法发送邮件")

    msg = MIMEMultipart("alternative")
    sender_name = Header(settings.smtp_from_name, "utf-8").encode()
    sender_email = settings.smtp_from_email or settings.smtp_user
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = to_email
    msg["Subject"] = Header(subject, "utf-8")

    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    host = settings.smtp_host.strip()
    port = int(settings.smtp_port)
    timeout = 15.0

    # 465 默认使用 SSL；587 / 25 走 STARTTLS
    if settings.smtp_use_ssl or port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=timeout) as server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(sender_email, [to_email], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except smtplib.SMTPException:
                pass  # 如果服务器不支持 starttls 则继续尝试明文传输
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(sender_email, [to_email], msg.as_string())


def send_registration_code(
    db: Session,
    email: str,
    ip: str = "",
) -> tuple[bool, str]:
    """检查频控、生成并发送邮箱验证码。

    返回值：(成功与否, 提示信息)。
    """
    settings = config.get_settings()
    if not settings.smtp_configured:
        return False, "系统尚未配置邮件发送服务（SMTP），无法发送验证码"

    cleaned_email = email.strip().lower()
    now = utcnow()

    # 1. 频控：同邮箱 60 秒内只能发送 1 次
    recent_email = db.scalar(
        select(EmailVerification)
        .where(
            EmailVerification.email == cleaned_email,
            EmailVerification.created_at > (now - timedelta(seconds=60)),
        )
        .order_by(EmailVerification.created_at.desc())
    )
    if recent_email:
        return False, "验证码发送过于频繁，请 60 秒后再试"

    # 2. 频控：同 IP 1 小时内最多发送 15 次
    if ip:
        ip_count = db.scalar(
            select(func.count())
            .select_from(EmailVerification)
            .where(
                EmailVerification.ip == ip,
                EmailVerification.created_at > (now - timedelta(hours=1)),
            )
        ) or 0
        if ip_count >= 15:
            return False, "当前 IP 发送验证码过于频繁，请 1 小时后再试"

    code = generate_6digit_code()
    expires_at = now + timedelta(minutes=10)

    # 3. 构造邮件模板
    app_name = settings.app_name or "InvoiceDock"
    subject = f"【{app_name}】您的注册验证码：{code}"
    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f4f6f8; margin: 0; padding: 30px; }}
    .card {{ max-width: 520px; margin: 0 auto; background: #ffffff; border-radius: 8px; border: 1px solid #e1e8ed; padding: 32px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }}
    .logo {{ font-size: 20px; font-weight: 700; color: #173b56; margin-bottom: 24px; }}
    .title {{ font-size: 16px; color: #2c3e50; margin-bottom: 16px; }}
    .code-box {{ background: #f0f7fa; border: 1px dashed #78c1e0; border-radius: 6px; padding: 18px; text-align: center; margin: 24px 0; }}
    .code {{ font-family: monospace; font-size: 32px; font-weight: 700; letter-spacing: 8px; color: #0d6efd; }}
    .tip {{ font-size: 13px; color: #6c757d; line-height: 1.6; margin-top: 20px; }}
    .footer {{ font-size: 12px; color: #adb5bd; margin-top: 30px; border-top: 1px solid #edf2f7; padding-top: 16px; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">InvoiceDock · 票舱</div>
    <div class="title">您正在申请注册账号，本次验证码为：</div>
    <div class="code-box">
      <span class="code">{code}</span>
    </div>
    <div class="tip">
      <p>• 验证码 <strong>10 分钟</strong> 内有效，请勿将验证码泄露给他人。</p>
      <p>• 如非您本人操作，请忽略此邮件。</p>
    </div>
    <div class="footer">
      本邮件由系统自动发送，请勿直接回复。
    </div>
  </div>
</body>
</html>
"""
    text_body = f"【{app_name}】您正在注册账号，验证码为：{code}，10分钟内有效。如非本人操作请忽略。"

    try:
        send_smtp_email(cleaned_email, subject, html_body, text_body)
    except Exception as exc:
        logger.exception("Failed to send verification email to %s: %s", cleaned_email, exc)
        return False, f"邮件发送失败，请检查邮箱地址或稍后重试（{type(exc).__name__}）"

    record = EmailVerification(
        email=cleaned_email,
        code=code,
        ip=ip,
        used=False,
        created_at=now,
        expires_at=expires_at,
    )
    db.add(record)
    db.commit()

    return True, "验证码已发送至您的邮箱，10 分钟内有效"


def verify_registration_code(
    db: Session,
    email: str,
    code: str,
) -> tuple[bool, str]:
    """校验邮箱验证码。

    成功返回 (True, "ok")，失败返回 (False, 错误提示)。
    """
    settings = config.get_settings()
    if not settings.require_email_verification:
        return True, "not_required"

    cleaned_email = email.strip().lower()
    cleaned_code = code.strip()

    if not cleaned_code:
        return False, "请输入邮箱验证码"

    now = utcnow()
    record = db.scalar(
        select(EmailVerification)
        .where(
            EmailVerification.email == cleaned_email,
            EmailVerification.code == cleaned_code,
            EmailVerification.used.is_(False),
            EmailVerification.expires_at >= now,
        )
        .order_by(EmailVerification.created_at.desc())
    )

    if not record:
        return False, "验证码错误或已过期，请重新获取"

    record.used = True
    db.commit()
    return True, "ok"
