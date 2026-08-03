from __future__ import annotations

import ipaddress
import os
import re
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import urljoin, urlparse


class UnsafeUrlError(ValueError):
    pass


class DownloadLimitExceeded(RuntimeError):
    pass


class DownloadCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaPolicy:
    max_media_bytes: int = 100 * 1024 * 1024
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 60.0
    task_timeout_seconds: float = 1800.0
    max_redirects: int = 5

    @classmethod
    def from_environ(cls, environ: Optional[Mapping[str, str]] = None) -> "MediaPolicy":
        values = dict(os.environ if environ is None else environ)
        return cls(
            max_media_bytes=max(1, int(values.get("MAX_MEDIA_BYTES", 100 * 1024 * 1024))),
            connect_timeout_seconds=max(
                0.1, float(values.get("MEDIA_CONNECT_TIMEOUT_SECONDS", 10))
            ),
            read_timeout_seconds=max(
                0.1, float(values.get("MEDIA_READ_TIMEOUT_SECONDS", 60))
            ),
            task_timeout_seconds=max(
                1.0, float(values.get("MEDIA_TASK_TIMEOUT_SECONDS", 1800))
            ),
            max_redirects=max(0, int(values.get("MEDIA_MAX_REDIRECTS", 5))),
        )


_URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = "'\"),.;!?]}，。；！？）》】」』"


def extract_first_url(text: str) -> Optional[str]:
    match = _URL_PATTERN.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)


def _host_matches(host: str, domain: str) -> bool:
    normalized = host.lower().rstrip(".")
    return normalized == domain or normalized.endswith(f".{domain}")


def detect_text_source(text: str) -> Optional[str]:
    url = extract_first_url(text)
    if url:
        host = (urlparse(url).hostname or "").lower()
        if _host_matches(host, "youtube.com") or _host_matches(host, "youtu.be"):
            return "youtube"
        if _host_matches(host, "douyin.com") or _host_matches(
            host, "iesdouyin.com"
        ):
            return "douyin"
        return "video_url"
    normalized = (text or "").lower()
    if "抖音" in normalized and any(token in normalized for token in ("复制", "口令", "打开")):
        return "douyin"
    return None


def _resolved_addresses(host: str, port: int, resolver) -> set[str]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return {str(literal)}

    try:
        results = resolver(host, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise UnsafeUrlError(f"无法解析链接主机：{host}") from exc
    addresses: set[str] = set()
    for result in results:
        try:
            addresses.add(str(result[4][0]))
        except (IndexError, TypeError):
            continue
    if not addresses:
        raise UnsafeUrlError(f"链接主机没有可用地址：{host}")
    return addresses


def validate_public_url(
    url: str,
    *,
    resolver: Callable = socket.getaddrinfo,
) -> str:
    value = (url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("只允许 HTTP/HTTPS 链接。")
    if not parsed.hostname:
        raise UnsafeUrlError("链接缺少有效主机名。")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("链接不能包含用户名或密码。")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    for address in _resolved_addresses(parsed.hostname, port, resolver):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeUrlError(f"链接解析到无效地址：{address}") from exc
        if not ip.is_global:
            raise UnsafeUrlError("链接指向非公网地址，已拒绝访问。")
    return value


def sanitize_upload_name(value: str, *, default: str = "upload.bin") -> str:
    basename = Path((value or "").replace("\\", "/")).name.strip()
    basename = re.sub(r"[^\w.-]+", "_", basename, flags=re.UNICODE)
    basename = basename.strip("._")
    if not basename:
        basename = default
    stem = Path(basename).stem[:80] or "upload"
    suffix = Path(basename).suffix[:16]
    return f"{stem}{suffix}"


def _check_cancelled(cancelled: Optional[Callable[[], bool]]) -> None:
    if cancelled is not None and cancelled():
        raise DownloadCancelled("任务已取消。")


def _check_deadline(deadline) -> None:
    if deadline is not None:
        deadline.check()


def download_public_url(
    url: str,
    destination: str | Path,
    *,
    policy: Optional[MediaPolicy] = None,
    session=None,
    resolver: Callable = socket.getaddrinfo,
    proxies: Optional[Mapping[str, str]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    deadline=None,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> int:
    import requests

    active_policy = policy or MediaPolicy.from_environ()
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination_path.with_name(
        f".{destination_path.name}.{uuid.uuid4().hex}.part"
    )
    own_session = session is None
    active_session = session or requests.Session()
    response = None
    current_url = url
    try:
        for redirect_count in range(active_policy.max_redirects + 1):
            _check_cancelled(cancelled)
            _check_deadline(deadline)
            current_url = validate_public_url(current_url, resolver=resolver)
            response = active_session.get(
                current_url,
                stream=True,
                allow_redirects=False,
                timeout=(
                    active_policy.connect_timeout_seconds,
                    active_policy.read_timeout_seconds,
                ),
                proxies=dict(proxies) if proxies else None,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                response.close()
                response = None
                if not location:
                    raise UnsafeUrlError("下载重定向缺少目标地址。")
                if redirect_count >= active_policy.max_redirects:
                    raise UnsafeUrlError("下载重定向次数过多。")
                current_url = urljoin(current_url, location)
                continue
            break
        if response is None:
            raise UnsafeUrlError("无法获得下载响应。")
        response.raise_for_status()

        content_length: Optional[int] = None
        raw_length = response.headers.get("content-length")
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError:
                content_length = None
        if content_length is not None and content_length > active_policy.max_media_bytes:
            raise DownloadLimitExceeded(
                f"媒体文件超过限制（最大 {active_policy.max_media_bytes} 字节）。"
            )

        total = 0
        with part_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                _check_cancelled(cancelled)
                _check_deadline(deadline)
                if not chunk:
                    continue
                total += len(chunk)
                if total > active_policy.max_media_bytes:
                    raise DownloadLimitExceeded(
                        f"媒体文件超过限制（最大 {active_policy.max_media_bytes} 字节）。"
                    )
                handle.write(chunk)
                if on_progress is not None:
                    on_progress(total, content_length)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part_path, destination_path)
        return total
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: S110 - cleanup must not mask the request error
                pass
        if own_session:
            try:
                active_session.close()
            except Exception:  # noqa: S110 - cleanup must not mask the request error
                pass
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass
