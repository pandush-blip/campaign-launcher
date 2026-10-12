"""Instantly API v2 client.

All requests pass the hard allowlist in :mod:`tri_launch.allowlist`. Mutating
campaign-scoped calls additionally require the campaign to be tracked in local state
(spec rule 2). The API key is never logged.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Iterable, Optional

import httpx

from . import state
from .allowlist import AllowlistError, GuardError, campaign_id_from_path, check_allowed

log = logging.getLogger("tri_launch.instantly")

RETRY_STATUSES = {429, 500, 502, 503, 504}

# Only addresses in this shape may be force-deleted after their campaign is gone.
PHASE0_EMAIL_RE = re.compile(r"^phase0\.[a-z0-9]+@example\.com$")


class InstantlyApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: Any):
        self.method, self.path, self.status, self.body = method, path, status, body
        super().__init__(f"Instantly API {method} {path} -> {status}: {body!r}"[:2000])


class NotFound(InstantlyApiError):
    pass


class InstantlyClient:
    BASE_URL = "https://api.instantly.ai"
    # Some networks/Cloudflare setups reject bare python user agents; use a neutral one.
    USER_AGENT = "tri-launch/0.1 (+https://launcher.therevenueinbox.com)"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 4,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": self.USER_AGENT,
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )
        self.max_retries = max_retries

    # -- plumbing ---------------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "InstantlyClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        rule = check_allowed(method, path)  # raises before anything is sent
        attempt = 0
        while True:
            started = time.monotonic()
            resp = self._client.request(method, path, json=json_body, params=params)
            ms = int((time.monotonic() - started) * 1000)
            log.info("%s %s -> %s (%d ms) [%s]", method, path, resp.status_code, ms, rule.note)
            if resp.status_code < 400:
                if not resp.content:
                    return None
                return resp.json()
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                delay = min(2.0**attempt, 30.0) + random.uniform(0, 0.5)
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, min(float(retry_after), 60.0))
                log.warning(
                    "%s %s -> %s, retrying in %.1fs (attempt %d/%d)",
                    method,
                    path,
                    resp.status_code,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(delay)
                attempt += 1
                continue
            body: Any
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:2000]
            if resp.status_code == 404:
                raise NotFound(method, path, resp.status_code, body)
            raise InstantlyApiError(method, path, resp.status_code, body)

    # -- campaigns --------------------------------------------------------------
    def list_campaigns(
        self, *, search: Optional[str] = None, limit: int = 100, status: Optional[int] = None
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        starting_after: Optional[str] = None
        while True:
            params: dict[str, Any] = {"limit": min(limit, 100)}
            if search:
                params["search"] = search
            if status is not None:
                params["status"] = status
            if starting_after:
                params["starting_after"] = starting_after
            page = self.request("GET", "/api/v2/campaigns", params=params)
            items.extend(page.get("items", []))
            starting_after = page.get("next_starting_after")
            if not starting_after or len(items) >= limit:
                return items[:limit]

    def find_campaign_by_name(self, name: str) -> list[dict[str, Any]]:
        """Exact-name (case-sensitive) matches; caller decides on 0 / 1 / many."""
        return [c for c in self.list_campaigns(search=name, limit=100) if c.get("name") == name]

    def resolve_campaign(self, ref: str) -> dict[str, Any]:
        """Resolve a campaign name or UUID to exactly one campaign object."""
        if re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            ref.strip(),
        ):
            return self.get_campaign(ref.strip())
        matches = self.find_campaign_by_name(ref)
        if not matches:
            raise LookupError(f"No campaign named {ref!r} found (exact match).")
        if len(matches) > 1:
            ids = ", ".join(c["id"] for c in matches)
            raise LookupError(f"{len(matches)} campaigns named {ref!r}: {ids}")
        return matches[0]

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v2/campaigns/{campaign_id}")

    def duplicate_campaign(self, source_id: str, name: str) -> dict[str, Any]:
        return self.request(
            "POST", f"/api/v2/campaigns/{source_id}/duplicate", json_body={"name": name}
        )

    def patch_campaign(self, campaign_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        state.require_tracked(campaign_id)
        return self.request("PATCH", f"/api/v2/campaigns/{campaign_id}", json_body=payload)

    def add_campaign_variables(
        self, campaign_id: str, names: Iterable[str]
    ) -> dict[str, Any]:
        state.require_tracked(campaign_id)
        return self.request(
            "POST",
            f"/api/v2/campaigns/{campaign_id}/variables",
            json_body={"variables": list(names)},
        )

    def delete_campaign(self, campaign_id: str, *, confirm_name: str) -> None:
        rec = state.require_tracked(campaign_id)
        if rec.get("name") != confirm_name:
            raise GuardError(
                f"Refusing to delete {campaign_id}: confirmation name mismatch "
                f"(tracked name {rec.get('name')!r} != {confirm_name!r})"
            )
        self.request("DELETE", f"/api/v2/campaigns/{campaign_id}")
        state.mark_deleted(campaign_id)

    def pause_campaign_safety(self, campaign_id: str, *, reason: str) -> Any:
        """The only pause path. Used when a tracked campaign is not draft."""
        if not reason.startswith("safety:"):
            raise GuardError("pause_campaign_safety requires a 'safety:' reason")
        state.require_tracked(campaign_id)
        return self.request("POST", f"/api/v2/campaigns/{campaign_id}/pause")

    def activate_campaign(self, campaign_id: str) -> Any:
        """Launch a campaign this tool created. The only path that starts sending.

        Guarded twice: the campaign must be tracked in local state, and the caller is the
        explicit confirmed launch flow (which verifies draft status by readback first).
        """
        state.require_tracked(campaign_id)
        log.warning("ACTIVATING campaign %s (explicit launch-all confirmed)", campaign_id)
        return self.request("POST", f"/api/v2/campaigns/{campaign_id}/activate")

    # -- accounts / leads -------------------------------------------------------
    def list_accounts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        page = self.request("GET", "/api/v2/accounts", params={"limit": min(limit, 100)})
        if isinstance(page, dict):
            return page.get("items", [])
        return page if isinstance(page, list) else []

    # -- blocklist --------------------------------------------------------------
    def list_blocklist(
        self, *, search: Optional[str] = None, limit: int = 100, domains_only: Optional[bool] = None
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        starting_after: Optional[str] = None
        while True:
            params: dict[str, Any] = {"limit": min(limit, 100)}
            if search:
                params["search"] = search
            if domains_only is not None:
                params["domains_only"] = domains_only
            if starting_after:
                params["starting_after"] = starting_after
            page = self.request("GET", "/api/v2/block-lists-entries", params=params)
            items.extend(page.get("items", []))
            starting_after = page.get("next_starting_after")
            if not starting_after or len(items) >= limit:
                return items[:limit]

    def count_blocklist(self) -> int:
        total = 0
        starting_after: Optional[str] = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if starting_after:
                params["starting_after"] = starting_after
            page = self.request("GET", "/api/v2/block-lists-entries", params=params)
            total += len(page.get("items", []))
            starting_after = page.get("next_starting_after")
            if not starting_after:
                return total

    def add_leads(
        self,
        campaign_id: str,
        leads: list[dict[str, Any]],
        *,
        skip_if_in_workspace: Optional[bool] = None,
        skip_if_in_campaign: Optional[bool] = None,
        skip_if_in_list: Optional[bool] = None,
        blocklist_id: Optional[str] = None,
        verify_leads_on_import: Optional[bool] = None,
    ) -> dict[str, Any]:
        state.require_tracked(campaign_id)
        if not (1 <= len(leads) <= 1000):
            raise ValueError("leads/add accepts 1..1000 leads per request")
        body: dict[str, Any] = {"campaign_id": campaign_id, "leads": leads}
        if skip_if_in_workspace is not None:
            body["skip_if_in_workspace"] = skip_if_in_workspace
        if skip_if_in_campaign is not None:
            body["skip_if_in_campaign"] = skip_if_in_campaign
        if skip_if_in_list is not None:
            body["skip_if_in_list"] = skip_if_in_list
        if blocklist_id is not None:
            body["blocklist_id"] = blocklist_id
        if verify_leads_on_import is not None:
            body["verify_leads_on_import"] = verify_leads_on_import
        resp = self.request("POST", "/api/v2/leads/add", json_body=body)
        ids = [lead.get("id") for lead in (resp.get("created_leads") or []) if lead.get("id")]
        state.add_lead_ids(campaign_id, ids)
        return resp

    def list_leads(
        self,
        *,
        campaign_id: Optional[str] = None,
        search: Optional[str] = None,
        ids: Optional[list[str]] = None,
        limit: int = 100,
        starting_after: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"limit": min(limit, 100)}
        if campaign_id:
            body["campaign"] = campaign_id
        if search:
            body["search"] = search
        if ids:
            body["ids"] = ids
        if starting_after:
            body["starting_after"] = starting_after
        return self.request("POST", "/api/v2/leads/list", json_body=body)

    def delete_leads(
        self,
        campaign_id: str,
        lead_ids: list[str],
        *,
        require_phase0_pattern: bool = False,
    ) -> Any:
        """Delete leads this tool created. Guarded to tracked campaigns and known leads."""
        ids = [i for i in (lead_ids or []) if i]
        if not ids:
            return None
        rec = state.known_campaign(campaign_id)
        if rec is None:
            raise GuardError(
                f"Refusing to delete leads: campaign {campaign_id} was not created by this tool."
            )
        allowed = set(rec.get("lead_ids") or [])
        unknown = [i for i in ids if i not in allowed]
        if unknown and not require_phase0_pattern:
            raise GuardError(
                f"Refusing to delete {len(unknown)} lead(s) not recorded as created by this "
                f"tool in campaign {campaign_id}."
            )
        if require_phase0_pattern:
            verified: list[str] = []
            for start in range(0, len(ids), 100):
                chunk = ids[start : start + 100]
                page = self.list_leads(ids=chunk, limit=len(chunk))
                got = {lead["id"]: lead for lead in page.get("items", [])}
                for i in chunk:
                    lead = got.get(i)
                    if lead is None:
                        log.info("Lead %s is already gone; skipping", i)
                        continue
                    if lead.get("campaign") != campaign_id or not PHASE0_EMAIL_RE.match(
                        lead.get("email") or ""
                    ):
                        raise GuardError(
                            f"Refusing to delete lead {i}: not a verified phase0 test lead "
                            f"in campaign {campaign_id}."
                        )
                    verified.append(i)
            ids = verified
            if not ids:
                return {"count": 0, "note": "already deleted"}
        log.info("Deleting %d test lead(s) from campaign %s", len(ids), campaign_id)
        if len(ids) <= 100:
            return self.request(
                "DELETE", "/api/v2/leads", json_body={"campaign_id": campaign_id, "ids": ids}
            )
        total = 0
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            resp = self.request(
                "DELETE", "/api/v2/leads", json_body={"campaign_id": campaign_id, "ids": chunk}
            )
            total += int((resp or {}).get("count") or 0)
        return {"count": total}

    def count_leads(
        self,
        *,
        campaign_id: Optional[str] = None,
        search: Optional[str] = None,
        cap: Optional[int] = None,
    ) -> int:
        total = 0
        starting_after: Optional[str] = None
        while True:
            page = self.list_leads(
                campaign_id=campaign_id, search=search, limit=100, starting_after=starting_after
            )
            total += len(page.get("items", []))
            if cap is not None and total >= cap:
                return total
            starting_after = page.get("next_starting_after")
            if not starting_after:
                return total

    def all_leads(self, *, campaign_id: Optional[str] = None, search: Optional[str] = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        starting_after: Optional[str] = None
        while True:
            page = self.list_leads(
                campaign_id=campaign_id, search=search, limit=100, starting_after=starting_after
            )
            out.extend(page.get("items", []))
            starting_after = page.get("next_starting_after")
            if not starting_after:
                return out
