from __future__ import annotations

import secrets
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from satrx.core.config import Settings

_bearer = HTTPBearer(auto_error=False)


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def require_auth(settings: Settings = Depends(get_settings_dep),
                 creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                 token_q: str | None = Query(None, alias="token", include_in_schema=False)) -> None:
    """Bearer fejléc; a `?token=` paraméter a böngésző által közvetlenül töltött fájlokhoz (<audio>, <img>) kell."""
    token = settings.server.auth_token
    if token is None:
        return
    supplied = creds.credentials if creds is not None else token_q
    if supplied is None or not secrets.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="Invalid or missing token")
