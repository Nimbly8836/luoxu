"""Password hashing and JWT authentication helpers."""

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt  # type: ignore[import-not-found]
from argon2 import PasswordHasher  # type: ignore[import-not-found]
from argon2.exceptions import (  # type: ignore[import-not-found]
  InvalidHashError,
  VerificationError,
  VerifyMismatchError,
)


@dataclass(frozen=True)
class Principal:
  user_id: str | None
  username: str | None
  is_admin: bool = False
  is_anonymous: bool = True


class AuthService:
  def __init__(self, config: dict[str, Any]):
    self.secret = str(config.get("jwt_secret", ""))
    if len(self.secret.encode("utf-8")) < 32:
      raise ValueError("jwt_secret must contain at least 32 bytes")
    self.access_ttl = self._positive_int(config.get("access_token_ttl"), 900)
    self.refresh_ttl = self._positive_int(config.get("refresh_token_ttl"), 30 * 86400)
    if self.access_ttl >= self.refresh_ttl:
      raise ValueError("access token TTL must be shorter than refresh token TTL")
    self.hasher = PasswordHasher()

  @staticmethod
  def _positive_int(value: Any, default: int) -> int:
    try:
      result = int(value) if value is not None else default
    except (TypeError, ValueError) as exc:
      raise ValueError("token TTL must be an integer") from exc
    if result <= 0:
      raise ValueError("token TTL must be positive")
    return result

  def hash_password(self, password: str) -> str:
    if not isinstance(password, str) or not 8 <= len(password) <= 1024:
      raise ValueError("password must contain 8 to 1024 characters")
    return self.hasher.hash(password)

  def verify_password(self, password_hash: str, password: str) -> bool:
    if not isinstance(password, str) or len(password) > 1024:
      return False
    try:
      return self.hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
      return False

  def _encode(
    self,
    subject: str,
    username: str,
    is_admin: bool,
    token_type: str,
    ttl: int,
    jti: str | None = None,
  ) -> str:
    try:
      now = int(time.time())
    except (OverflowError, OSError) as exc:
      raise RuntimeError("cannot read system clock") from exc
    claims = {
      "sub": subject,
      "username": username,
      "admin": is_admin,
      "typ": token_type,
      "iat": now,
      "exp": now + ttl,
    }
    if jti:
      claims["jti"] = jti
    return jwt.encode(claims, self.secret, algorithm="HS256")

  def access_token(self, user: dict[str, Any]) -> str:
    return self._encode(
      str(user["id"]), user["username"], user["is_admin"], "access", self.access_ttl
    )

  def refresh_token(self, user: dict[str, Any]) -> tuple[str, str, int]:
    jti = str(uuid.uuid4())
    return (
      self._encode(
        str(user["id"]),
        user["username"],
        user["is_admin"],
        "refresh",
        self.refresh_ttl,
        jti,
      ),
      jti,
      self.refresh_ttl,
    )

  def decode(self, token: str, expected_type: str = "access") -> dict[str, Any]:
    required = ["sub", "username", "admin", "typ", "iat", "exp"]
    if expected_type == "refresh":
      required.append("jti")
    claims = jwt.decode(
      token,
      self.secret,
      algorithms=["HS256"],
      options={"require": required},
    )
    if claims.get("typ") != expected_type or not isinstance(claims["sub"], str):
      raise jwt.InvalidTokenError("unexpected token type")
    return claims

  @staticmethod
  def token_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

  @staticmethod
  def random_secret() -> str:
    return secrets.token_urlsafe(32)
