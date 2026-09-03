"""Shared map clients: coords, sync httpx, AMap/Baidu/OSM, status contract."""

from __future__ import annotations

import json
import math
import os
import random
import threading
import time
import urllib.parse
from typing import Any

import httpx

from version import SERVER_VERSION


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


HTTP_TIMEOUT = _env_float("HTTP_TIMEOUT_SECONDS", 12.0)
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OVERPASS_BATCH_SIZE = 10
# Set OSM_ENABLED=false to skip all Overpass calls (e.g. when the Overpass host
# is blocked by an egress allowlist). Defaults to enabled.
OSM_ENABLED = os.environ.get("OSM_ENABLED", "true").strip().lower() not in (
    "0", "false", "no", "off"
)
BAIDU_DEFAULT_QUERY = "公司|住宅|写字楼|生活服务"
PROJECT_KEYWORDS = os.environ.get("PROJECT_KEYWORDS", "在建|项目|工地|建设")
_PROJECT_SIGNAL_EXTRA = ("工程", "construction", "development", "project")
AMAP_REGEO_BATCH_SIZE = 20
EXPAND_RADIUS_FACTOR = 2.5
EXPAND_RADIUS_MAX_M = 5000.0
AMAP_QPS_LIMIT = _env_float("AMAP_QPS_LIMIT", 3.0)
BAIDU_QPS_LIMIT = _env_float("BAIDU_QPS_LIMIT", 3.0)
AMAP_BATCH_SIZE = _env_int("AMAP_BATCH_SIZE", 5)
AMAP_BATCH_DELAY_MS = _env_int("AMAP_BATCH_DELAY_MS", 2000)
AMAP_RETRY_MAX = _env_int("AMAP_RETRY_MAX", 3)
AMAP_RETRY_BASE_MS = _env_int("AMAP_RETRY_BASE_MS", 500)
BAIDU_RETRY_MAX = _env_int("BAIDU_RETRY_MAX", 3)
BAIDU_RETRY_BASE_MS = _env_int("BAIDU_RETRY_BASE_MS", 500)
RATE_LIMIT_BATCH_RATIO = _env_float("RATE_LIMIT_BATCH_RATIO", 0.10)
BURST_PROBE_TIMEOUT_MS = _env_int("BURST_PROBE_TIMEOUT_MS", 3000)
BURST_PROBE_MAX_CONCURRENCY = _env_int("BURST_PROBE_MAX_CONCURRENCY", 8)
LAST_RETRY_AFTER_MS: dict[str, int] = {"amap": 0, "baidu": 0}


class TokenBucket:
    def __init__(self, rate: float) -> None:
        self.rate = max(rate, 0.1)
        self.capacity = max(rate, 1.0)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.updated
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.updated = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            wait = (1.0 - self.tokens) / self.rate
            self.tokens = 0.0
            self.updated = now + wait
        if wait > 0:
            time.sleep(wait)


_AMAP_BUCKET = TokenBucket(AMAP_QPS_LIMIT)
_BAIDU_BUCKET = TokenBucket(BAIDU_QPS_LIMIT)

NO_API_KEY = "NO_API_KEY"
INVALID_API_KEY = "INVALID_API_KEY"
RATE_LIMIT = "RATE_LIMIT"
TIMEOUT = "TIMEOUT"
HTTP_ERROR = "HTTP_ERROR"
UPSTREAM_ERROR = "UPSTREAM_ERROR"
INVALID_RESPONSE = "INVALID_RESPONSE"
DISABLED = "DISABLED"

X_PI = math.pi * 3000.0 / 180.0
PI = math.pi
A = 6378245.0
EE = 0.00669342162296594323

from geo_geometry import deg_to_m_factors  # noqa: E402


def _out_of_china(lon: float, lat: float) -> bool:
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * PI) + 320 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * PI
    magic = math.sin(rad_lat)
    magic = 1 - EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sqrt_magic) * PI)
    dlon = (dlon * 180.0) / (A / sqrt_magic * math.cos(rad_lat) * PI)
    return lon + dlon, lat + dlat


def gcj02_to_bd09(lon: float, lat: float) -> tuple[float, float]:
    z = math.sqrt(lon * lon + lat * lat) + 0.00002 * math.sin(lat * X_PI)
    theta = math.atan2(lat, lon) + 0.000003 * math.cos(lon * X_PI)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def wgs84_to_bd09(lon: float, lat: float) -> tuple[float, float]:
    return gcj02_to_bd09(*wgs84_to_gcj02(lon, lat))


def _host_bucket(url: str) -> str:
    if "amap.com" in url:
        return "amap"
    if "baidu.com" in url:
        return "baidu"
    return "overpass"


def _amap_reason_code(info: str) -> str:
    u = (info or "").upper()
    if any(k in u for k in ("INVALID_USER", "USERKEY", "INVALID_USER_KEY", "KEY")):
        return INVALID_API_KEY
    if any(k in u for k in ("LIMIT", "QPS", "CUQPS", "OVER_LIMIT")):
        return RATE_LIMIT
    return UPSTREAM_ERROR


def _baidu_reason_code(status: Any, message: str) -> str:
    try:
        code = int(status)
    except (TypeError, ValueError):
        code = -1
    if code in (2, 4, 5, 101, 200, 201, 202, 203, 210, 211, 240, 250):
        return INVALID_API_KEY
    if code in (302, 401, 402):
        return RATE_LIMIT
    msg = (message or "").upper()
    if "AK" in msg or "KEY" in msg:
        return INVALID_API_KEY
    if "LIMIT" in msg or "QUOTA" in msg:
        return RATE_LIMIT
    return UPSTREAM_ERROR


def classify_amap_around(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"status": "error", "reason_code": INVALID_RESPONSE, "reason": "non-object response", "items": []}
    if str(raw.get("status")) != "1":
        info = str(raw.get("info") or raw.get("infocode") or "amap_error")
        return {"status": "error", "reason_code": _amap_reason_code(info), "reason": info, "items": []}
    pois = raw.get("pois") or []
    if not isinstance(pois, list):
        pois = []
    items = [_compact_poi(p, "amap") for p in pois]
    return {"status": "ok" if items else "empty", "count": len(items), "items": items[:30]}


def classify_baidu_search(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"status": "error", "reason_code": INVALID_RESPONSE, "reason": "non-object response", "items": []}
    if raw.get("status") != 0:
        reason = str(raw.get("message") or raw.get("status"))
        return {
            "status": "error",
            "reason_code": _baidu_reason_code(raw.get("status"), reason),
            "reason": reason,
            "items": [],
        }
    results = raw.get("results") or []
    if not isinstance(results, list):
        results = []
    items = [_compact_poi(p, "baidu") for p in results]
    return {"status": "ok" if items else "empty", "count": len(items), "items": items[:30]}


def _compact_poi(item: dict[str, Any], source: str) -> dict[str, Any]:
    if source == "amap":
        return {
            "name": item.get("name"),
            "type": item.get("type"),
            "address": item.get("address"),
            "businessarea": item.get("businessarea"),
            "location": item.get("location"),
        }
    if source == "baidu":
        return {
            "name": item.get("name"),
            "type": item.get("tag") or item.get("type"),
            "address": item.get("address"),
            "uid": item.get("uid"),
            "location": item.get("location"),
        }
    return item


def _source_shell(source: str, **extra: Any) -> dict[str, Any]:
    out = {"source": source, "items": [], "places": [], "landuse": [], "amenities": [], "roads": [], "project_signals": [], "buildings": {"count": 0, "by_type": {}}}
    out.update(extra)
    return out


def has_admin_context(src: dict[str, Any] | None) -> bool:
    if not src:
        return False
    for place in src.get("places") or []:
        tag = str(place.get("tag") or "")
        name = str(place.get("name") or "").strip()
        if name and any(k in tag for k in ("formatted_address", "province", "city", "district", "township")):
            return True
    for item in src.get("items") or []:
        if str(item.get("address") or "").strip():
            return True
    return False


def project_signal_tokens() -> tuple[str, ...]:
    seen: list[str] = []
    for tok in PROJECT_KEYWORDS.split("|"):
        t = tok.strip()
        if t and t not in seen:
            seen.append(t)
    for extra in _PROJECT_SIGNAL_EXTRA:
        if extra not in seen:
            seen.append(extra)
    return tuple(seen)


def project_signal(p: dict[str, Any]) -> bool:
    s = " ".join(str(p.get(k) or "") for k in ("name", "type", "address", "businessarea", "description")).lower()
    return any(k.lower() in s for k in project_signal_tokens())


class GeoHTTPClient:
    def __init__(self) -> None:
        self.counts = {"amap": 0, "baidu": 0, "overpass": 0, "total": 0}
        # Overpass public endpoint rejects requests without a User-Agent (HTTP 406);
        # set a default UA so all channels (Overpass/AMap/Baidu) are accepted.
        self.client = httpx.Client(
            timeout=httpx.Timeout(HTTP_TIMEOUT),
            headers={"User-Agent": f"geo-region-inference/{SERVER_VERSION} (analysis)"},
        )

    def reset_counts(self) -> None:
        self.counts = {"amap": 0, "baidu": 0, "overpass": 0, "total": 0}

    def close(self) -> None:
        self.client.close()

    def request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        bucket = _host_bucket(url)
        self.counts[bucket] = self.counts.get(bucket, 0) + 1
        self.counts["total"] += 1
        t = timeout if timeout is not None else HTTP_TIMEOUT
        try:
            resp = self.client.request(method, url, content=data, headers=headers or {}, timeout=t)
            if resp.status_code == 429:
                raise httpx.HTTPStatusError("429", request=resp.request, response=resp)
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError(str(resp.status_code), request=resp.request, response=resp)
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException as e:
            raise TimeoutError(str(e)) from e
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 429:
                raise RateLimitError(str(e)) from e
            raise


class RateLimitError(Exception):
    pass


_HTTP: GeoHTTPClient | None = None


def get_http() -> GeoHTTPClient:
    global _HTTP
    if _HTTP is None:
        _HTTP = GeoHTTPClient()
    return _HTTP


def close_http() -> None:
    global _HTTP
    if _HTTP is not None:
        _HTTP.close()
        _HTTP = None


def _backoff_sleep(source: str, attempt: int, base_ms: int) -> None:
    delay_ms = base_ms * (2**attempt) + random.randint(0, base_ms // 2)
    LAST_RETRY_AFTER_MS[source] = delay_ms
    time.sleep(delay_ms / 1000.0)


def _is_rate_limited_result(result: dict[str, Any]) -> bool:
    return result.get("reason_code") == RATE_LIMIT


def run_amap_baidu_job_batches(
    jobs: list[tuple[int, float, float, float]],
    keywords: str | None,
    max_workers: int,
    *,
    query_amap_jobs: list[tuple[int, float, float, float]] | None = None,
    query_baidu_jobs: list[tuple[int, float, float, float]] | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Run jobs in batches with optional inter-batch delay."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    amap_by: dict[int, dict[str, Any]] = {}
    baidu_by: dict[int, dict[str, Any]] = {}
    split = query_amap_jobs is not None or query_baidu_jobs is not None
    amap_jobs = jobs if query_amap_jobs is None else query_amap_jobs
    baidu_jobs = jobs if query_baidu_jobs is None else query_baidu_jobs
    workers = min(max(max_workers, 1), 4)

    if not split:
        if not jobs:
            return amap_by, baidu_by
        batch_size = max(AMAP_BATCH_SIZE, 1) if AMAP_BATCH_SIZE > 0 else len(jobs)

        def one(job: tuple[int, float, float, float]):
            idx, lat, lon, radius = job
            return idx, query_amap(lat, lon, radius, keywords), query_baidu(lat, lon, radius, keywords)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, len(jobs), batch_size):
                chunk = jobs[start : start + batch_size]
                futs = [pool.submit(one, job) for job in chunk]
                for fut in as_completed(futs):
                    idx, amap, baidu = fut.result()
                    amap_by[idx] = amap
                    baidu_by[idx] = baidu
                if start + batch_size < len(jobs) and AMAP_BATCH_DELAY_MS > 0:
                    time.sleep(AMAP_BATCH_DELAY_MS / 1000.0)
        return amap_by, baidu_by

    tasks: list[tuple[str, tuple[int, float, float, float]]] = (
        [("amap", j) for j in amap_jobs] + [("baidu", j) for j in baidu_jobs]
    )
    if not tasks:
        return amap_by, baidu_by
    batch_size = max(AMAP_BATCH_SIZE, 1) if AMAP_BATCH_SIZE > 0 else len(tasks)

    def one_split(kind: str, job: tuple[int, float, float, float]):
        idx, lat, lon, radius = job
        if kind == "amap":
            return kind, idx, query_amap(lat, lon, radius, keywords)
        return kind, idx, query_baidu(lat, lon, radius, keywords)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(tasks), batch_size):
            chunk = tasks[start : start + batch_size]
            futs = [pool.submit(one_split, kind, job) for kind, job in chunk]
            for fut in as_completed(futs):
                kind, idx, rec = fut.result()
                if kind == "amap":
                    amap_by[idx] = rec
                else:
                    baidu_by[idx] = rec
            if start + batch_size < len(tasks) and AMAP_BATCH_DELAY_MS > 0:
                time.sleep(AMAP_BATCH_DELAY_MS / 1000.0)
    return amap_by, baidu_by


def _http_fail(source: str, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, TimeoutError):
        code, reason = TIMEOUT, str(exc)
    elif isinstance(exc, RateLimitError):
        code, reason = RATE_LIMIT, str(exc)
    elif isinstance(exc, json.JSONDecodeError):
        code, reason = INVALID_RESPONSE, str(exc)
    else:
        code, reason = HTTP_ERROR, str(exc)
    return _source_shell(source, status="error", reason_code=code, reason=reason)


def query_amap(lat: float, lon: float, radius: float, keywords: str | None = None) -> dict[str, Any]:
    key = os.environ.get("AMAP_KEY")
    if not key:
        return _source_shell("amap", status="unavailable", reason_code=NO_API_KEY, reason="AMAP_KEY not configured")
    gcj_lon, gcj_lat = wgs84_to_gcj02(lon, lat)
    params = {
        "key": key,
        "location": f"{gcj_lon},{gcj_lat}",
        "radius": str(int(radius)),
        "extensions": "all",
        "output": "JSON",
        "offset": "50",
        "page": "1",
    }
    if keywords:
        params["keywords"] = keywords
    url = "https://restapi.amap.com/v3/place/around?" + urllib.parse.urlencode(params)
    last: dict[str, Any] | None = None
    for attempt in range(AMAP_RETRY_MAX + 1):
        _AMAP_BUCKET.acquire()
        try:
            raw = get_http().request_json(url)
        except (TimeoutError, RateLimitError, httpx.HTTPError, json.JSONDecodeError) as e:
            last = _http_fail("amap", e)
            if last.get("reason_code") == RATE_LIMIT and attempt < AMAP_RETRY_MAX:
                _backoff_sleep("amap", attempt, AMAP_RETRY_BASE_MS)
                continue
            return last
        classified = classify_amap_around(raw)
        last = _source_shell("amap", **classified)
        if _is_rate_limited_result(last) and attempt < AMAP_RETRY_MAX:
            _backoff_sleep("amap", attempt, AMAP_RETRY_BASE_MS)
            continue
        return last
    return last or _source_shell("amap", status="error", reason_code=UPSTREAM_ERROR, reason="amap failed")


def query_baidu(lat: float, lon: float, radius: float, keywords: str | None = None) -> dict[str, Any]:
    ak = os.environ.get("BAIDU_AK")
    if not ak:
        return _source_shell("baidu", status="unavailable", reason_code=NO_API_KEY, reason="BAIDU_AK not configured")
    bd_lon, bd_lat = wgs84_to_bd09(lon, lat)
    params = {
        "ak": ak,
        "location": f"{bd_lat},{bd_lon}",
        "radius": str(int(radius)),
        "output": "json",
        "page_size": "50",
        "page_num": "0",
        "query": keywords or BAIDU_DEFAULT_QUERY,
    }
    url = "https://api.map.baidu.com/place/v2/search?" + urllib.parse.urlencode(params)
    last: dict[str, Any] | None = None
    for attempt in range(BAIDU_RETRY_MAX + 1):
        _BAIDU_BUCKET.acquire()
        try:
            raw = get_http().request_json(url)
        except (TimeoutError, RateLimitError, httpx.HTTPError, json.JSONDecodeError) as e:
            last = _http_fail("baidu", e)
            if last.get("reason_code") == RATE_LIMIT and attempt < BAIDU_RETRY_MAX:
                _backoff_sleep("baidu", attempt, BAIDU_RETRY_BASE_MS)
                continue
            return last
        classified = classify_baidu_search(raw)
        last = _source_shell("baidu", **classified)
        if _is_rate_limited_result(last) and attempt < BAIDU_RETRY_MAX:
            _backoff_sleep("baidu", attempt, BAIDU_RETRY_BASE_MS)
            continue
        return last
    return last or _source_shell("baidu", status="error", reason_code=UPSTREAM_ERROR, reason="baidu failed")


def probe_amap_burst(lat: float, lon: float, radius: float = 200.0) -> dict[str, Any]:
    """Incremental concurrent burst probe for CUQPS; stops on first rate limit."""
    key = os.environ.get("AMAP_KEY")
    if not key:
        return {"key_valid": False, "reason_code": NO_API_KEY, "concurrent_limit_detected": False}
    from concurrent.futures import ThreadPoolExecutor, as_completed

    deadline = time.monotonic() + BURST_PROBE_TIMEOUT_MS / 1000.0
    rate_limit_at: int | None = None
    max_c = max(1, min(BURST_PROBE_MAX_CONCURRENCY, 8))
    for concurrency in range(1, max_c + 1):
        if time.monotonic() >= deadline:
            break
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = [pool.submit(query_amap, lat, lon, radius, None) for _ in range(concurrency)]
            results = []
            for fut in as_completed(futs):
                results.append(fut.result())
            if any(_is_rate_limited_result(r) for r in results):
                rate_limit_at = concurrency
                break
    out: dict[str, Any] = {
        "key_valid": True,
        "concurrent_limit_detected": rate_limit_at is not None,
    }
    if rate_limit_at is not None:
        out["rate_limit_at_concurrency"] = rate_limit_at
        out["estimated_concurrent_limit"] = max(rate_limit_at - 1, 1)
        out["suggested_amap_qps_limit"] = out["estimated_concurrent_limit"]
        out["hint"] = (
            f"第 {rate_limit_at} 路并发触发 CUQPS；建议 AMAP_QPS_LIMIT≤{out['suggested_amap_qps_limit']} "
            "（并发上限估算，非官方配额）"
        )
    else:
        out["hint"] = "burst 探测未触发 CUQPS（不保证大批量 analyze_regions 无限流）"
    return out


def probe_api_status(
    lat: float,
    lon: float,
    *,
    probe_mode: str = "single",
) -> dict[str, Any]:
    amap_single = query_amap(lat, lon, 200.0, None)
    baidu_single = query_baidu(lat, lon, 200.0, None)
    result: dict[str, Any] = {
        "probe_mode": probe_mode,
        "center": {"lat": lat, "lon": lon},
        "disclaimer": "estimated_* 为客户端探测估算，请以高德/百度控制台为准",
        "amap": {
            "key_valid": amap_single.get("reason_code") != NO_API_KEY,
            "status": amap_single.get("status"),
            "reason_code": amap_single.get("reason_code"),
        },
        "baidu": {
            "key_valid": baidu_single.get("reason_code") != NO_API_KEY,
            "status": baidu_single.get("status"),
            "reason_code": baidu_single.get("reason_code"),
        },
    }
    if probe_mode == "burst":
        result["amap"].update(probe_amap_burst(lat, lon))
        result["disclaimer"] = (
            "single 模式不能预测并发限流；burst 为 CUQPS 估算。"
            + result["disclaimer"]
        )
    else:
        result["disclaimer"] = (
            "single（默认）仅验活 Key，不能预测批量 analyze_regions 的并发限流。"
            "如需诊断 CUQPS，请 probe_mode=burst。"
            + result["disclaimer"]
        )
    return result


def _summarize_overpass_elements(elements: list[dict[str, Any]]) -> dict[str, Any]:
    landuse, amenities, roads, places = [], [], [], []
    building_types: dict[str, int] = {}
    project_signals: list[dict[str, Any]] = []
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name")
        if "landuse" in tags:
            landuse.append({"tag": tags["landuse"], "name": name})
        if "building" in tags:
            b = tags["building"]
            building_types[b] = building_types.get(b, 0) + 1
        if "amenity" in tags:
            amenities.append({"tag": f"amenity={tags['amenity']}", "name": name})
        if "shop" in tags:
            amenities.append({"tag": f"shop={tags['shop']}", "name": name})
        if "office" in tags:
            amenities.append({"tag": f"office={tags['office']}", "name": name})
        if "highway" in tags and name:
            roads.append({"name": name, "highway": tags["highway"]})
        if "place" in tags:
            places.append({"tag": f"place={tags['place']}", "name": name})
        if tags.get("construction") or (
            name and any(w.lower() in name.lower() for w in project_signal_tokens())
        ):
            project_signals.append({"name": name, "construction": tags.get("construction"), "description": tags.get("description")})
    nonempty = bool(landuse or building_types or amenities or project_signals)
    return {
        "status": "ok" if nonempty else "empty",
        "landuse": landuse[:30],
        "buildings": {"count": sum(building_types.values()), "by_type": building_types},
        "amenities": amenities[:30],
        "roads": roads[:20],
        "places": places[:10],
        "project_signals": project_signals[:30],
    }


def _element_latlon(el: dict[str, Any]) -> tuple[float, float] | None:
    if "lat" in el and "lon" in el:
        return float(el["lat"]), float(el["lon"])
    center = el.get("center") or {}
    if "lat" in center and "lon" in center:
        return float(center["lat"]), float(center["lon"])
    return None


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    mx, my = deg_to_m_factors((lat1 + lat2) / 2.0)
    return math.hypot((lon1 - lon2) * mx, (lat1 - lat2) * my)


def _overpass_around_clause(lat: float, lon: float, radius: float) -> str:
    r = int(radius)
    return (
        f'way(around:{r},{lat},{lon})["landuse"];'
        f'relation(around:{r},{lat},{lon})["landuse"];'
        f'way(around:{r},{lat},{lon})["building"];'
        f'node(around:{r},{lat},{lon})["amenity"];'
        f'node(around:{r},{lat},{lon})["shop"];'
        f'node(around:{r},{lat},{lon})["office"];'
        f'way(around:{r},{lat},{lon})["highway"]["name"];'
        f'node(around:{r},{lat},{lon})["place"];'
        f'way(around:{r},{lat},{lon})["place"];'
    )


def _post_overpass(query: str) -> Any:
    body = ("data=" + urllib.parse.quote(query)).encode("utf-8")
    return get_http().request_json(
        OVERPASS_URL,
        method="POST",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=max(20.0, HTTP_TIMEOUT),
    )


def overpass_query(lat: float, lon: float, radius: float) -> dict[str, Any]:
    if not OSM_ENABLED:
        return _source_shell("osm", status="unavailable", reason_code=DISABLED, reason="OSM disabled via OSM_ENABLED")
    query = f"[out:json][timeout:20];({_overpass_around_clause(lat, lon, radius)});out center tags;"
    try:
        raw = _post_overpass(query)
    except (TimeoutError, RateLimitError, httpx.HTTPError, json.JSONDecodeError) as e:
        return _http_fail("osm", e)
    if not isinstance(raw, dict):
        return _source_shell("osm", status="error", reason_code=INVALID_RESPONSE, reason="non-object response")
    remark = str(raw.get("remark") or "")
    if "error" in remark.lower() or "timeout" in remark.lower():
        return _source_shell("osm", status="error", reason_code=TIMEOUT if "timeout" in remark.lower() else UPSTREAM_ERROR, reason=remark)
    summarized = _summarize_overpass_elements(list(raw.get("elements") or []))
    return _source_shell("osm", **summarized)


def overpass_query_batch(points: list[tuple[int, float, float, float]]) -> dict[int, dict[str, Any]]:
    """points: (index, lat, lon, radius). One HTTP call per OVERPASS_BATCH_SIZE points."""
    out: dict[int, dict[str, Any]] = {}
    if not points:
        return out
    if not OSM_ENABLED:
        for idx, *_ in points:
            out[idx] = _source_shell(
                "osm", status="unavailable", reason_code=DISABLED, reason="OSM disabled via OSM_ENABLED"
            )
        return out
    for start in range(0, len(points), OVERPASS_BATCH_SIZE):
        chunk = points[start : start + OVERPASS_BATCH_SIZE]
        body_parts = "".join(_overpass_around_clause(lat, lon, radius) for _, lat, lon, radius in chunk)
        query = f"[out:json][timeout:25];({body_parts});out center tags;"
        try:
            raw = _post_overpass(query)
        except (TimeoutError, RateLimitError, httpx.HTTPError, json.JSONDecodeError) as e:
            fail = _http_fail("osm", e)
            for idx, _, _, _ in chunk:
                out[idx] = dict(fail)
            continue
        if not isinstance(raw, dict):
            fail = _source_shell("osm", status="error", reason_code=INVALID_RESPONSE, reason="non-object response")
            for idx, _, _, _ in chunk:
                out[idx] = dict(fail)
            continue
        elements = list(raw.get("elements") or [])
        grouped: dict[int, list[dict[str, Any]]] = {idx: [] for idx, _, _, _ in chunk}
        for el in elements:
            pos = _element_latlon(el)
            if pos is None:
                continue
            elat, elon = pos
            best_idx = None
            best_d = None
            for idx, lat, lon, radius in chunk:
                d = _dist_m(lat, lon, elat, elon)
                if d <= float(radius) and (best_d is None or d < best_d):
                    best_d = d
                    best_idx = idx
            if best_idx is not None:
                grouped[best_idx].append(el)
        for idx, _, _, _ in chunk:
            summarized = _summarize_overpass_elements(grouped.get(idx, []))
            out[idx] = _source_shell("osm", **summarized)
    return out


def _amap_places_from_regeo(raw: dict[str, Any]) -> list[dict[str, Any]]:
    places = []
    rc = raw.get("regeocode") or {}
    formatted = rc.get("formatted_address")
    if isinstance(formatted, str) and formatted:
        places.append({"tag": "amap_formatted_address", "name": formatted})
    addr_comp = rc.get("addressComponent") or {}
    for level in ("province", "city", "district", "township"):
        val = addr_comp.get(level)
        if isinstance(val, str) and val:
            places.append({"tag": f"amap_{level}", "name": val})
    return places[:10]


def _baidu_places_from_regeo(raw: dict[str, Any]) -> list[dict[str, Any]]:
    places = []
    result = raw.get("result") or {}
    formatted = result.get("formatted_address")
    if isinstance(formatted, str) and formatted:
        places.append({"tag": "baidu_formatted_address", "name": formatted})
    ac = result.get("addressComponent") or {}
    for level in ("province", "city", "district"):
        val = ac.get(level)
        if isinstance(val, str) and val:
            places.append({"tag": f"baidu_{level}", "name": val})
    return places[:10]


def _regeo_coord_key(provider: str, lat: float, lon: float) -> tuple[str, float, float]:
    return (provider, round(float(lat), 6), round(float(lon), 6))


def _merge_regeo_places(src: dict[str, Any], extra: list[dict[str, Any]]) -> dict[str, Any]:
    if not extra:
        return src
    out = dict(src)
    out["places"] = (list(src.get("places") or []) + extra)[:10]
    return out


def _amap_regeo_payloads(raw: dict[str, Any]) -> list[dict[str, Any]]:
    items = raw.get("regeocodes")
    if items is None:
        items = raw.get("regeocode")
    if isinstance(items, dict):
        return [items]
    if isinstance(items, list):
        return [x for x in items if isinstance(x, dict)]
    return []


def _fetch_amap_regeo_places_single(lat: float, lon: float) -> list[dict[str, Any]]:
    key = os.environ.get("AMAP_KEY")
    if not key:
        return []
    gcj_lon, gcj_lat = wgs84_to_gcj02(lon, lat)
    params = {"key": key, "location": f"{gcj_lon},{gcj_lat}", "radius": "1000", "extensions": "all", "output": "json"}
    url = "https://restapi.amap.com/v3/geocode/regeo?" + urllib.parse.urlencode(params)
    _AMAP_BUCKET.acquire()
    try:
        raw = get_http().request_json(url)
    except (TimeoutError, RateLimitError, httpx.HTTPError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict) or str(raw.get("status")) != "1":
        return []
    payloads = _amap_regeo_payloads(raw)
    if not payloads:
        return []
    return _amap_places_from_regeo({"regeocode": payloads[0]})


def _fetch_amap_regeo_places_batch(coords: list[tuple[float, float]]) -> list[list[dict[str, Any]]]:
    empty = [[] for _ in coords]
    if not coords:
        return empty
    key = os.environ.get("AMAP_KEY")
    if not key:
        return empty
    locations = []
    for lat, lon in coords:
        gcj_lon, gcj_lat = wgs84_to_gcj02(lon, lat)
        locations.append(f"{gcj_lon},{gcj_lat}")
    params = {
        "key": key,
        "batch": "true",
        "radius": "1000",
        "extensions": "all",
        "output": "json",
    }
    qs = urllib.parse.urlencode(params)
    url = f"https://restapi.amap.com/v3/geocode/regeo?{qs}&location={'|'.join(locations)}"
    _AMAP_BUCKET.acquire()
    try:
        raw = get_http().request_json(url)
    except (TimeoutError, RateLimitError, httpx.HTTPError, json.JSONDecodeError):
        return empty
    if not isinstance(raw, dict) or str(raw.get("status")) != "1":
        return empty
    payloads = _amap_regeo_payloads(raw)
    out: list[list[dict[str, Any]]] = []
    for i in range(len(coords)):
        if i < len(payloads):
            out.append(_amap_places_from_regeo({"regeocode": payloads[i]}))
        else:
            out.append([])
    return out


def _fetch_baidu_regeo_places(lat: float, lon: float) -> list[dict[str, Any]]:
    ak = os.environ.get("BAIDU_AK")
    if not ak:
        return []
    bd_lon, bd_lat = wgs84_to_bd09(lon, lat)
    params = {"ak": ak, "output": "json", "coordtype": "bd09ll", "location": f"{bd_lat:.6f},{bd_lon:.6f}"}
    url = "https://api.map.baidu.com/reverse_geocoding/v3/?" + urllib.parse.urlencode(params)
    _BAIDU_BUCKET.acquire()
    try:
        raw = get_http().request_json(url)
    except (TimeoutError, RateLimitError, httpx.HTTPError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict) or raw.get("status") != 0:
        return []
    return _baidu_places_from_regeo(raw)


def maybe_regeo_amap(src: dict[str, Any], lat: float, lon: float) -> dict[str, Any]:
    if src.get("status") in ("unavailable", "error") or has_admin_context(src):
        return src
    if not os.environ.get("AMAP_KEY"):
        return src
    return _merge_regeo_places(src, _fetch_amap_regeo_places_single(lat, lon))


def maybe_regeo_baidu(src: dict[str, Any], lat: float, lon: float) -> dict[str, Any]:
    if src.get("status") in ("unavailable", "error") or has_admin_context(src):
        return src
    if not os.environ.get("BAIDU_AK"):
        return src
    return _merge_regeo_places(src, _fetch_baidu_regeo_places(lat, lon))


def apply_regeo_for_jobs(
    jobs: list[tuple[int, float, float, float]],
    amap_by: dict[int, dict[str, Any]],
    baidu_by: dict[int, dict[str, Any]],
    cache: dict[tuple[str, float, float], list[dict[str, Any]]],
) -> None:
    amap_groups: dict[tuple[str, float, float], list[int]] = {}
    amap_ll: dict[tuple[str, float, float], tuple[float, float]] = {}
    baidu_groups: dict[tuple[str, float, float], list[int]] = {}
    baidu_ll: dict[tuple[str, float, float], tuple[float, float]] = {}
    amap_key = os.environ.get("AMAP_KEY")
    baidu_key = os.environ.get("BAIDU_AK")
    for idx, lat, lon, _radius in jobs:
        src = amap_by.get(idx)
        if src is not None and src.get("status") not in ("unavailable", "error") and not has_admin_context(src) and amap_key:
            ck = _regeo_coord_key("amap", lat, lon)
            if ck in cache:
                amap_by[idx] = _merge_regeo_places(src, cache[ck])
            else:
                amap_groups.setdefault(ck, []).append(idx)
                amap_ll[ck] = (lat, lon)
        srcb = baidu_by.get(idx)
        if srcb is not None and srcb.get("status") not in ("unavailable", "error") and not has_admin_context(srcb) and baidu_key:
            ck = _regeo_coord_key("baidu", lat, lon)
            if ck in cache:
                baidu_by[idx] = _merge_regeo_places(srcb, cache[ck])
            else:
                baidu_groups.setdefault(ck, []).append(idx)
                baidu_ll[ck] = (lat, lon)

    keys = list(amap_groups.keys())
    for start in range(0, len(keys), AMAP_REGEO_BATCH_SIZE):
        chunk_keys = keys[start : start + AMAP_REGEO_BATCH_SIZE]
        coords = [amap_ll[ck] for ck in chunk_keys]
        extras = _fetch_amap_regeo_places_batch(coords)
        for ck, extra in zip(chunk_keys, extras):
            cache[ck] = extra
            for idx in amap_groups[ck]:
                amap_by[idx] = _merge_regeo_places(amap_by[idx], extra)

    for ck, idxs in baidu_groups.items():
        lat, lon = baidu_ll[ck]
        extra = _fetch_baidu_regeo_places(lat, lon)
        cache[ck] = extra
        for idx in idxs:
            baidu_by[idx] = _merge_regeo_places(baidu_by[idx], extra)


def merge_source_records(base: dict[str, Any], expanded: dict[str, Any] | None, base_r: float, expanded_r: float | None) -> dict[str, Any]:
    out = dict(base)
    out["radius_m"] = base_r
    out["expanded_radius_m"] = expanded_r
    if not expanded:
        return out
    seen: set[str] = set()
    items = []
    for item in list(base.get("items") or []) + list(expanded.get("items") or []):
        key = str(item.get("name") or item.get("uid") or item)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    out["items"] = items[:30]
    out["count"] = len(out["items"])
    signals = list(base.get("project_signals") or []) + list(expanded.get("project_signals") or [])
    out["project_signals"] = signals[:30]
    ranks = {"ok": 3, "empty": 2, "error": 1, "unavailable": 0}
    if ranks.get(expanded.get("status"), 0) > ranks.get(base.get("status"), 0):
        out["status"] = expanded.get("status")
        if expanded.get("reason_code"):
            out["reason_code"] = expanded.get("reason_code")
        if expanded.get("reason"):
            out["reason"] = expanded.get("reason")
    return out
