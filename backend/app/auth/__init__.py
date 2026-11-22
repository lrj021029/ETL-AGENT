"""认证与邮件二次验证（Email OTP）模块导出。"""
from __future__ import annotations

from app.auth.service import (
    DEFAULT_PERMISSIONS_SPEC,
    ROLE_PERMISSION_MATRIX,
    SYSTEM_ROLE_CODES,
    SYSTEM_ROLES_SPEC,
    AuthService,
    OtpDeliveryRecord,
    hash_password,
    seed_demo_iam_data,
    verify_password,
)

__all__ = [
    "AuthService",
    "OtpDeliveryRecord",
    "SYSTEM_ROLES_SPEC",
    "SYSTEM_ROLE_CODES",
    "DEFAULT_PERMISSIONS_SPEC",
    "ROLE_PERMISSION_MATRIX",
    "hash_password",
    "verify_password",
    "seed_demo_iam_data",
]
