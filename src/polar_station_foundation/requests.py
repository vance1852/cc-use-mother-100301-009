"""提供请求级幂等回执的共享实现。"""

from __future__ import annotations

import json
from typing import Any, Callable

from .audit import canonical_json, digest
from .errors import ConflictError
from .models import WriteReceipt


def idempotent(connection, *, now: str, identifier_validator: Callable[[str], str],
               request_id: str, action: str, payload: dict[str, Any],
               create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
    """执行一次带幂等保护的写入，重放返回原始回执。"""

    request_id = identifier_validator(request_id)
    payload_hash = digest(payload)
    row = connection.execute(
        "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
    ).fetchone()
    if row:
        if row["action"] != action or row["payload_hash"] != payload_hash:
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True,
                            json.loads(row["response_json"]))
    resource_type, resource_id, response = create()
    connection.execute(
        "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
        "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (request_id, action, payload_hash, resource_type, resource_id,
         canonical_json(response), now),
    )
    return WriteReceipt(request_id, resource_type, resource_id, False, response)
