"""药品与用药保障模块的 HTTP/JSON 路由。

由基础 api.route 在未命中基础路由时委派；与基础接口一致，通过
X-Actor-Id 标识操作者，并复用同一数据库与时钟。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from .med_service import MedicationService


def med_route(domain_service, method: str, path: str, body: dict[str, Any],
              actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """返回 (状态码, 载荷)；无法识别时返回 None 交回基础路由。"""

    service = MedicationService(domain_service.database, domain_service.clock)
    parsed = urlparse(path)
    p = parsed.path
    query = parse_qs(parsed.query)

    def one(key: str, default: str | None = None) -> str | None:
        return query.get(key, [default])[0]

    if method == "POST" and p == "/medications":
        receipt = service.register_medication(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and p == "/medications/names":
        receipt = service.add_medication_name(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and p == "/medications/substitutes":
        receipt = service.add_substitute(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "GET" and p == "/medications/lookup":
        return 200, service.medication(actor_id, one("medication", ""))
    if method == "POST" and p == "/patients":
        receipt = service.register_patient(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and p == "/patients/allergies":
        receipt = service.add_allergy(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and p == "/med-batches":
        receipt = service.receive_batch(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and p == "/med-losses":
        receipt = service.record_loss(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and p == "/med-destructions":
        receipt = service.destroy_batch(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and p == "/med-requests/evaluate":
        return 200, service.evaluate_request(actor_id=actor_id, **body)
    if method == "POST" and p == "/med-orders":
        receipt = service.create_order(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and p == "/med-orders/approve":
        receipt = service.approve_order(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 200, receipt
    if method == "POST" and p == "/med-orders/issue":
        receipt = service.issue_order(actor_id=actor_id, **body)
        return 200, receipt
    if method == "POST" and p == "/med-orders/return":
        receipt = service.return_medication(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and p == "/med-orders/reject":
        receipt = service.reject_order(actor_id=actor_id, **body)
        return 200, receipt
    if method == "POST" and p == "/med-orders/cancel":
        receipt = service.cancel_order(actor_id=actor_id, **body)
        return 200, receipt
    if method == "POST" and p == "/med-orders/amend":
        receipt = service.amend_order(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "GET" and p.startswith("/med-orders/"):
        order_id = p.rsplit("/", 1)[1]
        return 200, service.get_order(actor_id=actor_id, order_id=order_id)
    if method == "POST" and p == "/med-transfers/ship":
        receipt = service.ship_transfer(actor_id=actor_id, **body)
        return 200, receipt
    if method == "POST" and p == "/med-transfers/receive":
        receipt = service.receive_transfer(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "GET" and p == "/med-supply":
        return 200, service.supply_overview(actor_id, one("site_id", ""))
    if method == "GET" and p == "/med-expiry-report":
        within = int(one("within_days", "30"))
        return 200, service.expiry_and_shortage_report(actor_id, one("site_id", ""), within)
    if method == "GET" and p == "/med-reconcile":
        return 200, service.reconcile_site(actor_id, one("site_id", ""))
    if method == "GET" and p.startswith("/med-batches/") and p.endswith("/trace"):
        batch_id = p.split("/")[2]
        return 200, service.batch_trace(actor_id=actor_id, batch_id=batch_id)
    if method == "GET" and p.startswith("/med-batches/") and p.endswith("/reconcile"):
        batch_id = p.split("/")[2]
        return 200, service.reconcile_batch(actor_id, batch_id)
    if method == "GET" and p == "/med-audit-events":
        after = int(one("after_sequence", "0"))
        return 200, service.redacted_audit_events(actor_id=actor_id, after_sequence=after)
    return None
