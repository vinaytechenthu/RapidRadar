"""
db_scan — multi-engine database performance scanner for RapidRadar.

Supported engines: Microsoft SQL Server, PostgreSQL, Oracle, MySQL / MariaDB.

    from db_scan import ConnectionConfig, create_scanner
    scanner = create_scanner(ConnectionConfig(db_type="postgresql", server="db01",
                                              database="app", username="u", password="p"))
    results = scanner.run_full_scan()
"""

from __future__ import annotations

import importlib.util

from .base import DB_TYPES, BaseDBScanner, ConnectionConfig, db_type_label

__all__ = ["DB_TYPES", "ConnectionConfig", "BaseDBScanner", "create_scanner",
           "db_type_label", "driver_status"]

# Python modules that satisfy each engine (any one is enough)
_DRIVER_MODULES = {
    "sqlserver":  ["pyodbc"],
    "postgresql": ["psycopg2", "psycopg"],
    "oracle":     ["oracledb"],
    "mysql":      ["pymysql"],
}


def create_scanner(cfg: ConnectionConfig) -> BaseDBScanner:
    if cfg.db_type == "sqlserver":
        from .sqlserver import SQLServerScanner
        return SQLServerScanner(cfg)
    if cfg.db_type == "postgresql":
        from .postgres import PostgresScanner
        return PostgresScanner(cfg)
    if cfg.db_type == "oracle":
        from .oracle import OracleScanner
        return OracleScanner(cfg)
    if cfg.db_type == "mysql":
        from .mysql import MySQLScanner
        return MySQLScanner(cfg)
    raise ValueError(f"Unsupported database type: {cfg.db_type}")


def driver_status() -> dict:
    """Return {db_type: {label, port, installed, package}} for the UI."""
    out = {}
    for key, meta in DB_TYPES.items():
        installed = any(importlib.util.find_spec(m) is not None for m in _DRIVER_MODULES[key])
        out[key] = {"label": meta["label"], "port": meta["port"],
                    "installed": installed, "package": meta["driver_pkg"]}
    return out
