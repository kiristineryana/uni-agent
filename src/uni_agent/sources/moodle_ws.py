"""Read-only Moodle web service client (token from Profile -> Preferences -> Security keys).
Only the read functions in ALLOWED can ever be called. The token is never logged or put in error messages."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

ALLOWED = frozenset({"core_webservice_get_site_info", "core_enrol_get_users_courses", "core_course_get_contents"})
# Error codes meaning "this Moodle/token doesn't allow web services": stop, don't work around it.
PERMANENT = {"invalidtoken", "accessexception", "webservicesnotenabled", "servicerequireslogin",
             "servicenotavailable", "invalidlogin", "usernotfullysetup", "sitemaintenance"}


class MoodleWSError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    @property
    def permanent(self) -> bool:
        return self.code in PERMANENT


@dataclass(frozen=True)
class RemoteFile:
    section: str
    module: str
    filename: str
    fileurl: str
    timemodified: int
    filesize: int


def base_url_from(ical_url: str) -> str:
    u = urlparse(ical_url)
    return f"{u.scheme}://{u.netloc}"


class MoodleWS:
    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=60, follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, function: str, **params):
        if function not in ALLOWED:  # hard guarantee: this agent never writes to Moodle
            raise ValueError(f"{function} is not an allowed read-only Moodle function")
        try:
            r = await self._client.get(f"{self.base_url}/webservice/rest/server.php", params={
                "wstoken": self._token, "wsfunction": function, "moodlewsrestformat": "json", **params})
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise MoodleWSError(f"network:{type(e).__name__}") from None  # str(e) could contain the token
        if isinstance(data, dict) and "exception" in data:
            raise MoodleWSError(str(data.get("errorcode") or "unknown"))
        return data

    async def download(self, fileurl: str, dest: Path, max_bytes: int) -> None:
        try:
            async with self._client.stream("GET", fileurl, params={"token": self._token}) as r:
                if r.status_code != 200:
                    raise MoodleWSError(f"http:{r.status_code}")
                if "json" in r.headers.get("content-type", ""):  # Moodle returns errors as JSON
                    raise MoodleWSError("download_refused")
                size = 0
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise MoodleWSError("too_large")
                        f.write(chunk)
        except httpx.HTTPError as e:
            raise MoodleWSError(f"network:{type(e).__name__}") from None


def list_files(contents: list) -> list[RemoteFile]:
    """core_course_get_contents -> every downloadable file (resources and folders), skipping links/pages."""
    files = []
    for section in contents or []:
        for module in section.get("modules", []) or []:
            if module.get("modname") not in ("resource", "folder"):
                continue
            for c in module.get("contents", []) or []:
                if c.get("type") != "file" or not c.get("fileurl"):
                    continue
                files.append(RemoteFile(
                    section=str(section.get("name") or ""),
                    module=str(module.get("name") or ""),
                    filename=str(c.get("filename") or ""),
                    fileurl=str(c["fileurl"]),
                    timemodified=int(c.get("timemodified") or 0),
                    filesize=int(c.get("filesize") or 0),
                ))
    return files
