from __future__ import annotations
import asyncio
import logging
from functools import lru_cache
from typing import Any
import aiohttp
from dotenv import load_dotenv
# from common_config import BaseAppSettings, NonEmptyStr
from pydantic_settings import BaseSettings
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)

class FindAddressCandidatesPayload(BaseModel):
   """Wire-level response from ``/findAddressCandidates``.
   ``extra="allow"`` so additive fields don't break the validator.
   """
   model_config = ConfigDict(extra="allow")
   spatialReference: dict[str, Any] | None = None
   candidates: list[dict[str, Any]] = Field(default_factory=list)
   error: dict[str, Any] | None = None

class SuggestPayload(BaseModel):
    """Wire-level response from ``/suggest``.
    ``extra="allow"`` so additive fields don't break the validator.
    """
    model_config = ConfigDict(extra="allow")
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    error: dict[str, Any] | None = None

_AUTH_ERROR_CODES: frozenset[int] = frozenset({401, 403, 498, 499})

class ArcGISSettings(BaseSettings):
   """Settings for the ArcGIS World Geocoder tool family.
   Auth uses an API key (Esri Location Platform) passed as the ``token``
   query parameter. The endpoint defaults to the public Location Platform
   base; the Enhanced endpoint is selectable via env override.
   """
   api_key: str = Field(validation_alias="ESRI_API_KEY")
   endpoint: HttpUrl = Field(
       default=HttpUrl("https://geocode-api.arcgis.com/arcgis/rest/services/World/GeocodeServer"),
       validation_alias="ARCGIS_GEOCODE_ENDPOINT",
   )
   request_timeout_seconds: float = Field(
       default=10.0,
       validation_alias="ARCGIS_GEOCODE_TIMEOUT",
   )
   max_retries: int = Field(
       default=3,
       validation_alias="ARCGIS_GEOCODE_MAX_RETRIES",
   )
   @property
   def endpoint_str(self) -> str:
       return str(self.endpoint).rstrip("/")

@lru_cache(maxsize=1)
def load_arcgis_settings() -> ArcGISSettings:
   return ArcGISSettings()  # ty: ignore[missing-argument]

class ArcGISRateLimited(Exception):
   """Raised internally when retries are exhausted on 429; surfaced as a
   structured tool result via the service layer."""
   def __init__(self, message: str, retry_after_seconds: float | None) -> None:
       super().__init__(message)
       self.retry_after_seconds = retry_after_seconds

class ArcGISClient:
   """Async client for ArcGIS World Geocoder REST endpoints.
   - Auth: API key (Esri Location Platform) passed as ``token`` query
     parameter on every request.
   - Retries: ``settings.max_retries`` attempts on HTTP 5xx and 429 with
     exponential backoff plus any ``Retry-After`` hint.
   - 401/403/498/499 → ``RuntimeError`` (raised, not retried) — config/auth
     problem.
   - 429 exhausted → ``ArcGISRateLimited`` (raised; callers turn into a
     structured response).
   - In-body ``{"error": {"code": ...}}`` errors are honored at the same
     severity as the matching HTTP status would be.
   """
   def __init__(
       self,
       settings: ArcGISSettings,
       *,
       session: aiohttp.ClientSession | None = None,
   ) -> None:
       self._settings = settings
       self._own_session = session is None
       # aiohttp binds the session to the running event loop at construction,
       # so build it lazily on first use rather than eagerly in __init__.
       self._session = session
       self._timeout = aiohttp.ClientTimeout(total=settings.request_timeout_seconds)
   def _get_session(self) -> aiohttp.ClientSession:
       if self._session is None:
           self._session = aiohttp.ClientSession(timeout=self._timeout)
       return self._session
   async def aclose(self) -> None:
       if self._own_session and self._session is not None:
           await self._session.close()
   async def __aenter__(self) -> ArcGISClient:
       return self
   async def __aexit__(self, *_: object) -> None:
       await self.aclose()
   async def _request(self, path: str, params: dict[str, str]) -> dict[str, Any]:
       url = f"{self._settings.endpoint_str}{path}"
       full_params = {
           **params,
           "f": "json",
           "token": self._settings.api_key,
       }
       session = self._get_session()
       last_exc: Exception | None = None
       attempts = max(1, self._settings.max_retries)
       for attempt in range(attempts):
           is_last = attempt + 1 >= attempts
           try:
               async with session.get(url, params=full_params) as response:
                   status = response.status
                   if status in _AUTH_ERROR_CODES:
                       raise RuntimeError(
                           f"ArcGIS rejected the credential (status={status}). "
                           "Verify ESRI_API_KEY and that the key has Geocoding privileges."
                       )
                   if status == 429:
                       retry_after = _parse_retry_after(response.headers.get("retry-after"))
                       if is_last:
                           raise ArcGISRateLimited(
                               "ArcGIS rate limit exceeded after retries.",
                               retry_after,
                           )
                       await self._sleep_backoff(attempt, retry_after)
                       continue
                   if 500 <= status < 600:
                       last_exc = aiohttp.ClientResponseError(
                           response.request_info,
                           response.history,
                           status=status,
                           message=f"ArcGIS server error {status}",
                       )
                       logger.warning(
                           "arcgis 5xx attempt=%d/%d path=%s status=%d",
                           attempt + 1,
                           attempts,
                           path,
                           status,
                       )
                       if is_last:
                           continue
                       await self._sleep_backoff(attempt, None)
                       continue
                   if status >= 400:
                       # Non-retryable client error (400/404/...) — surface
                       # immediately, mirroring httpx's raise_for_status().
                       body = (await response.text())[:200]
                       raise RuntimeError(f"ArcGIS request failed (status={status}): {body}")
                   payload = await response.json(content_type=None)
                   self._raise_on_body_error(payload)
                   return payload
           except (TimeoutError, aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as exc:
               last_exc = exc
               logger.warning(
                   "arcgis transport error attempt=%d/%d path=%s err=%s",
                   attempt + 1,
                   attempts,
                   path,
                   exc,
               )
               if is_last:
                   continue
               await self._sleep_backoff(attempt, None)
               continue
       assert last_exc is not None
       raise last_exc
   @staticmethod
   def _raise_on_body_error(payload: dict[str, Any]) -> None:
       """ArcGIS sometimes returns 200 with ``{"error": {...}}``."""
       err = payload.get("error")
       if not isinstance(err, dict):
           return
       code = err.get("code")
       message = err.get("message") or "ArcGIS returned an error in the response body."
       if isinstance(code, int) and code in _AUTH_ERROR_CODES:
           raise RuntimeError(f"ArcGIS error code={code}: {message}")
       if code == 429:
           raise ArcGISRateLimited(message, None)
       raise aiohttp.ClientError(f"ArcGIS error code={code}: {message}")
   @staticmethod
   async def _sleep_backoff(attempt: int, retry_after: float | None) -> None:
       delay = retry_after if retry_after is not None else 0.1 * (2**attempt)
       await asyncio.sleep(min(delay, 5.0))
   async def find_address_candidates(
       self,
       query: str,
       *,
       top: int = 5,
       location: tuple[float, float] | None = None,
       magic_key: str | None = None,
       country_code: str | None = None,
   ) -> FindAddressCandidatesPayload:
       params: dict[str, str] = {
           "SingleLine": query,
           "maxLocations": str(top),
           "outFields": "*",
       }
       if location is not None:
           # ``location`` is a proximity bias (ranks nearby candidates higher).
           # Internal tuples are (lat, lon); ArcGIS wants ``x,y`` = ``lon,lat``.
           lat, lon = location
           params["location"] = f"{lon},{lat}"
       if magic_key:
           params["magicKey"] = magic_key
       if country_code:
           params["sourceCountry"] = country_code
       raw = await self._request("/findAddressCandidates", params)
       return FindAddressCandidatesPayload.model_validate(raw)

   async def suggest(
       self,
       text: str,
       *,
       top: int = 5,
       location: tuple[float, float] | None = None,
       country_code: str | None = None,
   ) -> SuggestPayload:
       params: dict[str, str] = {
           "text": text,
           "maxSuggestions": str(top),
       }
       if location is not None:
           # Internal tuples are (lat, lon); ArcGIS wants ``x,y`` = ``lon,lat``.
           lat, lon = location
           params["location"] = f"{lon},{lat}"
       if country_code:
           params["countryCode"] = country_code
       raw = await self._request("/suggest", params)
       return SuggestPayload.model_validate(raw)

def _parse_retry_after(value: str | None) -> float | None:
   if value is None:
       return None
   try:
       return max(0.0, float(value))
   except ValueError:
       return None

_CLIENT_LOCK = asyncio.Lock()
_CLIENT_SINGLETON: ArcGISClient | None = None

async def get_arcgis_client() -> ArcGISClient:
   """Return a process-wide ``ArcGISClient`` singleton.
   Consumers must call ``close_arcgis_client`` on shutdown to release the
   aiohttp session.
   """
   global _CLIENT_SINGLETON
   if _CLIENT_SINGLETON is not None:
       return _CLIENT_SINGLETON
   async with _CLIENT_LOCK:
       if _CLIENT_SINGLETON is None:
           _CLIENT_SINGLETON = ArcGISClient(load_arcgis_settings())
       return _CLIENT_SINGLETON

async def close_arcgis_client() -> None:
   """Close and discard the process-wide client singleton.
   Releases the aiohttp session. A no-op when no client was created, so it is
   safe to call unconditionally from a shutdown hook even if the ArcGIS tools
   were never exercised.
   """
   global _CLIENT_SINGLETON
   async with _CLIENT_LOCK:
       if _CLIENT_SINGLETON is not None:
           await _CLIENT_SINGLETON.aclose()
           _CLIENT_SINGLETON = None