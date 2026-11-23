"""JWT 认证、Refresh Token 家族轮转重用检测与邮件二次验证（Email OTP）服务。

严格遵循 PRD §3.4、DEC-009 与 contracts/openapi/openapi.yaml：
1. 密码登录成功后不直接签发正式 Access Token，而是发起 Email OTP 挑战（界面与文档明确标注为“邮件二次验证”，不称强 MFA）。
2. 本地开发默认通过 Mailpit SMTP (1025) 投递验证码，并保留内存态投递记录以便自动化测试校验。
3. 验证 Email OTP 通过后签发 JWT Access Token + Refresh Token，并建立 `RefreshTokenFamily`。
4. 每次 `/auth/refresh` 轮转 `current_token_hash`；若检测到已轮转旧 Token 被重放（Reuse Detection），
   立即吊销整个 `RefreshTokenFamily` 与关联 `UserSession`，抛出 `AUTH_REFRESH_REUSE_DETECTED` (401)。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import smtplib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.shared.errors import AppError, ErrorCode
from app.shared.models.iam import (
    EmailOtpChallenge,
    MemberRoleBinding,
    Organization,
    OrganizationMember,
    Permission,
    Project,
    ProjectMember,
    RefreshTokenFamily,
    Role,
    RolePermission,
    User,
    UserSession,
)

# 固定系统角色定义（is_system=True，严禁修改或删除）
SYSTEM_ROLES_SPEC: tuple[tuple[str, str, str], ...] = (
    ("MAKER", "数据工程师 (Maker)", "登记连接、元数据探查、需求澄清、生成候选、冻结版本与发起 Prepare"),
    ("DATA_CHECKER", "数据审批人 (Data Checker)", "审查字段映射、Schema、脱敏结构、质量规则与 DAG"),
    ("SECURITY_CHECKER", "安全审批人 (Security Checker)", "审查数据分级、资源范围、Secret 引用、预算、回滚计划与策略"),
    ("OPERATOR", "系统操作员 (Operator)", "在审批通过后执行 Commit、运行监控、受管暂停/取消与回滚"),
    ("AUDITOR", "审计人员 (Auditor)", "只读查看执行历史、证据账本、质量报告与 Benchmark"),
    ("ORG_ADMIN", "组织管理员 (Organization Admin)", "管理组织成员与项目创建（受职责分离硬约束限制）"),
    ("PROJECT_ADMIN", "项目管理员 (Project Admin)", "管理项目成员、自定义角色与项目策略（不可绕过系统 SoD）"),
)

SYSTEM_ROLE_CODES: frozenset[str] = frozenset(code for code, _, _ in SYSTEM_ROLES_SPEC)

# 预置细粒度权限清单
DEFAULT_PERMISSIONS_SPEC: tuple[tuple[str, str, str, str], ...] = (
    ("connection:read", "connection", "read", "查看数据源连接与能力矩阵"),
    ("connection:write", "connection", "write", "创建与更新数据源连接"),
    ("metadata:probe", "metadata", "probe", "发起受管只读元数据探查"),
    ("metadata:classify", "metadata", "classify", "标记敏感字段分级"),
    ("pipeline:write", "pipeline", "write", "编辑 Pipeline 草稿与冻结版本"),
    ("pipeline:prepare", "preparation", "prepare", "发起 Harness Prepare 准备单"),
    ("approval:data_check", "approval", "data_check", "行使数据审批槽位决策"),
    ("approval:security_check", "approval", "security_check", "行使安全审批槽位决策"),
    ("execution:commit", "execution", "commit", "行使 Operator Commit 触发受管执行"),
    ("execution:control", "execution", "control", "受管取消、对账、暂停或发起回滚"),
    ("schedule:manage", "schedule", "manage", "创建与启停受管定时调度"),
    ("evidence:read", "evidence", "read", "查询与校验审计证据账本"),
    ("benchmark:run", "benchmark", "run", "生成 Benchmark 数据集与运行回归"),
    ("project:manage_members", "project", "manage_members", "管理项目成员与自定义角色"),
)

# 系统角色默认权限绑定矩阵
ROLE_PERMISSION_MATRIX: dict[str, tuple[str, ...]] = {
    "MAKER": (
        "connection:read",
        "connection:write",
        "metadata:probe",
        "metadata:classify",
        "pipeline:write",
        "pipeline:prepare",
    ),
    "DATA_CHECKER": ("connection:read", "metadata:classify", "approval:data_check", "evidence:read"),
    "SECURITY_CHECKER": ("connection:read", "metadata:classify", "approval:security_check", "evidence:read"),
    "OPERATOR": ("connection:read", "execution:commit", "execution:control", "schedule:manage", "evidence:read"),
    "AUDITOR": ("connection:read", "evidence:read"),
    "ORG_ADMIN": ("connection:read", "project:manage_members", "evidence:read"),
    "PROJECT_ADMIN": (
        "connection:read",
        "connection:write",
        "metadata:probe",
        "metadata:classify",
        "project:manage_members",
        "benchmark:run",
        "evidence:read",
    ),
}


def hash_password(password: str, salt: str | None = None) -> str:
    """使用 PBKDF2-HMAC-SHA256 计算密码哈希。"""
    actual_salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), actual_salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${actual_salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """恒定时间比较校验密码哈希。"""
    parts = stored_hash.split("$")
    if len(parts) != 3 or parts[0] != "pbkdf2_sha256":
        return False
    expected = hash_password(password, salt=parts[1])
    return hmac.compare_digest(expected, stored_hash)


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class OtpDeliveryRecord:
    """邮件二次验证（Email OTP）本地发送记录（供 Mailpit 投递与单测断言使用）。"""

    challenge_id: uuid.UUID
    session_id: uuid.UUID
    email: str
    otp_code: str
    verification_label: str
    sent_at: datetime


class AuthService:
    """认证、邮件二次验证（Email OTP）与 Refresh Token Family 轮转服务。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # 内存中记录最近发送的 OTP（仅用于本地开发调试与自动化单测验证，不在生产日志或 API 暴露）
        self.sent_otp_outbox: list[OtpDeliveryRecord] = []

    def login_with_password(
        self,
        session: Session,
        *,
        email: str,
        password: str,
        ip_address: str = "127.0.0.1",
        user_agent: str = "etl-agent-client",
    ) -> dict[str, Any]:
        """第一步：校验邮箱与密码，创建未完成 OTP 验证的 Session 并发送邮件二次验证挑战。"""
        normalized_email = email.strip().lower()
        user = session.execute(select(User).where(User.email == normalized_email)).scalar_one_or_none()
        if user is None or user.status != "ACTIVE" or not verify_password(password, user.password_hash):
            raise AppError(
                ErrorCode.AUTH_INVALID_CREDENTIALS,
                "邮箱或密码错误，或者账号未处于激活状态",
            )

        now = datetime.now(timezone.utc)
        user_session = UserSession(
            user_id=user.id,
            organization_id=None,
            ip_hash=_sha256_hex(ip_address),
            user_agent=user_agent[:255],
            otp_verified=False,
            expires_at=now + timedelta(seconds=self.settings.refresh_token_ttl_seconds),
        )
        session.add(user_session)
        session.flush()

        # 生成 6 位数字邮件二次验证验证码
        otp_code = f"{secrets.randbelow(900000) + 100000:06d}"
        challenge = EmailOtpChallenge(
            user_id=user.id,
            session_id=user_session.id,
            email=user.email,
            code_hash=_sha256_hex(otp_code),
            attempts=0,
            max_attempts=self.settings.otp_max_attempts,
            expires_at=now + timedelta(seconds=self.settings.otp_ttl_seconds),
        )
        session.add(challenge)
        session.flush()

        self._deliver_email_otp(
            challenge_id=challenge.id,
            session_id=user_session.id,
            email=user.email,
            otp_code=otp_code,
        )

        local_part, _, domain_part = user.email.partition("@")
        masked_email = f"{local_part[:2]}***@{domain_part}" if domain_part else "***"
        return {
            "challenge_id": str(challenge.id),
            "session_id": str(user_session.id),
            "email": user.email,
            "masked_email": masked_email,
            "delivery_channel": "EMAIL_OTP",
            "verification_type": "EMAIL_OTP",
            "verification_label": "邮件二次验证",
            "expires_in_seconds": self.settings.otp_ttl_seconds,
            "notice": "已向邮箱发送邮件二次验证验证码（本地请在 Mailpit http://127.0.0.1:8025 查看 6 位验证码；此方式仅作演示二次验证，不等同于抗钓鱼强 MFA）",
            "message": "已向邮箱发送邮件二次验证验证码（本地通过 Mailpit 捕获）",
        }

    def _deliver_email_otp(
        self,
        *,
        challenge_id: uuid.UUID,
        session_id: uuid.UUID,
        email: str,
        otp_code: str,
    ) -> None:
        """向本地 Mailpit SMTP 发送邮件二次验证验证码，并记录到内存投递箱。"""
        record = OtpDeliveryRecord(
            challenge_id=challenge_id,
            session_id=session_id,
            email=email,
            otp_code=otp_code,
            verification_label="邮件二次验证",
            sent_at=datetime.now(timezone.utc),
        )
        self.sent_otp_outbox.append(record)

        if self.settings.smtp_enabled:
            msg = EmailMessage()
            msg["Subject"] = "[ETL-Agent] 邮件二次验证验证码 (Email OTP)"
            msg["From"] = self.settings.smtp_from_email
            msg["To"] = email
            msg.set_content(
                f"您的 ETL-Agent 邮件二次验证验证码为：{otp_code}\n"
                f"有效期 {self.settings.otp_ttl_seconds // 60} 分钟。"
                "注意：邮件二次验证仅用于本地演示二次验证，不等同于抗钓鱼强 MFA。"
            )
            try:
                with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=3) as smtp:
                    smtp.send_message(msg)
            except OSError:
                # 当本地单元测试未启动 Mailpit 容器时，保持 outbox 捕获可用而不阻塞登录测试
                pass

    def verify_email_otp(
        self,
        session: Session,
        *,
        challenge_id: uuid.UUID,
        otp_code: str,
    ) -> dict[str, Any]:
        """第二步：校验邮件二次验证（Email OTP）验证码，签发 JWT Access Token 与 Refresh Token 家族。"""
        challenge = session.get(EmailOtpChallenge, challenge_id)
        if challenge is None:
            raise AppError(
                ErrorCode.AUTH_OTP_INVALID_OR_EXPIRED,
                "邮件二次验证挑战不存在",
            )

        now = datetime.now(timezone.utc)
        if challenge.consumed_at is not None:
            raise AppError(
                ErrorCode.AUTH_OTP_INVALID_OR_EXPIRED,
                "该邮件二次验证挑战已被使用",
            )
        if _ensure_aware_utc(challenge.expires_at) <= now:
            raise AppError(
                ErrorCode.AUTH_OTP_INVALID_OR_EXPIRED,
                "邮件二次验证验证码已过期",
            )
        if challenge.attempts >= challenge.max_attempts:
            raise AppError(
                ErrorCode.AUTH_OTP_INVALID_OR_EXPIRED,
                "邮件二次验证尝试次数超限，请重新登录",
            )

        challenge.attempts += 1
        expected_hash = _sha256_hex(otp_code.strip())
        if not hmac.compare_digest(expected_hash, challenge.code_hash):
            session.commit()
            raise AppError(
                ErrorCode.AUTH_OTP_INVALID_OR_EXPIRED,
                "邮件二次验证验证码不正确",
                detail={"remaining_attempts": max(0, challenge.max_attempts - challenge.attempts)},
            )

        challenge.consumed_at = now
        user_session = session.get(UserSession, challenge.session_id)
        if user_session is None or user_session.revoked_at is not None:
            raise AppError(
                ErrorCode.AUTH_INVALID_CREDENTIALS,
                "关联会话已失效或被吊销",
            )
        user_session.otp_verified = True

        user = session.get(User, challenge.user_id)
        if user is None or user.status != "ACTIVE":
            raise AppError(
                ErrorCode.AUTH_INVALID_CREDENTIALS,
                "用户不存在或已被禁用",
            )

        family_id = uuid.uuid4()
        refresh_token = self._encode_refresh_token(
            user_id=user.id,
            session_id=user_session.id,
            family_id=family_id,
            rotation_count=0,
            now=now,
        )
        family = RefreshTokenFamily(
            id=family_id,
            user_id=user.id,
            session_id=user_session.id,
            current_token_hash=_sha256_hex(refresh_token),
            rotation_count=0,
            expires_at=now + timedelta(seconds=self.settings.refresh_token_ttl_seconds),
        )
        session.add(family)
        session.flush()

        access_token = self._encode_access_token(
            user=user,
            session_id=user_session.id,
            otp_verified=True,
            now=now,
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": self.settings.access_token_ttl_seconds,
            "verification_label": "邮件二次验证",
            "user": {
                "id": str(user.id),
                "email": user.email,
                "display_name": user.display_name,
            },
        }

    def rotate_refresh_token(
        self,
        session: Session,
        *,
        refresh_token: str,
    ) -> dict[str, Any]:
        """轮转 Refresh Token Family；若检测到旧 Token 重放（Reuse Detection），立即吊销整个家族与会话。"""
        payload = self.decode_jwt_token(refresh_token, expected_type="refresh")
        family_id = uuid.UUID(str(payload["family_id"]))
        family = session.get(RefreshTokenFamily, family_id)
        if family is None:
            raise AppError(
                ErrorCode.AUTH_INVALID_CREDENTIALS,
                "Refresh Token 家族不存在",
            )

        now = datetime.now(timezone.utc)
        user_session = session.get(UserSession, family.session_id)

        # 检查是否已被吊销
        if family.revoked_at is not None or (user_session is not None and user_session.revoked_at is not None):
            raise AppError(
                ErrorCode.AUTH_REFRESH_REUSE_DETECTED,
                "检测到已吊销的 Refresh Token 家族被访问",
                detail={"family_id": str(family.id)},
            )

        if _ensure_aware_utc(family.expires_at) <= now:
            raise AppError(
                ErrorCode.AUTH_TOKEN_EXPIRED,
                "Refresh Token 已过期",
            )

        presented_hash = _sha256_hex(refresh_token)
        # 核心安全不变量：Refresh Token 重用检测（Reuse Detection）
        if not hmac.compare_digest(presented_hash, family.current_token_hash):
            family.revoked_at = now
            if user_session is not None:
                user_session.revoked_at = now
            session.commit()
            raise AppError(
                ErrorCode.AUTH_REFRESH_REUSE_DETECTED,
                "检测到已轮转的旧 Refresh Token 被重放，已紧急吊销整个 Token 家族与会话",
                detail={"family_id": str(family.id), "rotation_count": family.rotation_count},
            )

        user = session.get(User, family.user_id)
        if user is None or user.status != "ACTIVE":
            raise AppError(
                ErrorCode.AUTH_INVALID_CREDENTIALS,
                "用户不存在或已禁用",
            )

        new_rotation = family.rotation_count + 1
        new_refresh_token = self._encode_refresh_token(
            user_id=user.id,
            session_id=family.session_id,
            family_id=family.id,
            rotation_count=new_rotation,
            now=now,
        )
        family.current_token_hash = _sha256_hex(new_refresh_token)
        family.rotation_count = new_rotation
        session.flush()

        new_access_token = self._encode_access_token(
            user=user,
            session_id=family.session_id,
            otp_verified=True,
            now=now,
        )
        return {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "token_type": "Bearer",
            "expires_in": self.settings.access_token_ttl_seconds,
            "rotation_count": new_rotation,
        }

    def logout(self, session: Session, *, session_id: uuid.UUID) -> None:
        """吊销当前会话及其关联的所有 Refresh Token 家族。"""
        now = datetime.now(timezone.utc)
        user_session = session.get(UserSession, session_id)
        if user_session is not None and user_session.revoked_at is None:
            user_session.revoked_at = now

        families = (
            session.execute(select(RefreshTokenFamily).where(RefreshTokenFamily.session_id == session_id))
            .scalars()
            .all()
        )
        for fam in families:
            if fam.revoked_at is None:
                fam.revoked_at = now
        session.flush()

    def _encode_access_token(
        self,
        *,
        user: User,
        session_id: uuid.UUID,
        otp_verified: bool,
        now: datetime,
    ) -> str:
        """编码签发短时 JWT Access Token。"""
        exp = now + timedelta(seconds=self.settings.access_token_ttl_seconds)
        claims = {
            "sub": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
            "sid": str(session_id),
            "otp_verified": otp_verified,
            "token_use": "access",
            "jti": str(uuid.uuid4()),
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }
        return jwt.encode(claims, self.settings.jwt_secret_key, algorithm=self.settings.jwt_algorithm)

    def _encode_refresh_token(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        family_id: uuid.UUID,
        rotation_count: int,
        now: datetime,
    ) -> str:
        """编码签发绑定 Family 的 Refresh Token。"""
        exp = now + timedelta(seconds=self.settings.refresh_token_ttl_seconds)
        claims = {
            "sub": str(user_id),
            "sid": str(session_id),
            "family_id": str(family_id),
            "rot": rotation_count,
            "token_use": "refresh",
            "jti": str(uuid.uuid4()),
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }
        return jwt.encode(claims, self.settings.jwt_secret_key, algorithm=self.settings.jwt_algorithm)

    def decode_jwt_token(self, token: str, *, expected_type: str = "access") -> dict[str, Any]:
        """校验并解码 JWT Token，强制验证过期时间与 token_use。"""
        try:
            payload = jwt.decode(
                token,
                self.settings.jwt_secret_key,
                algorithms=[self.settings.jwt_algorithm],
            )
        except jwt.ExpiredSignatureError as exc:
            raise AppError(
                ErrorCode.AUTH_TOKEN_EXPIRED,
                "JWT 令牌已过期",
            ) from exc
        except jwt.PyJWTError as exc:
            raise AppError(
                ErrorCode.AUTH_INVALID_CREDENTIALS,
                "无效的 JWT 令牌签名或格式",
            ) from exc

        if payload.get("token_use") != expected_type:
            raise AppError(
                ErrorCode.AUTH_INVALID_CREDENTIALS,
                f"期望令牌类型为 {expected_type}，实际收到 {payload.get('token_use')}",
            )
        if expected_type == "access" and not payload.get("otp_verified", False):
            raise AppError(
                ErrorCode.AUTH_OTP_REQUIRED,
                "当前会话尚未完成邮件二次验证（Email OTP）",
            )
        return payload

    def get_current_user_profile(self, session: Session, *, user_id: uuid.UUID) -> dict[str, Any]:
        """查询当前登录用户档案、所属组织、项目及绑定的角色列表。"""
        user = session.get(User, user_id)
        if user is None or user.status != "ACTIVE":
            raise AppError(ErrorCode.AUTH_INVALID_CREDENTIALS, "当前用户不存在或已被禁用")

        bind = session.get_bind()
        is_pg = bind is not None and bind.dialect.name == "postgresql"

        if is_pg:
            # 在 PostgreSQL FORCE RLS 下，通过受控 SECURITY DEFINER 函数仅查询当前已认证用户自身成员归属
            from sqlalchemy import text

            org_rows = session.execute(
                text("SELECT organization_id, slug, name, org_role FROM resolve_user_organizations(:uid)"),
                {"uid": str(user.id)},
            ).all()
            proj_rows = session.execute(
                text("SELECT project_id, organization_id, slug, name, environment FROM resolve_user_projects(:uid)"),
                {"uid": str(user.id)},
            ).all()
            role_rows = session.execute(
                text("SELECT organization_id, project_id, role_code, role_name, is_system FROM resolve_user_role_bindings(:uid)"),
                {"uid": str(user.id)},
            ).all()

            orgs = [
                {
                    "id": str(r[0]),
                    "slug": r[1],
                    "name": r[2],
                    "org_role": r[3],
                    "role": r[3],
                }
                for r in org_rows
            ]
            roles = [
                {
                    "organization_id": str(r[0]),
                    "project_id": str(r[1]) if r[1] else None,
                    "role_code": r[2],
                    "role_name": r[3],
                    "is_system": bool(r[4]),
                }
                for r in role_rows
            ]
            proj_role_map: dict[str, list[str]] = {}
            for rb in roles:
                if rb["project_id"]:
                    proj_role_map.setdefault(rb["project_id"], []).append(rb["role_code"])

            projects = [
                {
                    "id": str(r[0]),
                    "organization_id": str(r[1]),
                    "slug": r[2],
                    "name": r[3],
                    "environment": r[4],
                    "roles": sorted(set(proj_role_map.get(str(r[0]), []))),
                }
                for r in proj_rows
            ]
        else:
            org_memberships = (
                session.execute(
                    select(OrganizationMember, Organization)
                    .join(Organization, Organization.id == OrganizationMember.organization_id)
                    .where(
                        OrganizationMember.user_id == user.id,
                        OrganizationMember.status == "ACTIVE",
                    )
                )
                .all()
            )
            orgs = [
                {
                    "id": str(org.id),
                    "slug": org.slug,
                    "name": org.name,
                    "org_role": om.org_role,
                    "role": om.org_role,
                }
                for om, org in org_memberships
            ]
            active_org_ids = {org.id for _, org in org_memberships}

            proj_memberships = (
                session.execute(
                    select(ProjectMember, Project)
                    .join(Project, Project.id == ProjectMember.project_id)
                    .where(
                        ProjectMember.user_id == user.id,
                        ProjectMember.status == "ACTIVE",
                    )
                )
                .all()
            )
            role_bindings = (
                session.execute(
                    select(MemberRoleBinding, Role)
                    .join(Role, Role.id == MemberRoleBinding.role_id)
                    .where(MemberRoleBinding.user_id == user.id)
                )
                .all()
            )
            roles = [
                {
                    "organization_id": str(binding.organization_id),
                    "project_id": str(binding.project_id) if binding.project_id else None,
                    "role_code": role.code,
                    "role_name": role.name,
                    "is_system": role.is_system,
                }
                for binding, role in role_bindings
                if binding.organization_id in active_org_ids
            ]
            proj_role_map = {}
            for rb in roles:
                if rb["project_id"]:
                    proj_role_map.setdefault(rb["project_id"], []).append(rb["role_code"])

            projects = [
                {
                    "id": str(proj.id),
                    "organization_id": str(proj.organization_id),
                    "slug": proj.slug,
                    "name": proj.name,
                    "environment": proj.environment,
                    "roles": sorted(set(proj_role_map.get(str(proj.id), []))),
                }
                for pm, proj in proj_memberships
                if proj.organization_id in active_org_ids
            ]

        system_roles = sorted({rb["role_code"] for rb in roles if rb.get("is_system")})
        return {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
            "system_roles": system_roles,
            "user": {
                "id": str(user.id),
                "email": user.email,
                "display_name": user.display_name,
                "email_verified": user.email_verified,
            },
            "verification_label": "邮件二次验证",
            "organizations": orgs,
            "projects": projects,
            "roles": roles,
        }

    # ==================== 企业员工自助注册与管理员账号治理 ====================

    @staticmethod
    def _validate_registration_password(password: str) -> None:
        """校验企业员工密码基本复杂度（至少 8 位且同时包含字母与数字）。"""
        if len(password) < 8 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
            raise AppError(
                ErrorCode.DSL_SCHEMA_INVALID,
                "密码长度至少 8 位，且需同时包含英文字母与数字",
            )

    def register_employee(
        self,
        session: Session,
        *,
        email: str,
        display_name: str,
        password: str,
        org_slug: str = "org-alpha",
        ip_address: str = "127.0.0.1",
        user_agent: str = "etl-agent-client",
    ) -> dict[str, Any]:
        """企业员工自助注册：写入真实 User + 默认组织/项目 MAKER 最小权限，并立即发送 Email OTP 挑战。"""
        from sqlalchemy import text
        from app.shared.db_rls import TenantContext, apply_rls_context

        normalized_email = email.strip().lower()
        clean_name = display_name.strip()
        if "@" not in normalized_email or len(normalized_email) < 5:
            raise AppError(ErrorCode.DSL_SCHEMA_INVALID, "请输入合法的企业邮箱地址")
        if len(clean_name) < 2:
            raise AppError(ErrorCode.DSL_SCHEMA_INVALID, "姓名或昵称至少需要 2 个字符")
        self._validate_registration_password(password)

        existing = session.execute(select(User).where(User.email == normalized_email)).scalar_one_or_none()
        if existing is not None:
            raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, f"邮箱 '{normalized_email}' 已被注册，请直接登录")

        bind = session.get_bind()
        is_pg = bind is not None and bind.dialect.name == "postgresql"

        target_org_id: uuid.UUID
        target_proj_id: uuid.UUID
        maker_role_id: uuid.UUID

        if is_pg:
            row = session.execute(
                text(
                    "SELECT organization_id, project_id, role_id, role_code "
                    "FROM resolve_default_registration_target(:slug)"
                ),
                {"slug": org_slug},
            ).first()
            if row is None:
                raise AppError(ErrorCode.TENANT_CONTEXT_MISSING, "尚未初始化默认企业组织或项目，请联系管理员")
            target_org_id = uuid.UUID(str(row[0]))
            target_proj_id = uuid.UUID(str(row[1]))
            maker_role_id = uuid.UUID(str(row[2]))
        else:
            org = session.execute(
                select(Organization).where(Organization.slug == org_slug, Organization.status == "ACTIVE")
            ).scalar_one_or_none() or session.execute(
                select(Organization).where(Organization.status == "ACTIVE")
            ).scalars().first()
            if org is None:
                raise AppError(ErrorCode.TENANT_CONTEXT_MISSING, "尚未初始化默认企业组织，请联系管理员")
            proj = session.execute(
                select(Project).where(Project.organization_id == org.id, Project.status == "ACTIVE")
            ).scalars().first()
            maker_role = session.execute(
                select(Role).where(Role.code == "MAKER", Role.is_system.is_(True))
            ).scalar_one_or_none()
            if proj is None or maker_role is None:
                raise AppError(ErrorCode.TENANT_CONTEXT_MISSING, "尚未初始化默认项目或 MAKER 角色，请联系管理员")
            target_org_id = org.id
            target_proj_id = proj.id
            maker_role_id = maker_role.id

        # 创建全局员工账号（密码使用 PBKDF2-HMAC-SHA256 加盐哈希）
        user = User(
            email=normalized_email,
            display_name=clean_name,
            password_hash=hash_password(password),
            status="ACTIVE",
            email_verified=False,
        )
        session.add(user)
        session.flush()

        # 在当前事务注入目标组织/项目 RLS 上下文，严格遵循 FORCE ROW LEVEL SECURITY 写入成员关系
        apply_rls_context(
            session,
            TenantContext(
                organization_id=target_org_id,
                project_id=target_proj_id,
                actor_id=str(user.id),
            ),
            require_project=True,
        )
        session.add(
            OrganizationMember(
                organization_id=target_org_id,
                user_id=user.id,
                org_role="MEMBER",
                status="ACTIVE",
            )
        )
        session.add(
            ProjectMember(
                organization_id=target_org_id,
                project_id=target_proj_id,
                user_id=user.id,
                status="ACTIVE",
            )
        )
        session.add(
            MemberRoleBinding(
                organization_id=target_org_id,
                project_id=target_proj_id,
                user_id=user.id,
                role_id=maker_role_id,
                granted_by=user.id,
            )
        )
        session.flush()

        # 注册成功后立即发送 Email OTP 挑战，无缝衔接前端邮件二次验证登录
        challenge = self.login_with_password(
            session,
            email=normalized_email,
            password=password,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return {
            **challenge,
            "user_id": str(user.id),
            "assigned_role": "MAKER",
            "message": "注册成功！已分配默认最小权限角色 MAKER，请前往邮箱（Mailpit）查看 6 位验证码完成登录。",
        }

    def list_org_project_users(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """在当前组织/项目 RLS 保护下，列出企业成员花名册、账号状态与角色绑定。"""
        org_members = (
            session.execute(
                select(OrganizationMember, User)
                .join(User, User.id == OrganizationMember.user_id)
                .where(OrganizationMember.organization_id == organization_id)
                .order_by(User.created_at.asc())
            )
            .all()
        )
        proj_member_ids = {
            pm.user_id
            for pm in session.execute(
                select(ProjectMember).where(
                    ProjectMember.organization_id == organization_id,
                    ProjectMember.project_id == project_id,
                    ProjectMember.status == "ACTIVE",
                )
            )
            .scalars()
            .all()
        }
        bindings = (
            session.execute(
                select(MemberRoleBinding, Role)
                .join(Role, Role.id == MemberRoleBinding.role_id)
                .where(
                    MemberRoleBinding.organization_id == organization_id,
                    MemberRoleBinding.project_id == project_id,
                )
            )
            .all()
        )
        roles_by_user: dict[uuid.UUID, list[str]] = {}
        for binding, role in bindings:
            roles_by_user.setdefault(binding.user_id, []).append(role.code)

        return [
            {
                "id": str(user.id),
                "email": user.email,
                "display_name": user.display_name,
                "status": user.status,
                "email_verified": user.email_verified,
                "org_role": om.org_role,
                "in_project": user.id in proj_member_ids,
                "project_roles": sorted(set(roles_by_user.get(user.id, []))),
                "created_at": user.created_at.isoformat(),
            }
            for om, user in org_members
        ]

    def admin_create_employee(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        email: str,
        display_name: str,
        password: str,
        org_role: str,
        project_role_code: str,
        actor_user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """管理员直接创建企业员工账号并分配组织角色与项目角色。"""
        normalized_email = email.strip().lower()
        clean_name = display_name.strip()
        if "@" not in normalized_email or len(normalized_email) < 5:
            raise AppError(ErrorCode.DSL_SCHEMA_INVALID, "请输入合法的企业邮箱地址")
        if len(clean_name) < 2:
            raise AppError(ErrorCode.DSL_SCHEMA_INVALID, "姓名至少需要 2 个字符")
        self._validate_registration_password(password)

        clean_org_role = org_role.strip().upper()
        if clean_org_role not in ("MEMBER", "ORG_ADMIN"):
            raise AppError(ErrorCode.DSL_SCHEMA_INVALID, "org_role 仅允许 MEMBER 或 ORG_ADMIN")
        clean_proj_role = project_role_code.strip().upper()
        if clean_proj_role not in SYSTEM_ROLE_CODES:
            raise AppError(ErrorCode.DSL_SCHEMA_INVALID, f"无效的系统角色：{clean_proj_role}")

        if session.execute(select(User).where(User.email == normalized_email)).scalar_one_or_none() is not None:
            raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, f"邮箱 '{normalized_email}' 已存在")

        role_row = session.execute(select(Role).where(Role.code == clean_proj_role)).scalar_one_or_none()
        if role_row is None:
            raise AppError(ErrorCode.DSL_SCHEMA_INVALID, f"角色 '{clean_proj_role}' 未初始化")

        user = User(
            email=normalized_email,
            display_name=clean_name,
            password_hash=hash_password(password),
            status="ACTIVE",
            email_verified=True,
        )
        session.add(user)
        session.flush()

        session.add(
            OrganizationMember(
                organization_id=organization_id,
                user_id=user.id,
                org_role=clean_org_role,
                status="ACTIVE",
            )
        )
        session.add(
            ProjectMember(
                organization_id=organization_id,
                project_id=project_id,
                user_id=user.id,
                status="ACTIVE",
            )
        )
        session.add(
            MemberRoleBinding(
                organization_id=organization_id,
                project_id=project_id,
                user_id=user.id,
                role_id=role_row.id,
                granted_by=actor_user_id,
            )
        )
        session.flush()
        return {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
            "status": user.status,
            "org_role": clean_org_role,
            "in_project": True,
            "project_roles": [clean_proj_role],
            "created_at": user.created_at.isoformat(),
        }

    def admin_update_user_status(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        target_user_id: uuid.UUID,
        new_status: str,
        actor_user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """管理员启用或禁用本组织员工账号（禁止管理员禁用自身账号，禁用同时吊销活跃会话）。"""
        clean_status = new_status.strip().upper()
        if clean_status not in ("ACTIVE", "DISABLED"):
            raise AppError(ErrorCode.DSL_SCHEMA_INVALID, "账号状态仅允许 ACTIVE 或 DISABLED")
        if target_user_id == actor_user_id and clean_status == "DISABLED":
            raise AppError(ErrorCode.SOD_VIOLATION, "管理员不可禁用当前正在登录的自身账号")

        om = session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == target_user_id,
            )
        ).scalar_one_or_none()
        user = session.get(User, target_user_id)
        if om is None or user is None:
            raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标员工不属于当前组织或不存在")

        user.status = clean_status
        om.status = "ACTIVE" if clean_status == "ACTIVE" else "SUSPENDED"
        if clean_status == "DISABLED":
            now = datetime.now(timezone.utc)
            active_sessions = (
                session.execute(
                    select(UserSession).where(
                        UserSession.user_id == target_user_id,
                        UserSession.revoked_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            for s in active_sessions:
                s.revoked_at = now
        session.flush()
        return {"id": str(user.id), "email": user.email, "status": user.status}

    def admin_update_user_roles(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        target_user_id: uuid.UUID,
        org_role: str | None,
        project_role_codes: list[str],
        actor_user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """管理员调整指定员工在当前组织/项目中的角色绑定。"""
        om = session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == target_user_id,
            )
        ).scalar_one_or_none()
        user = session.get(User, target_user_id)
        if om is None or user is None:
            raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标员工不属于当前组织或不存在")

        if org_role:
            clean_org_role = org_role.strip().upper()
            if clean_org_role not in ("MEMBER", "ORG_ADMIN"):
                raise AppError(ErrorCode.DSL_SCHEMA_INVALID, "org_role 仅允许 MEMBER 或 ORG_ADMIN")
            om.org_role = clean_org_role

        clean_codes = sorted({c.strip().upper() for c in project_role_codes if c.strip()})
        if not clean_codes:
            raise AppError(ErrorCode.DSL_SCHEMA_INVALID, "至少需要分配一个项目角色")
        invalid = [c for c in clean_codes if c not in SYSTEM_ROLE_CODES]
        if invalid:
            raise AppError(ErrorCode.DSL_SCHEMA_INVALID, f"包含无效系统角色：{invalid}")

        # 确保用户属于当前项目成员
        pm = session.execute(
            select(ProjectMember).where(
                ProjectMember.organization_id == organization_id,
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == target_user_id,
            )
        ).scalar_one_or_none()
        if pm is None:
            session.add(
                ProjectMember(
                    organization_id=organization_id,
                    project_id=project_id,
                    user_id=target_user_id,
                    status="ACTIVE",
                )
            )
        else:
            pm.status = "ACTIVE"

        # 替换当前项目下的角色绑定
        old_bindings = (
            session.execute(
                select(MemberRoleBinding).where(
                    MemberRoleBinding.organization_id == organization_id,
                    MemberRoleBinding.project_id == project_id,
                    MemberRoleBinding.user_id == target_user_id,
                )
            )
            .scalars()
            .all()
        )
        for b in old_bindings:
            session.delete(b)
        session.flush()

        for code in clean_codes:
            role_row = session.execute(select(Role).where(Role.code == code)).scalar_one_or_none()
            if role_row is not None:
                session.add(
                    MemberRoleBinding(
                        organization_id=organization_id,
                        project_id=project_id,
                        user_id=target_user_id,
                        role_id=role_row.id,
                        granted_by=actor_user_id,
                    )
                )
        session.flush()
        return {
            "id": str(user.id),
            "email": user.email,
            "org_role": om.org_role,
            "project_roles": clean_codes,
        }


def seed_demo_iam_data(
    session: Session,
    *,
    default_password: str = "DemoPass!2026",
) -> dict[str, Any]:
    """幂等初始化固定系统角色、权限、多组织、多项目及 6 类预置演示账号。

    预置用户（遵循 PRD §3.2 & §8.1）：
    - maker@etl-agent.local (MAKER)
    - data_checker@etl-agent.local (DATA_CHECKER)
    - security_checker@etl-agent.local (SECURITY_CHECKER)
    - operator@etl-agent.local (OPERATOR)
    - auditor@etl-agent.local (AUDITOR)
    - admin@etl-agent.local (ORG_ADMIN + PROJECT_ADMIN)
    """
    # 1. 创建权限表记录
    perm_by_code: dict[str, Permission] = {}
    for code, res_type, action, desc in DEFAULT_PERMISSIONS_SPEC:
        existing_perm = session.execute(select(Permission).where(Permission.code == code)).scalar_one_or_none()
        if existing_perm is None:
            existing_perm = Permission(
                code=code,
                resource_type=res_type,
                action=action,
                description=desc,
            )
            session.add(existing_perm)
            session.flush()
        perm_by_code[code] = existing_perm

    # 2. 创建不可变固定系统角色
    role_by_code: dict[str, Role] = {}
    for code, name, desc in SYSTEM_ROLES_SPEC:
        existing_role = session.execute(
            select(Role).where(Role.code == code, Role.is_system.is_(True))
        ).scalar_one_or_none()
        if existing_role is None:
            existing_role = Role(
                organization_id=None,
                project_id=None,
                code=code,
                name=name,
                is_system=True,
                description=desc,
            )
            session.add(existing_role)
            session.flush()
        role_by_code[code] = existing_role

        for perm_code in ROLE_PERMISSION_MATRIX.get(code, ()):
            perm = perm_by_code[perm_code]
            existing_rp = session.execute(
                select(RolePermission).where(
                    RolePermission.role_id == existing_role.id,
                    RolePermission.permission_id == perm.id,
                )
            ).scalar_one_or_none()
            if existing_rp is None:
                session.add(RolePermission(role_id=existing_role.id, permission_id=perm.id))

    # 3. 创建两个演示组织（Enterprise Alpha 与 FinTech Beta）
    org_specs = [
        ("org-alpha", "Enterprise Data Org Alpha"),
        ("org-beta", "FinTech Analytics Org Beta"),
    ]
    orgs: dict[str, Organization] = {}
    for slug, name in org_specs:
        org = session.execute(select(Organization).where(Organization.slug == slug)).scalar_one_or_none()
        if org is None:
            org = Organization(slug=slug, name=name, status="ACTIVE")
            session.add(org)
            session.flush()
        orgs[slug] = org

    # 4. 创建多项目
    proj_specs = [
        ("org-alpha", "proj-orders-prod", "电商订单宽表同步项目 (Prod)", "prod"),
        ("org-alpha", "proj-logs-staging", "日志清洗与异常分流项目 (Staging)", "staging"),
        ("org-beta", "proj-risk-dev", "风控特征实时 CDC 项目 (Dev)", "dev"),
    ]
    projects: dict[str, Project] = {}
    for org_slug, proj_slug, proj_name, env in proj_specs:
        org = orgs[org_slug]
        proj = session.execute(
            select(Project).where(Project.organization_id == org.id, Project.slug == proj_slug)
        ).scalar_one_or_none()
        if proj is None:
            proj = Project(
                organization_id=org.id,
                slug=proj_slug,
                name=proj_name,
                environment=env,
                llm_data_policy_json={
                    "allow_schema_metadata": True,
                    "allow_masked_samples": True,
                    "allow_raw_sensitive_rows": False,
                },
                status="ACTIVE",
            )
            session.add(proj)
            session.flush()
        projects[proj_slug] = proj

    # 5. 创建 6 类标准演示用户并绑定跨组织/多项目成员与角色
    user_specs = [
        ("maker", "maker@etl-agent.local", "Maker 数据工程师", "MAKER", "MEMBER"),
        ("data_checker", "data_checker@etl-agent.local", "Data Checker 数据审批人", "DATA_CHECKER", "MEMBER"),
        (
            "security_checker",
            "security_checker@etl-agent.local",
            "Security Checker 安全审批人",
            "SECURITY_CHECKER",
            "MEMBER",
        ),
        ("operator", "operator@etl-agent.local", "Operator 系统操作员", "OPERATOR", "MEMBER"),
        ("auditor", "auditor@etl-agent.local", "Auditor 审计员", "AUDITOR", "MEMBER"),
        ("admin", "admin@etl-agent.local", "Org & Project 管理员", "PROJECT_ADMIN", "ORG_ADMIN"),
    ]
    pwd_hash = hash_password(default_password)
    users: dict[str, User] = {}

    for key, email, display_name, proj_role_code, org_role in user_specs:
        user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            user = User(
                email=email,
                display_name=display_name,
                password_hash=pwd_hash,
                status="ACTIVE",
                email_verified=True,
            )
            session.add(user)
            session.flush()
        users[key] = user

        # 加入所有演示组织与项目，验证多组织/多项目支持
        for org in orgs.values():
            om = session.execute(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == org.id,
                    OrganizationMember.user_id == user.id,
                )
            ).scalar_one_or_none()
            if om is None:
                session.add(
                    OrganizationMember(
                        organization_id=org.id,
                        user_id=user.id,
                        org_role=org_role,
                        status="ACTIVE",
                    )
                )

        for proj in projects.values():
            pm = session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == proj.id,
                    ProjectMember.user_id == user.id,
                )
            ).scalar_one_or_none()
            if pm is None:
                session.add(
                    ProjectMember(
                        organization_id=proj.organization_id,
                        project_id=proj.id,
                        user_id=user.id,
                        status="ACTIVE",
                    )
                )

            role_obj = role_by_code[proj_role_code]
            binding = session.execute(
                select(MemberRoleBinding).where(
                    MemberRoleBinding.organization_id == proj.organization_id,
                    MemberRoleBinding.project_id == proj.id,
                    MemberRoleBinding.user_id == user.id,
                    MemberRoleBinding.role_id == role_obj.id,
                )
            ).scalar_one_or_none()
            if binding is None:
                session.add(
                    MemberRoleBinding(
                        organization_id=proj.organization_id,
                        project_id=proj.id,
                        user_id=user.id,
                        role_id=role_obj.id,
                        granted_by=user.id,
                    )
                )

    session.flush()
    return {
        "organizations": orgs,
        "projects": projects,
        "users": users,
        "roles": role_by_code,
        "permissions": perm_by_code,
    }
