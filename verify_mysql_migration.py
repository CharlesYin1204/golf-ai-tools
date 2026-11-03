"""在切换服务前，独立逐行核对SQLite快照与MySQL的18张业务/系统表。"""
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import SCHEMA, mysql_config_from_env
from mysql_support import connect, table_columns

source = sqlite3.connect('file:' + str(Path(sys.argv[1]).resolve()) + '?mode=ro', uri=True)
assert source.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
assert not source.execute('PRAGMA foreign_key_check').fetchall()
target = connect(mysql_config_from_env())
report = {'integrity': 'ok', 'exact_tables': {}, 'admin_hash_unchanged': False}
try:
    for table, columns in table_columns(SCHEMA).items():
        selected = ','.join('`' + column + '`' for column in columns)
        order = '`' + columns[0] + '`'
        left = source.execute(f'SELECT {selected} FROM `{table}` ORDER BY {order}').fetchall()
        right = [tuple(row[column] for column in columns) for row in target.execute(
            f'SELECT {selected} FROM `{table}` ORDER BY {order}').fetchall()]
        assert left == right, '迁移数据不同：' + table
        report['exact_tables'][table] = len(left)
    report['admin_hash_unchanged'] = True
    assert len(report['exact_tables']) == 18
    print(json.dumps(report, ensure_ascii=False))
finally:
    target.close()
    source.close()
