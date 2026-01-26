"""
OAuth 2.0 endpoints for Claude Desktop MCP connector.

Implements OAuth 2.0 Authorization Code flow for Claude Desktop
custom connectors as per MCP specification.
"""
import secrets
import logging
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Form, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.auth.jwt import create_access_token

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oauth"])

# In-memory storage for OAuth state (in production, use Redis)
_oauth_states: dict[str, dict] = {}
_oauth_codes: dict[str, dict] = {}

# OAuth Client for Claude Desktop (fixed credentials)
CLAUDE_CLIENT_ID = "claude-desktop-mcp"
CLAUDE_CLIENT_SECRET = "sei-mcp-oauth-secret-2026"

logger.info(f"OAuth Client ID: {CLAUDE_CLIENT_ID}")


class TokenResponse(BaseModel):
    """OAuth token response."""
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    refresh_token: str | None = None
    scope: str = "mcp"


@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    """
    OAuth 2.0 Authorization Server Metadata.

    Returns server metadata as per RFC 8414.
    """
    base_url = str(request.base_url).rstrip("/")

    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "registration_endpoint": f"{base_url}/oauth/register",
        "scopes_supported": ["mcp", "openid", "profile", "email"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256", "plain"],
    }


@router.get("/oauth/authorize")
async def oauth_authorize(
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    response_type: str = Query("code"),
    scope: str = Query("mcp"),
    state: str = Query(None),
    code_challenge: str = Query(None),
    code_challenge_method: str = Query(None),
):
    """
    OAuth 2.0 Authorization Endpoint.

    Shows login form and handles authorization.
    """
    if response_type != "code":
        raise HTTPException(400, "Only 'code' response_type is supported")

    # Store state for verification
    auth_state = secrets.token_urlsafe(32)
    _oauth_states[auth_state] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "created_at": datetime.now(timezone.utc),
    }

    # Return login form with token option
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Login - SEI MCP</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                   background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                   min-height: 100vh; display: flex; align-items: center; justify-content: center; margin: 0; }}
            .container {{ background: white; padding: 2rem; border-radius: 12px; box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                         max-width: 400px; width: 90%; }}
            h1 {{ color: #333; margin-bottom: 0.5rem; font-size: 1.5rem; text-align: center; }}
            p {{ color: #666; margin-bottom: 1.5rem; font-size: 0.9rem; text-align: center; }}
            input {{ width: 100%; padding: 12px; margin-bottom: 1rem; border: 1px solid #ddd; border-radius: 8px;
                    font-size: 1rem; box-sizing: border-box; }}
            input:focus {{ outline: none; border-color: #667eea; }}
            button {{ width: 100%; padding: 12px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                     color: white; border: none; border-radius: 8px; font-size: 1rem; cursor: pointer; margin-bottom: 0.5rem; }}
            button:hover {{ opacity: 0.9; }}
            .btn-token {{ background: linear-gradient(135deg, #00b894 0%, #00cec9 100%); }}
            .divider {{ text-align: center; margin: 1.5rem 0; color: #999; position: relative; }}
            .divider::before, .divider::after {{ content: ''; position: absolute; top: 50%; width: 40%; height: 1px; background: #ddd; }}
            .divider::before {{ left: 0; }}
            .divider::after {{ right: 0; }}
            .tabs {{ display: flex; margin-bottom: 1.5rem; border-bottom: 2px solid #eee; }}
            .tab {{ flex: 1; padding: 10px; text-align: center; cursor: pointer; color: #666; border-bottom: 2px solid transparent; margin-bottom: -2px; }}
            .tab.active {{ color: #667eea; border-bottom-color: #667eea; }}
            .tab-content {{ display: none; }}
            .tab-content.active {{ display: block; }}
            .error {{ color: #e74c3c; margin-bottom: 1rem; font-size: 0.9rem; text-align: center; }}
            .info {{ background: #e8f4fd; border: 1px solid #b8daff; border-radius: 8px; padding: 12px; margin-bottom: 1rem; font-size: 0.85rem; color: #004085; }}
            .register-link {{ text-align: center; margin-top: 1rem; font-size: 0.9rem; color: #666; }}
            .register-link a {{ color: #667eea; text-decoration: none; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Autorizar SEI MCP</h1>
            <p>Conectar ao Claude Desktop</p>

            <div class="tabs">
                <div class="tab active" onclick="showTab('login')">Email/Senha</div>
                <div class="tab" onclick="showTab('token')">API Token</div>
            </div>

            <div id="login-tab" class="tab-content active">
                <form method="POST" action="/oauth/authorize/submit">
                    <input type="hidden" name="auth_state" value="{auth_state}">
                    <input type="hidden" name="auth_method" value="password">
                    <input type="email" name="email" placeholder="Email" required>
                    <input type="password" name="password" placeholder="Senha" required>
                    <button type="submit">Autorizar</button>
                </form>
                <div class="register-link">
                    Nao tem conta? <a href="/" target="_blank">Criar conta</a>
                </div>
            </div>

            <div id="token-tab" class="tab-content">
                <div class="info">
                    Cole seu API Token gerado em <a href="/" target="_blank">sei-tribunais-licensing-api.onrender.com</a>
                </div>
                <form method="POST" action="/oauth/authorize/token">
                    <input type="hidden" name="auth_state" value="{auth_state}">
                    <input type="text" name="api_token" placeholder="sei_xxxxxxxx..." required>
                    <button type="submit" class="btn-token">Autorizar com Token</button>
                </form>
            </div>
        </div>

        <script>
            function showTab(tab) {{
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                document.querySelector('.tab:nth-child(' + (tab === 'login' ? '1' : '2') + ')').classList.add('active');
                document.getElementById(tab + '-tab').classList.add('active');
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@router.post("/oauth/authorize/submit")
async def oauth_authorize_submit(
    auth_state: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Handle OAuth authorization form submission.
    """
    import bcrypt

    # Verify state
    if auth_state not in _oauth_states:
        raise HTTPException(400, "Invalid or expired authorization state")

    state_data = _oauth_states.pop(auth_state)

    # Check if state is expired (10 minutes)
    if datetime.now(timezone.utc) - state_data["created_at"] > timedelta(minutes=10):
        raise HTTPException(400, "Authorization state expired")

    # Authenticate user
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not user.password_hash:
        return HTMLResponse(content="""
            <html><body>
            <script>alert('Email ou senha invalidos'); history.back();</script>
            </body></html>
        """)

    # Verify password
    if not bcrypt.checkpw(password.encode('utf-8'), user.password_hash.encode('utf-8')):
        return HTMLResponse(content="""
            <html><body>
            <script>alert('Email ou senha invalidos'); history.back();</script>
            </body></html>
        """)

    # Generate authorization code
    auth_code = secrets.token_urlsafe(32)
    _oauth_codes[auth_code] = {
        "user_id": user.id,
        "user_email": user.email,
        "client_id": state_data["client_id"],
        "redirect_uri": state_data["redirect_uri"],
        "scope": state_data["scope"],
        "code_challenge": state_data["code_challenge"],
        "code_challenge_method": state_data["code_challenge_method"],
        "created_at": datetime.now(timezone.utc),
    }

    # Build redirect URL
    params = {"code": auth_code}
    if state_data["state"]:
        params["state"] = state_data["state"]

    redirect_url = f"{state_data['redirect_uri']}?{urlencode(params)}"
    return RedirectResponse(url=redirect_url, status_code=302)


@router.post("/oauth/authorize/token")
async def oauth_authorize_token(
    auth_state: str = Form(...),
    api_token: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Handle OAuth authorization via API token.
    """
    from hashlib import sha256

    # Verify state
    if auth_state not in _oauth_states:
        return HTMLResponse(content="""
            <html><body>
            <script>alert('Sessao expirada. Tente novamente.'); window.close();</script>
            </body></html>
        """)

    state_data = _oauth_states.pop(auth_state)

    # Check if state is expired (10 minutes)
    if datetime.now(timezone.utc) - state_data["created_at"] > timedelta(minutes=10):
        return HTMLResponse(content="""
            <html><body>
            <script>alert('Sessao expirada. Tente novamente.'); window.close();</script>
            </body></html>
        """)

    # Validate API token format
    if not api_token.startswith("sei_"):
        return HTMLResponse(content="""
            <html><body>
            <script>alert('Token invalido. O token deve comecar com sei_'); history.back();</script>
            </body></html>
        """)

    # Hash token and find user
    token_hash = sha256(api_token.encode()).hexdigest()
    result = await db.execute(select(User).where(User.api_token_hash == token_hash))
    user = result.scalar_one_or_none()

    if not user:
        return HTMLResponse(content="""
            <html><body>
            <script>alert('Token invalido ou expirado'); history.back();</script>
            </body></html>
        """)

    # Generate authorization code
    auth_code = secrets.token_urlsafe(32)
    _oauth_codes[auth_code] = {
        "user_id": user.id,
        "user_email": user.email,
        "client_id": state_data["client_id"],
        "redirect_uri": state_data["redirect_uri"],
        "scope": state_data["scope"],
        "code_challenge": state_data["code_challenge"],
        "code_challenge_method": state_data["code_challenge_method"],
        "created_at": datetime.now(timezone.utc),
    }

    # Build redirect URL
    params = {"code": auth_code}
    if state_data["state"]:
        params["state"] = state_data["state"]

    redirect_url = f"{state_data['redirect_uri']}?{urlencode(params)}"
    return RedirectResponse(url=redirect_url, status_code=302)


@router.post("/oauth/token")
async def oauth_token(
    grant_type: str = Form(...),
    code: str = Form(None),
    redirect_uri: str = Form(None),
    client_id: str = Form(None),
    client_secret: str = Form(None),
    code_verifier: str = Form(None),
    refresh_token: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """
    OAuth 2.0 Token Endpoint.

    Exchanges authorization code for access token.
    """
    if grant_type == "authorization_code":
        if not code:
            raise HTTPException(400, {"error": "invalid_request", "error_description": "Missing code"})

        # Verify code
        if code not in _oauth_codes:
            raise HTTPException(400, {"error": "invalid_grant", "error_description": "Invalid or expired code"})

        code_data = _oauth_codes.pop(code)

        # Check if code is expired (5 minutes)
        if datetime.now(timezone.utc) - code_data["created_at"] > timedelta(minutes=5):
            raise HTTPException(400, {"error": "invalid_grant", "error_description": "Code expired"})

        # Verify PKCE if used
        if code_data["code_challenge"]:
            if not code_verifier:
                raise HTTPException(400, {"error": "invalid_request", "error_description": "Missing code_verifier"})

            if code_data["code_challenge_method"] == "S256":
                computed = sha256(code_verifier.encode()).hexdigest()
                # Base64URL encode
                import base64
                computed = base64.urlsafe_b64encode(
                    bytes.fromhex(computed)
                ).decode().rstrip("=")
            else:
                computed = code_verifier

            if computed != code_data["code_challenge"]:
                raise HTTPException(400, {"error": "invalid_grant", "error_description": "Invalid code_verifier"})

        # Generate tokens
        access_token = create_access_token({
            "sub": code_data["user_id"],
            "email": code_data["user_email"],
            "scope": code_data["scope"],
        })

        # Generate refresh token
        refresh = secrets.token_urlsafe(32)

        # Store refresh token hash on user
        result = await db.execute(select(User).where(User.id == code_data["user_id"]))
        user = result.scalar_one_or_none()
        if user:
            user.refresh_token_hash = sha256(refresh.encode()).hexdigest()
            await db.commit()

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh,
            expires_in=settings.jwt_access_token_expire_minutes * 60,
        )

    elif grant_type == "refresh_token":
        if not refresh_token:
            raise HTTPException(400, {"error": "invalid_request", "error_description": "Missing refresh_token"})

        # Find user by refresh token hash
        token_hash = sha256(refresh_token.encode()).hexdigest()
        result = await db.execute(select(User).where(User.refresh_token_hash == token_hash))
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(400, {"error": "invalid_grant", "error_description": "Invalid refresh token"})

        # Generate new tokens
        access_token = create_access_token({
            "sub": user.id,
            "email": user.email,
            "scope": "mcp",
        })

        new_refresh = secrets.token_urlsafe(32)
        user.refresh_token_hash = sha256(new_refresh.encode()).hexdigest()
        await db.commit()

        return TokenResponse(
            access_token=access_token,
            refresh_token=new_refresh,
            expires_in=settings.jwt_access_token_expire_minutes * 60,
        )

    else:
        raise HTTPException(400, {"error": "unsupported_grant_type"})


@router.post("/oauth/register")
async def oauth_register(request: Request):
    """
    OAuth 2.0 Dynamic Client Registration (RFC 7591).

    Allows Claude Desktop to register as a client.
    """
    data = await request.json()

    # Generate client credentials
    client_id = f"claude-{secrets.token_hex(8)}"
    client_secret = secrets.token_urlsafe(32)

    # In production, store these in database
    logger.info(f"Registered new OAuth client: {client_id}")

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": data.get("client_name", "Claude Desktop"),
        "redirect_uris": data.get("redirect_uris", ["https://claude.ai/api/mcp/auth_callback"]),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }
