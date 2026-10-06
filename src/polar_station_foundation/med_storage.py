"""药品与用药保障模块的 SQLite 表结构。

基础库只提供组织、操作者、场所、通用资料、幂等回执与审计链。
本模块在同一数据库内追加药品目录、批次、连续账本、医嘱、预留、
患者、调拨、修订等表，全部使用 IF NOT EXISTS，可在旧库上升级。
"""

from __future__ import annotations

MED_SCHEMA = """
CREATE TABLE IF NOT EXISTS med_meta (
    meta_key TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS med_catalog (
    medication_id TEXT PRIMARY KEY,
    generic_name TEXT NOT NULL,
    form TEXT NOT NULL,
    strength TEXT NOT NULL,
    route TEXT NOT NULL,
    storage_condition TEXT NOT NULL,
    controlled INTEGER NOT NULL CHECK(controlled IN (0,1)),
    prescription_only INTEGER NOT NULL CHECK(prescription_only IN (0,1)),
    indications_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1)
);
CREATE TABLE IF NOT EXISTS med_catalog_names (
    medication_id TEXT NOT NULL REFERENCES med_catalog(medication_id),
    name TEXT NOT NULL COLLATE NOCASE,
    name_kind TEXT NOT NULL CHECK(name_kind IN ('generic','trade','alias')),
    PRIMARY KEY(medication_id, name)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_med_names_unique ON med_catalog_names(name);
CREATE TABLE IF NOT EXISTS med_substitutes (
    medication_id TEXT NOT NULL REFERENCES med_catalog(medication_id),
    substitute_id TEXT NOT NULL REFERENCES med_catalog(medication_id),
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(medication_id, substitute_id),
    CHECK(medication_id <> substitute_id)
);
CREATE TABLE IF NOT EXISTS med_patients (
    patient_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    display_name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1)
);
CREATE TABLE IF NOT EXISTS med_patient_allergies (
    patient_id TEXT NOT NULL REFERENCES med_patients(patient_id),
    medication_id TEXT NOT NULL REFERENCES med_catalog(medication_id),
    severity TEXT NOT NULL CHECK(severity IN ('mild','moderate','severe')),
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(patient_id, medication_id)
);
CREATE TABLE IF NOT EXISTS med_batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    medication_id TEXT NOT NULL REFERENCES med_catalog(medication_id),
    lot TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    quantity_initial INTEGER NOT NULL CHECK(quantity_initial >= 0),
    quantity_on_hand INTEGER NOT NULL CHECK(quantity_on_hand >= 0),
    reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved >= 0),
    emergency_reserve INTEGER NOT NULL DEFAULT 0 CHECK(emergency_reserve >= 0),
    storage_condition TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available','quarantined','depleted','destroyed')),
    source_transfer_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, medication_id, lot)
);
CREATE INDEX IF NOT EXISTS idx_med_batches_pick ON med_batches(site_id, medication_id, status, expiry_date);
CREATE TABLE IF NOT EXISTS med_ledger_entries (
    entry_id TEXT NOT NULL UNIQUE,
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES med_batches(batch_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    movement_type TEXT NOT NULL CHECK(movement_type IN (
        'receive','reserve','release_reservation','issue','return','loss','destroy',
        'transfer_out','transfer_in')),
    quantity INTEGER NOT NULL,
    reserved_delta INTEGER NOT NULL DEFAULT 0,
    balance_after INTEGER NOT NULL CHECK(balance_after >= 0),
    reserved_after INTEGER NOT NULL CHECK(reserved_after >= 0),
    order_id TEXT,
    reservation_id TEXT,
    transfer_id TEXT,
    counterparty_site_id TEXT,
    reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_med_ledger_batch ON med_ledger_entries(batch_id, sequence);
CREATE TABLE IF NOT EXISTS med_orders (
    order_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    patient_id TEXT REFERENCES med_patients(patient_id),
    medication_id TEXT NOT NULL REFERENCES med_catalog(medication_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    indication TEXT NOT NULL DEFAULT '',
    is_emergency INTEGER NOT NULL CHECK(is_emergency IN (0,1)),
    allow_substitute INTEGER NOT NULL CHECK(allow_substitute IN (0,1)),
    allow_transfer INTEGER NOT NULL CHECK(allow_transfer IN (0,1)),
    status TEXT NOT NULL CHECK(status IN (
        'pending_review','awaiting_transfer','reserved','issued','rejected','cancelled')),
    justification TEXT NOT NULL DEFAULT '',
    prescriber_id TEXT NOT NULL,
    approver_id TEXT,
    approved_at TEXT,
    source_transfer_id TEXT,
    plan_json TEXT NOT NULL DEFAULT '',
    decision_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1)
);
CREATE INDEX IF NOT EXISTS idx_med_orders_site ON med_orders(site_id, created_at);
CREATE TABLE IF NOT EXISTS med_order_lines (
    line_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES med_orders(order_id),
    batch_id TEXT NOT NULL REFERENCES med_batches(batch_id),
    medication_id TEXT NOT NULL REFERENCES med_catalog(medication_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    is_substitute INTEGER NOT NULL CHECK(is_substitute IN (0,1)),
    is_emergency_stock INTEGER NOT NULL CHECK(is_emergency_stock IN (0,1)),
    is_transfer_stock INTEGER NOT NULL CHECK(is_transfer_stock IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_med_lines_order ON med_order_lines(order_id);
CREATE TABLE IF NOT EXISTS med_reservations (
    reservation_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES med_batches(batch_id),
    order_id TEXT NOT NULL REFERENCES med_orders(order_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    is_substitute INTEGER NOT NULL DEFAULT 0 CHECK(is_substitute IN (0,1)),
    is_emergency INTEGER NOT NULL DEFAULT 0 CHECK(is_emergency IN (0,1)),
    status TEXT NOT NULL CHECK(status IN ('held','consumed','released')),
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_med_reservations_order ON med_reservations(order_id, status);
CREATE TABLE IF NOT EXISTS med_amendments (
    amendment_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES med_orders(order_id),
    kind TEXT NOT NULL CHECK(kind IN ('patient_link','indication','note','cancel_note')),
    payload_json TEXT NOT NULL,
    warning_json TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_med_amendments_order ON med_amendments(order_id, created_at);
CREATE TABLE IF NOT EXISTS med_transfers (
    transfer_id TEXT PRIMARY KEY,
    from_site_id TEXT NOT NULL REFERENCES sites(site_id),
    to_site_id TEXT NOT NULL REFERENCES sites(site_id),
    medication_id TEXT NOT NULL REFERENCES med_catalog(medication_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    status TEXT NOT NULL CHECK(status IN ('proposed','approved','shipped','received','rejected','cancelled')),
    reason TEXT NOT NULL DEFAULT '',
    justification TEXT NOT NULL DEFAULT '',
    approver_id TEXT,
    approved_at TEXT,
    order_id TEXT REFERENCES med_orders(order_id),
    from_batch_id TEXT,
    to_batch_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(from_site_id <> to_site_id)
);
CREATE INDEX IF NOT EXISTS idx_med_transfers_status ON med_transfers(status, created_at);
"""


def ensure_med_schema(connection) -> None:
    """在给定连接上幂等创建药品模块表。"""

    connection.executescript(MED_SCHEMA)
