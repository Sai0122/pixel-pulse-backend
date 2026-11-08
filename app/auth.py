
import os
import time
import uuid
import re

import jwt
from pwdlib import PasswordHash
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from . import db

# Authentication configuration
SECRET_KEY = os.getenv("JWT_SECRET_KEY")

if not SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY environment variable is not configured")

ALGORITHM = "HS256"
TOKEN_EXPIRE_SECONDS = 60 * 60 * 8

password_hash = PasswordHash.recommended()
bearer_scheme = HTTPBearer(auto_error=False)

ALLOWED_ROLES = {"Creator", "Reviewer", "Manager", "Admin"}


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def create_access_token(user: dict) -> str:
    now = int(time.time())

    payload = {
        "sub": user["id"],
        "role": user["role"],
        "iat": now,
        "exp": now + TOKEN_EXPIRE_SECONDS,
    }

    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(
            credentials.credentials,
            SECRET_KEY,
            algorithms=[ALGORITHM],
        )
        user_id = payload.get("sub")

        if not user_id:
            raise HTTPException(401, "Invalid authentication token")

    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid or expired authentication token")

    with db.conn() as connection:
        user = connection.execute(
            """
            SELECT id, name, email, role, is_active
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()

    if not user or not user["is_active"]:
        raise HTTPException(401, "User account is inactive or unavailable")

    return dict(user)


def require_roles(*roles):
    def role_checker(user=Depends(get_current_user)):
        if user["role"] not in roles:
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to perform this action",
            )
        return user

    return role_checker


def validate_registration(name: str, email: str, password: str):
    if not name.strip():
        raise HTTPException(422, "Name is required")

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
        raise HTTPException(422, "Enter a valid email address")

    if len(password) < 12:
        raise HTTPException(
            422,
            "Password must contain at least 12 characters",
        )

    if len(password) > 128:
        raise HTTPException(422, "Password is too long")
