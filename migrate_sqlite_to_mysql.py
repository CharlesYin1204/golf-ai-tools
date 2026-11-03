#!/usr/bin/env python3
"""把停止写入后的 SQLite 完整迁移到空 MySQL 库；保留密码哈希与全部业务记录。

先停止旧服务，再配置 GOLFERA_MYSQL_* 环境变量。运行示例：
python migrate_sqlite_to_mysql.py --sqlite /opt/golfera/data/golfera.db \
    --backup /安全备份目录/golfera-pre-mysql.db

脚本仅创建缺少的表，不删除/覆盖 MySQL 数据；目标任一原表非空时拒绝迁移。
导入与逐行校验在一个 InnoDB 事务内，失败回滚全部导入行。DDL 留下空表可重试。
"""
import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from server import Database, MODELS, SCHEMA, mysql_config_from_env
from mysql_support import table_columns


def canonical(rows):
    return json.dumps(rows,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)


def migrate(source_path, backup_path, mysql_config=None):
    source_path, backup_path = Path(source_path).resolve(), Path(backup_path).resolve()
    if not source_path.is_file() or source_path == backup_path:
        raise RuntimeError("SQLite 来源不存在，或备份路径与来源相同。")
    if backup_path.exists():
        raise RuntimeError("备份路径已存在；拒绝覆盖，请指定新的备份文件名。")
    columns=table_columns(SCHEMA)
    table_names=list(columns)
    # SQLite backup API 会包含 WAL 中已提交记录，比直接拷贝主文件更安全。
    source=sqlite3.connect(source_path.as_uri()+"?mode=ro",uri=True)
    source.row_factory=sqlite3.Row
    backup_path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(str(backup_path),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    os.close(fd)
    snapshot=sqlite3.connect(str(backup_path))
    snapshot.row_factory=sqlite3.Row
    try:
        source.backup(snapshot)
        source.close()
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite 完整性检查未通过，迁移已停止。")
        if snapshot.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("SQLite 有外键异常，迁移已停止。")
        actual={row[0] for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if actual != set(table_names):
            raise RuntimeError("来源表集合与当前数据库表规格不一致，拒绝不完整迁移。")
        payload={}
        source_sequence={}
        for name,expected_columns in columns.items():
            actual_columns=[row["name"] for row in snapshot.execute(f"PRAGMA table_info({name})")]
            if actual_columns != expected_columns:
                raise RuntimeError(f"来源 {name} 字段结构不一致，迁移已停止。")
            key="key" if name in ("app_meta","settings") else "token_hash" if name in ("sessions","user_sessions") else "user_id" if name=="user_accounts" else "date" if name=="ai_daily_usage" else "id"
            rows=snapshot.execute(f"SELECT * FROM {name} ORDER BY `{key}`").fetchall()
            payload[name]=[dict(row) for row in rows]
            if name in MODELS:
                source_sequence[name]={row["id"]:row["rowid"] for row in snapshot.execute(f"SELECT rowid,* FROM {name}")}
        if not payload["admins"]:
            raise RuntimeError("来源缺少管理员密码哈希，拒绝迁移。")
        database=Database(backend="mysql",mysql_config=mysql_config or mysql_config_from_env(),initialize=False)
        with database.connection() as conn:
            existing={row[0] for row in conn.execute("SHOW TABLES")}
            if existing - set(table_names) - {"_write_guard"}:
                raise RuntimeError("目标库包含其他应用的表，拒绝修改。")
            for name in existing & set(table_names):
                if conn.execute(f"SELECT COUNT(*) FROM `{name}`").fetchone()[0]:
                    raise RuntimeError(f"目标 {name} 已有数据，拒绝覆盖。")
            database.create_schema(conn)
            with database.transaction(conn):
                # 再次检查在创建DDL/取得全局写锁期间是否有其他服务写入。
                for name in table_names:
                    if conn.execute(f"SELECT COUNT(*) FROM `{name}`").fetchone()[0]:
                        raise RuntimeError(f"目标 {name} 已有数据，拒绝覆盖。")
                for name,rows in payload.items():
                    insert_columns=list(columns[name])+(["_seq"] if name in MODELS else [])
                    statement=f"INSERT INTO `{name}`({','.join('`'+key+'`' for key in insert_columns)}) VALUES({','.join('?' for _ in insert_columns)})"
                    for row in rows:
                        values=[row[key] for key in columns[name]]
                        if name in MODELS:
                            values.append(source_sequence[name][row["id"]])
                        conn.execute(statement,values)
                report={}
                for name in table_names:
                    key="key" if name in ("app_meta","settings") else "token_hash" if name in ("sessions","user_sessions") else "user_id" if name=="user_accounts" else "date" if name=="ai_daily_usage" else "id"
                    target=[dict(row) for row in conn.execute(f"SELECT {','.join('`'+column+'`' for column in columns[name])} FROM `{name}` ORDER BY `{key}`")]
                    expected=payload[name]
                    if target != expected:
                        raise RuntimeError(f"{name} 导入后逐行比对不一致，全部导入已回滚。")
                    report[name]={"rows":len(expected),"sha256":hashlib.sha256(canonical(expected).encode()).hexdigest()}
                if conn.execute("SELECT @@foreign_key_checks").fetchone()[0] != 1:
                    raise RuntimeError("MySQL 外键未启用，导入已回滚。")
                # 每条来源记录逐行相同；没有额外写业务日志、改密码或生成示例数据。
            return {"ok":True,"backend":"MySQL","tables":report,"table_count":len(report),
                    "rows":sum(item["rows"] for item in report.values()),"sqlite_backup":str(backup_path)}
    finally:
        source.close()
        snapshot.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite",required=True,help="旧SQLite完整数据库路径")
    parser.add_argument("--backup",required=True,help="新的一致性SQLite备份文件（不得已存在）")
    args=parser.parse_args()
    try:
        result=migrate(args.sqlite,args.backup)
    except Exception as error:
        # 不打印驱动堆栈/连接配置或行数据，避免凭据与用户资料出现在迁移日志。
        print(json.dumps({"ok":False,"error":type(error).__name__+": "+str(error)},ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
