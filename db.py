"""
Shared TiDB connection helper.

Preferred: set a single TIDB_CONNECTION_STRING env var, copied straight
from TiDB Cloud's "Connect" panel, e.g.:

    mysql://user.root:password@gateway01.xx-xxxxx-x.prod.aws.tidbcloud.com:4000/hoster?ssl-mode=VERIFY_IDENTITY

Fallback: if that's not set, individual variables are used instead
(useful for local testing or if you'd rather keep them separate):

    TIDB_HOST
    TIDB_PORT       (default 4000)
    TIDB_USER
    TIDB_PASSWORD
    TIDB_DATABASE
"""

import os
from urllib.parse import urlparse, unquote

import pymysql
from pymysql.cursors import DictCursor


def _connection_params():
    conn_str = os.environ.get("TIDB_CONNECTION_STRING")
    if conn_str:
        parsed = urlparse(conn_str)
        return dict(
            host=parsed.hostname,
            port=parsed.port or 4000,
            user=unquote(parsed.username) if parsed.username else None,
            password=unquote(parsed.password) if parsed.password else None,
            database=parsed.path.lstrip("/") or os.environ.get("TIDB_DATABASE", ""),
        )
    return dict(
        host=os.environ["TIDB_HOST"],
        port=int(os.environ.get("TIDB_PORT", "4000")),
        user=os.environ["TIDB_USER"],
        password=os.environ["TIDB_PASSWORD"],
        database=os.environ["TIDB_DATABASE"],
    )


def get_connection():
    params = _connection_params()
    return pymysql.connect(
        **params,
        ssl={"ssl": {}},          # TiDB Cloud requires TLS
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=10,
    )
