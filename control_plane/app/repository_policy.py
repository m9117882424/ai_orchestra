from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import re
from urllib.parse import urlsplit, urlunsplit


MAX_REMOTE_URL_LENGTH = 1024
KNOWN_PROVIDER_HOSTS = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
}
RESERVED_HOST_SUFFIXES = (
    ".example",
    ".internal",
    ".invalid",
    ".local",
    ".localhost",
    ".test",
)
NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,118}[a-z0-9])?")
PROFILE_REF_RE = re.compile(r"[a-z0-9](?:[a-z0-9._:-]{0,78}[a-z0-9])?")
SECRET_LIKE_PREFIXES = (
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "glpat-",
    "sk-",
    "xoxb-",
    "xoxp-",
)
PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._~-]{0,198}[A-Za-z0-9])?")
DOMAIN_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class RepositoryPolicyError(ValueError):
    """A deterministic, user-safe repository policy rejection."""


@dataclass(frozen=True)
class NormalizedRepositoryRemote:
    url: str
    identity: str
    host: str
    provider: str


def normalize_repository_name(value: str) -> str:
    normalized = value.strip().lower()
    if not NAME_RE.fullmatch(normalized):
        raise RepositoryPolicyError(
            "Имя репозитория должно содержать 1–120 символов: a-z, 0-9, точка, дефис или подчеркивание"
        )
    return normalized


def normalize_profile_reference(value: str, *, field_name: str) -> str:
    normalized = value.strip().lower()
    if not PROFILE_REF_RE.fullmatch(normalized):
        raise RepositoryPolicyError(
            f"{field_name} должен быть непрозрачным идентификатором из 1–80 безопасных символов"
        )
    if normalized.startswith(SECRET_LIKE_PREFIXES):
        raise RepositoryPolicyError(
            f"{field_name} похож на credential; передавайте только имя auth profile"
        )
    return normalized


def normalize_repository_host(hostname: str) -> str:
    if hostname.endswith("."):
        raise RepositoryPolicyError("Git remote host с завершающей точкой запрещен")
    try:
        host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise RepositoryPolicyError("Git remote содержит некорректное имя host") from exc

    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        raise RepositoryPolicyError("Git remote по IP-адресу запрещен; используйте разрешенное DNS-имя")

    if host == "localhost" or host.endswith(RESERVED_HOST_SUFFIXES):
        raise RepositoryPolicyError("Локальные и зарезервированные Git remote host запрещены")
    if len(host) > 253 or "." not in host:
        raise RepositoryPolicyError("Git remote должен использовать полное DNS-имя")
    if any(not DOMAIN_LABEL_RE.fullmatch(label) for label in host.split(".")):
        raise RepositoryPolicyError("Git remote содержит некорректное DNS-имя")
    return host


def normalize_repository_remote(value: str) -> NormalizedRepositoryRemote:
    raw = value.strip()
    if not raw or len(raw) > MAX_REMOTE_URL_LENGTH:
        raise RepositoryPolicyError(
            f"Git remote должен содержать от 1 до {MAX_REMOTE_URL_LENGTH} символов"
        )
    if any(ord(character) < 33 or ord(character) > 126 for character in raw):
        raise RepositoryPolicyError("Git remote должен быть печатным ASCII URL без пробелов")
    if "\\" in raw or "%" in raw:
        raise RepositoryPolicyError("Git remote с обратным слешем или percent-encoding запрещен")

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise RepositoryPolicyError("Git remote имеет некорректный URL") from exc

    if parsed.scheme.lower() != "https":
        raise RepositoryPolicyError("Разрешены только HTTPS Git remote")
    if not parsed.hostname:
        raise RepositoryPolicyError("Git remote не содержит host")
    if parsed.username is not None or parsed.password is not None:
        raise RepositoryPolicyError("Credentials внутри Git remote запрещены")
    if parsed.query or parsed.fragment:
        raise RepositoryPolicyError("Query и fragment в Git remote запрещены")
    if port not in (None, 443):
        raise RepositoryPolicyError("Git remote разрешен только через стандартный HTTPS port 443")

    host = normalize_repository_host(parsed.hostname)
    path = parsed.path.rstrip("/")
    if not path.startswith("/") or path == "/" or "//" in path:
        raise RepositoryPolicyError("Git remote должен содержать однозначный путь репозитория")

    segments = path[1:].split("/")
    if any(segment in {"", ".", ".."} or not PATH_SEGMENT_RE.fullmatch(segment) for segment in segments):
        raise RepositoryPolicyError("Git remote содержит небезопасный сегмент пути")

    provider = KNOWN_PROVIDER_HOSTS.get(host, "generic")
    if provider != "generic" and len(segments) < 2:
        raise RepositoryPolicyError("Git remote провайдера должен содержать владельца и репозиторий")

    identity_segments = list(segments)
    if identity_segments[-1].lower().endswith(".git"):
        identity_segments[-1] = identity_segments[-1][:-4]
    if not identity_segments[-1]:
        raise RepositoryPolicyError("Git remote не содержит имя репозитория")

    canonical_path = "/" + "/".join(segments)
    identity_path = "/" + "/".join(identity_segments)
    if provider != "generic":
        identity_path = identity_path.lower()

    return NormalizedRepositoryRemote(
        url=urlunsplit(("https", host, canonical_path, "", "")),
        identity=f"{host}{identity_path}",
        host=host,
        provider=provider,
    )


def validate_assurance_configuration(tier: str, profile: str | None) -> None:
    if tier == "regulated-critical" and profile is None:
        raise RepositoryPolicyError(
            "Для regulated-critical обязателен отдельный assurance_profile"
        )
    if tier != "regulated-critical" and profile is not None:
        raise RepositoryPolicyError(
            "assurance_profile разрешен только для regulated-critical"
        )
