"""
JWT token generation and validation
"""
from datetime import datetime, timedelta
from typing import Any

from jose import JWTError, jwt

from app.config import settings


class TokenError(Exception):
    """Exception raised for token-related errors."""
    pass


def create_access_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    """
    Create a JWT access token.

    Args:
        data: Payload data to encode in the token
        expires_delta: Custom expiration time (optional)

    Returns:
        Encoded JWT token string
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            minutes=settings.jwt_access_token_expire_minutes
        )

    to_encode.update({
        "exp": expire,
        "iat": datetime.utcnow(),
        "type": "access",
    })

    encoded_jwt = jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    return encoded_jwt


def create_refresh_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    """
    Create a JWT refresh token.

    Refresh tokens have longer expiration and are used to obtain new access tokens.

    Args:
        data: Payload data to encode in the token
        expires_delta: Custom expiration time (optional)

    Returns:
        Encoded JWT refresh token string
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            days=settings.jwt_refresh_token_expire_days
        )

    to_encode.update({
        "exp": expire,
        "iat": datetime.utcnow(),
        "type": "refresh",
    })

    encoded_jwt = jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    return encoded_jwt


def verify_token(token: str, expected_type: str = "access") -> dict[str, Any]:
    """
    Verify and decode a JWT token.

    Args:
        token: The JWT token to verify
        expected_type: Expected token type ("access" or "refresh")

    Returns:
        Decoded token payload

    Raises:
        TokenError: If token is invalid, expired, or wrong type
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )

        # Verify token type
        token_type = payload.get("type")
        if token_type != expected_type:
            raise TokenError(f"Token type mismatch: expected {expected_type}, got {token_type}")

        return payload

    except jwt.ExpiredSignatureError:
        raise TokenError("Token expired")
    except JWTError as e:
        raise TokenError(f"Invalid token: {str(e)}")


def decode_token_without_verification(token: str) -> dict[str, Any]:
    """
    Decode a JWT token without verifying signature.

    Useful for extracting claims from expired tokens.

    Args:
        token: The JWT token to decode

    Returns:
        Decoded token payload (unverified)
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
        )
        return payload
    except JWTError as e:
        raise TokenError(f"Cannot decode token: {str(e)}")
