import re
from typing import Optional


def quote_url(url):
    return url.replace('{','%7B').replace('}','%7D')


def cookie_get(cookie: str, name: str) -> Optional[str]:
    """
    Extract a cookie value from a raw Cookie header string.
    Works whether the cookie appears at the end (no trailing ';') or not.
    """
    if not cookie:
        return None
    m = re.search(r'(?:^|;\s*)' + re.escape(name) + r'=([^;]+)', cookie)
    return m.group(1) if m else None


def require_cookie_fields(cookie: str, *names: str) -> None:
    missing = [n for n in names if not cookie_get(cookie, n)]
    if missing:
        raise ValueError(f"cookie 缺少字段: {', '.join(missing)} (至少需要 auth_token 与 ct0)")
