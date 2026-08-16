import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .settings import get_settings


security = HTTPBasic()


def require_manager(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> str:
    settings = get_settings()
    username_ok = secrets.compare_digest(credentials.username, settings.server_username)
    password_ok = secrets.compare_digest(credentials.password, settings.server_password)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учетные данные",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def require_control_request(
    marker: Annotated[str | None, Header(alias="X-Control-Request")] = None,
) -> None:
    if marker != "ai-orchestra":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Для изменения состояния требуется заголовок X-Control-Request",
        )
