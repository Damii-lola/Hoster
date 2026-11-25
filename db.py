"""
Shared TiDB connection helper.

Reads connection details from environment variables so the same code
works locally, in the Render API service, and inside GitHub Actions:

    TIDB_HOST
    TIDB_PORT       (default 4000)
    TIDB_USER
    TIDB_PASSWORD
    TIDB_DATABASE
"""

import os
import pymysql
from pymysql.cursors import DictCursor


def get_connection():
    return pymysql.connect(
        host=os.environ["TIDB_HOST"],
        port=int(os.environ.get("TIDB_PORT", "4000")),
        user=os.environ["TIDB_USER"],
        password=os.environ["TIDB_PASSWORD"],
        database=os.environ["TIDB_DATABASE"],
        ssl={"ssl": {}},          # TiDB Cloud requires TLS
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=10,
    )
