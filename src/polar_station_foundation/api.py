"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .pharmacy import PharmacyService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。

    service 为 PharmacyService 时同时提供药品保障路由；为 DomainService 时
    仅提供基础路由。
    """

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    pharmacy = service if isinstance(service, PharmacyService) else None
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if pharmacy is not None:
            pharmacy_response = _route_pharmacy(pharmacy, method, parsed.path, body,
                                                parsed.query, actor_id)
            if pharmacy_response is not None:
                status, payload, created = pharmacy_response
                return status, payload
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _route_pharmacy(pharmacy: PharmacyService, method: str, path: str, body: dict[str, Any],
                    query_string: str, actor_id: str):
    """返回 (status, payload, created)；未命中路由返回 None。"""

    query = parse_qs(query_string)

    def call(fn, created: bool = True):
        result = fn(actor_id=actor_id, **body)
        replayed = bool(result.get("replayed", False))
        return (200 if replayed else (201 if created else 200)), result, created

    posts = {
        "/medications": lambda: call(pharmacy.register_medication),
        "/medication-aliases": lambda: call(pharmacy.register_alias),
        "/medication-substitutes": lambda: call(pharmacy.register_substitute),
        "/site-storage": lambda: call(pharmacy.configure_site_storage),
        "/medication-policies": lambda: call(pharmacy.configure_medication_policy),
        "/prescriber-profiles": lambda: call(pharmacy.set_prescriber_profile),
        "/patients": lambda: call(pharmacy.register_patient),
        "/approvals": lambda: call(pharmacy.grant_approval),
        "/batches": lambda: call(pharmacy.intake_batch),
        "/prescriptions": lambda: call(pharmacy.record_prescription),
        "/prescription-amendments": lambda: call(pharmacy.amend_prescription),
        "/reservations": lambda: call(pharmacy.reserve_prescription),
        "/reservation-releases": lambda: call(pharmacy.release_reservation),
        "/dispensations": lambda: call(pharmacy.dispense_prescription),
        "/returns": lambda: call(pharmacy.return_dispensed),
        "/losses": lambda: call(pharmacy.record_loss),
        "/destructions": lambda: call(pharmacy.destroy_batch),
        "/transfers": lambda: call(pharmacy.transfer_batch),
        "/transfer-receptions": lambda: call(pharmacy.receive_transfer),
    }
    if method == "POST" and path in posts:
        return posts[path]()
    if method == "GET":
        if path == "/site-inventory":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, pharmacy.site_inventory(site_id), False
        if path == "/shortage-risks":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, pharmacy.shortage_and_expiry_risks(site_id), False
        if path == "/prescription-decision":
            return 200, pharmacy.evaluate_prescription(query.get("prescription_id", [""])[0]), False
        if path == "/prescription-amendments":
            return 200, {"items": pharmacy.list_amendments(query.get("prescription_id", [""])[0])}, False
        if path == "/batch-destinations":
            batch_id = query.get("batch_id", [""])[0]
            return 200, pharmacy.batch_destinations(actor_id=actor_id, batch_id=batch_id), False
        if path == "/pharmacy-audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, pharmacy.redacted_audit_events(actor_id=actor_id, after_sequence=after), False
        if path == "/ledger-verification":
            site_id = query.get("site_id", [None])[0]
            return 200, pharmacy.verify_ledger(site_id), False
    return None


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动极地科考站协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = PharmacyService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
