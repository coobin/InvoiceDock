from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Request
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialCreationOptions,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialRequestOptions,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.config import get_settings
from app.models import User, UserPasskey


def get_rp_id(request: Request) -> str:
    """获取 Relying Party ID（域名或主机名，不带端口与协议）。"""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    if not host and request.headers.get("origin"):
        host = urlparse(request.headers["origin"]).netloc
    if not host and request.headers.get("referer"):
        host = urlparse(request.headers["referer"]).netloc
    host = host.split(":")[0].strip()
    if host:
        return host
    parsed = urlparse(get_settings().app_base_url)
    return (parsed.hostname or "localhost").strip()


def get_origins(request: Request) -> list[str]:
    """获取发起 WebAuthn 请求的合法 Origin 候选列表（包含 scheme 与可选 port）。"""
    origins: set[str] = set()
    if request.headers.get("origin"):
        origins.add(request.headers["origin"].rstrip("/"))
    if request.headers.get("referer"):
        parsed = urlparse(request.headers["referer"])
        origins.add(f"{parsed.scheme}://{parsed.netloc}".rstrip("/"))
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    raw_host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or f"{request.url.hostname}:{request.url.port}"
    )
    origins.add(f"{scheme}://{raw_host}".rstrip("/"))
    clean_host = raw_host.split(":")[0].strip()
    if clean_host:
        origins.add(f"https://{clean_host}")
    base = get_settings().app_base_url.rstrip("/")
    if base:
        origins.add(base)
    return [o for o in origins if o]


def generate_reg_options(
    user: User,
    rp_id: str,
    existing_passkeys: list[UserPasskey],
) -> PublicKeyCredentialCreationOptions:
    """生成通行密钥（Passkey）注册选项。

    resident_key 设为 PREFERRED，使现代操作系统（Mac Touch ID、iOS Face ID、Windows Hello）
    自动创建可发现凭据（Resident Key / Passkey），同时避免不支持强制 resident_key 的设备直接报错。
    """
    exclude_credentials = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(p.credential_id))
        for p in existing_passkeys
    ]

    return generate_registration_options(
        rp_id=rp_id,
        rp_name="InvoiceDock · 票舱",
        user_id=(user.id or user.username).encode("utf-8"),
        user_name=user.username,
        user_display_name=user.display_name or user.username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=exclude_credentials or None,
    )


def verify_reg_response(
    credential_data: dict,
    expected_challenge: str,
    rp_id: str,
    expected_origin: str | list[str],
):
    """验证客户端返回的 WebAuthn 注册凭据。"""
    return verify_registration_response(
        credential=credential_data,
        expected_challenge=base64url_to_bytes(expected_challenge),
        expected_rp_id=rp_id,
        expected_origin=expected_origin,
    )


def generate_auth_options(rp_id: str) -> PublicKeyCredentialRequestOptions:
    """生成免输用户名/免输密码的 Passkey 登录认证选项。

    不限制 allow_credentials（传 None），由客户端设备自动匹配在该 RP 下存储的所有通行密钥。
    """
    return generate_authentication_options(
        rp_id=rp_id,
        user_verification=UserVerificationRequirement.PREFERRED,
    )


def verify_auth_response(
    credential_data: dict,
    expected_challenge: str,
    passkey: UserPasskey,
    rp_id: str,
    expected_origin: str | list[str],
):
    """验证客户端返回的 WebAuthn 认证凭据签名与计数器。"""
    return verify_authentication_response(
        credential=credential_data,
        expected_challenge=base64url_to_bytes(expected_challenge),
        expected_rp_id=rp_id,
        expected_origin=expected_origin,
        credential_public_key=base64url_to_bytes(passkey.public_key),
        credential_current_sign_count=passkey.sign_count,
    )
