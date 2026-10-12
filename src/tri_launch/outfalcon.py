"""OutFalcon (SendingOS) API client.

Base `https://send.outfalcon.com`, auth `x-api-key: mk_live_...`. Every success is
`{"data": ..., "meta": {...}}`; every failure `{"error": {message, code, field}}`.
Rate limit is 100 requests/minute per key, so this client backs off on 429/5xx and
supports the `Idempotency-Key` header on mutations.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from typing import Any, Optional

import httpx

log = logging.getLogger("tri_launch.outfalcon")

RETRY_STATUSES = {429, 500, 502, 503, 504}


class OutFalconApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: Any):
        self.method, self.path, self.status, self.body = method, path, status, body
        super().__init__(f"OutFalcon API {method} {path} -> {status}: {body!r}"[:2000])


class OutFalconNotFound(OutFalconApiError):
    pass


class OutFalconClient:
    BASE_URL = "https://send.outfalcon.com"
    USER_AGENT = "tri-launch/0.1 (+https://launcher.therevenueinbox.com)"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 8,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": self.USER_AGENT,
            },
            timeout=timeout,
            transport=transport,
        )
        self.max_retries = max_retries

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OutFalconClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- plumbing ---------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[dict[str, Any]] = None,
        idempotent: bool = False,
        timeout: Optional[float] = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Return (data, meta). Mutations send an Idempotency-Key when idempotent=True."""
        headers: dict[str, str] = {}
        if idempotent and method.upper() in {"POST", "PUT", "PATCH"}:
            headers["Idempotency-Key"] = uuid.uuid4().hex
        attempt = 0
        while True:
            started = time.monotonic()
            extra = {"timeout": timeout} if timeout is not None else {}
            resp = self._client.request(
                method, path, json=json_body, params=params, headers=headers or None, **extra
            )
            ms = int((time.monotonic() - started) * 1000)
            log.info("%s %s -> %s (%d ms)", method, path, resp.status_code, ms)
            if resp.status_code < 400:
                payload = resp.json() if resp.content else {}
                if isinstance(payload, dict) and "data" in payload:
                    return payload.get("data"), (payload.get("meta") or {})
                return payload, {}
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                delay = min(2.0**attempt, 30.0) + random.uniform(0, 0.5)
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, min(float(retry_after), 65.0))
                log.warning(
                    "%s %s -> %s, retrying in %.1fs (attempt %d/%d)",
                    method, path, resp.status_code, delay, attempt + 1, self.max_retries,
                )
                time.sleep(delay)
                attempt += 1
                continue
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text[:2000]
            if resp.status_code == 404:
                raise OutFalconNotFound(method, path, resp.status_code, body)
            raise OutFalconApiError(method, path, resp.status_code, body)

    def _paginate(
        self, path: str, *, params: Optional[dict[str, Any]] = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Walk either pagination style (?page= or ?cursor=) until exhausted."""
        items: list[dict[str, Any]] = []
        query = dict(params or {})
        query["limit"] = min(limit, 100)
        cursor: Optional[str] = None
        page = 1
        while True:
            if cursor is not None:
                query["cursor"] = cursor
            else:
                query["page"] = page
            data, meta = self.request("GET", path, params=query)
            chunk = data if isinstance(data, list) else (data or {}).get("items") or []
            items.extend(chunk)
            if len(items) >= limit:
                return items[:limit]
            next_cursor = meta.get("next_cursor")
            if next_cursor:
                cursor = next_cursor
                continue
            if meta.get("has_more"):
                page += 1
                continue
            total_pages = meta.get("total_pages")
            if total_pages and page < int(total_pages):
                page += 1
                continue
            return items

    # -- campaigns --------------------------------------------------------------
    def list_campaigns(self, *, limit: int = 100, search: str = "") -> list[dict[str, Any]]:
        params = {"search": search} if search else None
        return self._paginate("/api/v1/campaigns", params=params, limit=limit)

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        data, _ = self.request("GET", f"/api/v1/campaigns/{campaign_id}")
        return data

    def find_campaign_by_name(self, name: str) -> list[dict[str, Any]]:
        return [c for c in self.list_campaigns(search=name, limit=100) if c.get("name") == name]

    def resolve_campaign(self, ref: str) -> dict[str, Any]:
        ref = (ref or "").strip()
        if ref and "-" in ref and len(ref) >= 32:
            return self.get_campaign(ref)
        matches = self.find_campaign_by_name(ref)
        if not matches:
            raise LookupError(f"No OutFalcon campaign named {ref!r} found (exact match).")
        if len(matches) > 1:
            raise LookupError(f"{len(matches)} OutFalcon campaigns named {ref!r}")
        return matches[0]

    def create_campaign(self, name: str) -> dict[str, Any]:
        data, _ = self.request(
            "POST", "/api/v1/campaigns", json_body={"name": name}, idempotent=True
        )
        return data

    def duplicate_campaign(
        self, campaign_id: str, name: str, *, include_accounts: bool = True
    ) -> dict[str, Any]:
        data, _ = self.request(
            "POST",
            f"/api/v1/campaigns/{campaign_id}/duplicate",
            json_body={
                "name": name,
                "include_leads": False,
                "include_accounts": include_accounts,
            },
            idempotent=True,
        )
        return data

    def update_campaign(self, campaign_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        data, _ = self.request("PATCH", f"/api/v1/campaigns/{campaign_id}", json_body=payload)
        return data

    def set_status(self, campaign_id: str, status: str) -> dict[str, Any]:
        data, _ = self.request(
            "PATCH", f"/api/v1/campaigns/{campaign_id}/status", json_body={"status": status}
        )
        return data

    def delete_campaign(self, campaign_id: str) -> None:
        self.request("DELETE", f"/api/v1/campaigns/{campaign_id}")

    # -- sequence (steps + variants) --------------------------------------------
    def get_sequences(self, campaign_id: str) -> dict[str, Any]:
        data, _ = self.request("GET", f"/api/v1/campaigns/{campaign_id}/sequences")
        return data if isinstance(data, dict) else {"steps": data}

    def add_step(self, campaign_id: str, *, delay_days: int = 1, delay_hours: int = 0) -> dict[str, Any]:
        data, _ = self.request(
            "POST",
            f"/api/v1/campaigns/{campaign_id}/steps",
            json_body={"delay_days": int(delay_days), "delay_hours": int(delay_hours)},
            idempotent=True,
        )
        return data

    def patch_step(self, step_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        data, _ = self.request("PATCH", f"/api/v1/campaigns/steps/{step_id}", json_body=payload)
        return data

    def delete_step(self, step_id: str) -> None:
        self.request("DELETE", f"/api/v1/campaigns/steps/{step_id}")

    def add_variant(self, step_id: str, label: str) -> dict[str, Any]:
        data, _ = self.request(
            "POST",
            f"/api/v1/campaigns/steps/{step_id}/variants",
            json_body={"label": label},
            idempotent=True,
        )
        return data

    def patch_variant(
        self,
        variant_id: str,
        *,
        subject: Optional[str] = None,
        body_html: Optional[str] = None,
        weight: Optional[int] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if subject is not None:
            payload["subject"] = subject
        if body_html is not None:
            payload["body_html"] = body_html
        if weight is not None:
            payload["weight"] = int(weight)
        data, _ = self.request(
            "PATCH", f"/api/v1/campaigns/variants/{variant_id}", json_body=payload
        )
        return data

    def delete_variant(self, variant_id: str) -> None:
        self.request("DELETE", f"/api/v1/campaigns/variants/{variant_id}")

    # -- accounts -----------------------------------------------------------------
    def list_accounts(self, *, limit: int = 100, only_active: bool = False) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if only_active:
            params["status"] = "active"
        return self._paginate("/api/v1/email-accounts", params=params, limit=limit)

    def count_accounts(self, *, only_active: bool = False) -> int:
        params: dict[str, Any] = {"limit": 1}
        if only_active:
            params["status"] = "active"
        _data, meta = self.request("GET", "/api/v1/email-accounts", params=params)
        total = meta.get("total")
        return int(total) if total is not None else len(self.list_accounts(limit=100))

    # -- leads ----------------------------------------------------------------------
    def create_lead_list(self, name: str, description: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        data, _ = self.request("POST", "/api/v1/leads/lists", json_body=body, idempotent=True)
        return data

    def find_lead_list(self, name: str) -> dict[str, Any] | None:
        """Exact-name lookup (used to reuse the segment's list on a resume)."""
        data, _ = self.request(
            "GET", "/api/v1/leads/lists", params={"q": name, "limit": 100}
        )
        items = data if isinstance(data, list) else (data or {}).get("items") or []
        for item in items:
            if (item.get("name") or "").strip() == name.strip():
                return item
        return None

    def bulk_create_leads(self, leads: list[dict[str, Any]]) -> dict[str, Any]:
        """Synchronous upsert-by-email, up to 10,000 leads per call."""
        data, _ = self.request("POST", "/api/v1/leads/bulk", json_body={"leads": leads})
        return data if isinstance(data, dict) else {"raw": data}

    def add_leads_to_list(self, list_id: str, lead_ids: list[str]) -> Any:
        data, _ = self.request(
            "POST", f"/api/v1/leads/lists/{list_id}/leads", json_body={"lead_ids": lead_ids}
        )
        return data

    def import_csv(
        self,
        list_id: str,
        csv_text: str,
        mapping: dict[str, str],
        *,
        async_job: bool = False,
    ) -> dict[str, Any]:
        """Import CSV text into a list with an explicit column mapping.

        Up to 5,000 rows runs inline (returns CsvImportResult); above that, or with
        async_job=True, the API answers 202 and the caller polls the job.
        """
        data, meta = self.request(
            "POST",
            f"/api/v1/leads/lists/{list_id}/upload",
            json_body={"csv_text": csv_text, "mapping": mapping, "async": bool(async_job)},
            timeout=600,
        )
        if isinstance(data, dict) and data.get("job_id"):
            return {"job_id": data["job_id"], "async": True}
        return data if isinstance(data, dict) else {"raw": data}

    def get_job(self, job_id: str) -> dict[str, Any]:
        data, _ = self.request("GET", f"/api/v1/jobs/{job_id}")
        return data

    def wait_for_job(self, job_id: str, *, timeout: float = 600.0, interval: float = 3.0) -> dict[str, Any]:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            job = self.get_job(job_id)
            status = str(job.get("status") or "")
            if status in {"completed", "failed"}:
                return job
            time.sleep(interval)
        raise TimeoutError(f"OutFalcon job {job_id} did not finish within {timeout}s")

    def enroll_list(self, campaign_id: str, list_id: str) -> dict[str, Any]:
        # Enrolling a multi-thousand-lead list is a heavy single call; give it room and
        # retry a read timeout once (already-enrolled leads are skipped server-side).
        for attempt in range(3):
            try:
                data, _ = self.request(
                    "POST",
                    f"/api/v1/campaigns/{campaign_id}/leads",
                    json_body={"list_id": list_id},
                    idempotent=True,
                    timeout=900,
                )
                return data if isinstance(data, dict) else {"raw": data}
            except httpx.ReadTimeout:
                if attempt == 2:
                    raise
                time.sleep(10)
        return {}

    def list_campaign_leads(
        self, campaign_id: str, *, limit: int = 100, status: str = ""
    ) -> list[dict[str, Any]]:
        params = {"status": status} if status else None
        return self._paginate(
            f"/api/v1/campaigns/{campaign_id}/leads", params=params, limit=limit
        )

    def count_campaign_leads(self, campaign_id: str) -> int:
        return len(self.list_campaign_leads(campaign_id, limit=100_000))

    def delete_leads(self, lead_ids: list[str]) -> Any:
        data, _ = self.request(
            "POST", "/api/v1/leads/bulk-delete", json_body={"ids": lead_ids}, idempotent=True
        )
        return data

    def delete_lead_list(self, list_id: str) -> None:
        self.request("DELETE", f"/api/v1/leads/lists/{list_id}")

    # -- preview (read-only render used for verification) -------------------------
    def preview(
        self,
        campaign_id: str,
        *,
        step_order: int = 1,
        variant_label: str = "A",
        subject: Optional[str] = None,
        body_html: Optional[str] = None,
        lead_id: Optional[str] = None,
        sample: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "step_order": int(step_order),
            "variant_label": variant_label,
            "sample": sample,
        }
        if subject is not None:
            body["subject"] = subject
        if body_html is not None:
            body["body_html"] = body_html
        if lead_id:
            body["lead_id"] = lead_id
        data, _ = self.request(
            "POST", f"/api/v1/campaigns/{campaign_id}/preview", json_body=body
        )
        return data
