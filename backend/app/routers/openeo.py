import base64
import json
import os

import openeo
from openeo.rest import OpenEoApiError, OpenEoClientException

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


router = APIRouter(prefix="/openeo", tags=["openeo"])


OPENEO_BACKEND_URL = os.getenv("OPENEO_BACKEND_URL", "https://openeo.cloud").rstrip("/")
OPENEO_BEARER_TOKEN = (os.getenv("OPENEO_BEARER_TOKEN", "") or "").strip()


class OpenEOResultRequest(BaseModel):
    process: dict[str, object] = Field(
        description="openEO process graph payload that will be POSTed to /result"
    )
    bearer_token: str | None = Field(
        default=None,
        description="Optional bearer token. If omitted, backend env OPENEO_BEARER_TOKEN is used.",
    )


class OpenEOProcessValidationRequest(BaseModel):
    process_graph: dict[str, object]


class OpenEOProxyResponse(BaseModel):
    backend_url: str
    status_code: int
    data: dict[str, object] | list[object]


def _auth_header(token: str | None) -> dict[str, str]:
    resolved = (token or OPENEO_BEARER_TOKEN or "").strip()
    if not resolved:
        return {}
    return {"Authorization": f"Bearer {resolved}"}


def _resolve_token(token: str | None) -> str | None:
    resolved = (token or OPENEO_BEARER_TOKEN or "").strip()
    return resolved or None


def _connect(token: str | None = None):
    try:
        connection = openeo.connect(OPENEO_BACKEND_URL)
        resolved = _resolve_token(token)
        if resolved:
            connection = connection.authenticate_bearer_token(resolved)
        return connection
    except OpenEoApiError as exc:
        raise HTTPException(status_code=502, detail=f"openEO API error while connecting: {exc}") from exc
    except OpenEoClientException as exc:
        raise HTTPException(status_code=502, detail=f"openEO client error while connecting: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not initialize openEO connection: {exc}") from exc


def _request_json(path: str, token: str | None = None, method: str = "GET", payload: dict[str, object] | None = None) -> OpenEOProxyResponse:
    connection = _connect(token=token)
    try:
        if method.upper() == "GET":
            response = connection.get(path=path, expected_status=200)
        elif method.upper() == "POST":
            response = connection.post(path=path, json=payload or {}, expected_status=200)
        else:
            raise HTTPException(status_code=500, detail=f"Unsupported method in proxy helper: {method}")

        parsed = response.json()
        if not isinstance(parsed, (dict, list)):
            raise HTTPException(status_code=502, detail="openEO response is not valid JSON object/list")
        return OpenEOProxyResponse(
            backend_url=OPENEO_BACKEND_URL,
            status_code=response.status_code,
            data=parsed,
        )
    except OpenEoApiError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"openEO backend error: {exc}",
        ) from exc
    except OpenEoClientException as exc:
        raise HTTPException(status_code=502, detail=f"openEO client error: {exc}") from exc


@router.get("/health")
async def openeo_health():
    connection = _connect()
    payload = connection.capabilities()
    return {
        "status": "ok",
        "backend_url": OPENEO_BACKEND_URL,
        "api_version": payload.api_version(),
        "backend_version": payload.get("backend_version"),
        "authenticated_by_default": bool(OPENEO_BEARER_TOKEN),
    }


@router.get("/collections")
async def openeo_collections(token: str | None = None):
    connection = _connect(token=token)
    listing = connection.list_collections()
    return {
        "backend_url": OPENEO_BACKEND_URL,
        "status_code": 200,
        "data": {
            "collections": list(listing),
            "links": [
                {"rel": l.rel, "href": l.href, "type": l.type, "title": l.title}
                for l in listing.links
            ],
        },
    }


@router.get("/collections/{collection_id}")
async def openeo_collection(collection_id: str, token: str | None = None):
    connection = _connect(token=token)
    return {
        "backend_url": OPENEO_BACKEND_URL,
        "status_code": 200,
        "data": connection.describe_collection(collection_id),
    }


@router.get("/file-formats")
async def openeo_file_formats(token: str | None = None):
    connection = _connect(token=token)
    return {
        "backend_url": OPENEO_BACKEND_URL,
        "status_code": 200,
        "data": dict(connection.list_file_formats()),
    }


@router.post("/validation")
async def openeo_validation(payload: OpenEOProcessValidationRequest, token: str | None = None):
    connection = _connect(token=token)
    validation = connection.validate_process_graph(payload.process_graph)
    return {
        "backend_url": OPENEO_BACKEND_URL,
        "status_code": 200,
        "data": {
            "errors": list(validation),
            "federation_backends": validation.ext_federation_backends(),
        },
    }


@router.post("/result")
async def openeo_result(payload: OpenEOResultRequest):
    connection = _connect(token=payload.bearer_token)

    try:
        # Only forward openEO /result schema fields (do not leak local helper fields like bearer_token).
        response = connection.post(path="/result", json={"process": payload.process})
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        raw = response.content

        if "application/json" in content_type:
            try:
                return {
                    "backend_url": OPENEO_BACKEND_URL,
                    "status_code": response.status_code,
                    "content_type": content_type,
                    "data": json.loads(raw.decode("utf-8")),
                }
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Invalid JSON payload from openEO /result: {exc}") from exc

        # Binary response (e.g., image/tiff, image/png) is base64-wrapped for API transport.
        return {
            "backend_url": OPENEO_BACKEND_URL,
            "status_code": response.status_code,
            "content_type": content_type,
            "bytes_base64": base64.b64encode(raw).decode("ascii"),
        }
    except OpenEoApiError as exc:
        error_status = getattr(exc, "http_status_code", None) or 502
        raise HTTPException(status_code=error_status, detail=f"openEO /result API error: {exc}") from exc
    except OpenEoClientException as exc:
        raise HTTPException(status_code=502, detail=f"openEO /result client error: {exc}") from exc
