"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
-- 药品目录：以通用名、剂型、规格作为统一身份，商品名/别名经 medication_aliases 归并。
CREATE TABLE IF NOT EXISTS medications (
    medication_id TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    generic_name TEXT NOT NULL,
    dosage_form TEXT NOT NULL,
    strength TEXT NOT NULL,
    unit TEXT NOT NULL,
    storage_required TEXT NOT NULL,
    controlled_level INTEGER NOT NULL DEFAULT 0 CHECK(controlled_level BETWEEN 0 AND 3),
    indications_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, medication_id),
    UNIQUE (organization_id, generic_name, dosage_form, strength)
);
CREATE TABLE IF NOT EXISTS medication_aliases (
    organization_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    medication_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, alias)
);
CREATE TABLE IF NOT EXISTS medication_substitutes (
    organization_id TEXT NOT NULL,
    medication_id TEXT NOT NULL,
    substitute_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, medication_id, substitute_id)
);
CREATE TABLE IF NOT EXISTS site_storage_capabilities (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    storage_mode TEXT NOT NULL,
    PRIMARY KEY (site_id, storage_mode)
);
CREATE TABLE IF NOT EXISTS site_medication_policy (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    medication_id TEXT NOT NULL,
    emergency_reserve REAL NOT NULL DEFAULT 0 CHECK(emergency_reserve >= 0),
    reorder_point REAL NOT NULL DEFAULT 0 CHECK(reorder_point >= 0),
    next_resupply_date TEXT,
    PRIMARY KEY (site_id, medication_id)
);
CREATE TABLE IF NOT EXISTS prescriber_profiles (
    actor_id TEXT PRIMARY KEY REFERENCES actors(actor_id),
    controlled_level_max INTEGER NOT NULL DEFAULT 0 CHECK(controlled_level_max BETWEEN 0 AND 3)
);
CREATE TABLE IF NOT EXISTS patients (
    patient_id TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    display_name TEXT NOT NULL,
    allergies_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (site_id, patient_id)
);
CREATE TABLE IF NOT EXISTS medication_batches (
    batch_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    medication_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    batch_number TEXT NOT NULL,
    quantity_on_hand REAL NOT NULL CHECK(quantity_on_hand >= 0),
    unit TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    storage_required TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'quarantined', 'destroyed')),
    received_at TEXT NOT NULL,
    UNIQUE (site_id, medication_id, batch_number)
);
-- 连续库存账本：每笔批次级增减并携带发生后的批次余额。
CREATE TABLE IF NOT EXISTS inventory_ledger (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    medication_id TEXT NOT NULL,
    entry_type TEXT NOT NULL CHECK(entry_type IN (
        'intake', 'reserve', 'reserve_release', 'dispense', 'return',
        'loss', 'destroy', 'transfer_out', 'transfer_in')),
    delta_qty REAL NOT NULL,
    balance_after REAL NOT NULL CHECK(balance_after >= 0),
    held_after REAL NOT NULL DEFAULT 0 CHECK(held_after >= 0),
    ref_type TEXT,
    ref_id TEXT,
    reason TEXT,
    approval_id TEXT,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_batch ON inventory_ledger(batch_id, sequence);
CREATE INDEX IF NOT EXISTS idx_ledger_site_med ON inventory_ledger(site_id, medication_id, sequence);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    prescription_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    status TEXT NOT NULL CHECK(status IN ('held', 'consumed', 'released')),
    approvals_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reservations_held ON reservations(batch_id, status);
CREATE TABLE IF NOT EXISTS prescriptions (
    prescription_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    medication_id TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    indication TEXT NOT NULL,
    allow_substitute INTEGER NOT NULL DEFAULT 0 CHECK(allow_substitute IN (0, 1)),
    emergency_use INTEGER NOT NULL DEFAULT 0 CHECK(emergency_use IN (0, 1)),
    prescriber_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'recorded' CHECK(status IN ('recorded', 'reserved', 'dispensed')),
    decision_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prescription_amendments (
    amendment_id TEXT PRIMARY KEY,
    prescription_id TEXT NOT NULL REFERENCES prescriptions(prescription_id),
    note TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    amended_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 发放事实不可变；同一处方只能存在一条发放记录，重试不会再次扣减。
CREATE TABLE IF NOT EXISTS dispensations (
    dispensation_id TEXT PRIMARY KEY,
    prescription_id TEXT NOT NULL UNIQUE,
    organization_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    medication_id TEXT NOT NULL,
    requested_medication_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    emergency_used INTEGER NOT NULL,
    substituted INTEGER NOT NULL,
    near_expiry_substitute INTEGER NOT NULL,
    approval_id TEXT,
    prescriber_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispensation_lines (
    dispensation_id TEXT NOT NULL REFERENCES dispensations(dispensation_id),
    batch_id TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    PRIMARY KEY (dispensation_id, batch_id)
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    request_kind TEXT NOT NULL CHECK(request_kind IN (
        'emergency_reserve', 'near_expiry_substitute', 'cross_site_transfer')),
    reason TEXT NOT NULL,
    approver_id TEXT NOT NULL,
    subject_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_usages (
    approval_id TEXT PRIMARY KEY REFERENCES approvals(approval_id),
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    used_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    medication_id TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    from_site_id TEXT NOT NULL,
    to_site_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('in_transit', 'received')),
    approval_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    received_at TEXT
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
