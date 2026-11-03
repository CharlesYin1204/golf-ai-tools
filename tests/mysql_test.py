#!/usr/bin/env python3
"""在隔离的真实 MySQL 实例上复用 HTTP 回归并验收完整迁移。

需要 PyMySQL，以及允许创建/删除临时 golfera_test_* 数据库的测试账号。
显式启用：GOLFERA_TEST_MYSQL=1；可选 GOLFERA_TEST_MYSQL_HOST/PORT/USER/PASSWORD。
不接受生产数据库名称，只创建随机临时库，完成后删除本次临时库。
"""
import copy
import os
import secrets
import tempfile
import threading
import unittest
from pathlib import Path

import backend_test as base
from server import Database, GolfServer, Handler, MODELS, now
from migrate_sqlite_to_mysql import migrate
from mysql_support import compile_sql, table_columns

if os.environ.get("GOLFERA_TEST_MYSQL") == "1":
    import pymysql


def test_config():
    return {"host":os.environ.get("GOLFERA_TEST_MYSQL_HOST","127.0.0.1"),
            "port":int(os.environ.get("GOLFERA_TEST_MYSQL_PORT","33071")),
            "user":os.environ.get("GOLFERA_TEST_MYSQL_USER","root"),
            "password":os.environ.get("GOLFERA_TEST_MYSQL_PASSWORD","")}


class TemporaryMySQL:
    def __init__(self):
        self.name="golfera_test_"+secrets.token_hex(8)
        self.config={**test_config(),"database":self.name}
        self.admin=pymysql.connect(charset="utf8mb4",autocommit=True,**test_config())
        with self.admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{self.name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")

    def close(self):
        with self.admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{self.name}`")
        self.admin.close()


@unittest.skipUnless(os.environ.get("GOLFERA_TEST_MYSQL")=="1","需要显式启用隔离 MySQL 测试")
class MySQLHTTPTests(base.BackendTests):
    @classmethod
    def setUpClass(cls):
        cls.mysql=TemporaryMySQL()
        cls.temp=tempfile.TemporaryDirectory(prefix="golfera-mysql-http-")
        cls.password="MySQLOnly_"+secrets.token_urlsafe(16)
        original=os.environ.get("GOLFERA_ADMIN_PASSWORD")
        os.environ["GOLFERA_ADMIN_PASSWORD"]=cls.password
        cls.db=Database(backend="mysql",mysql_config=cls.mysql.config,test_fixtures=True)
        if original is None:
            del os.environ["GOLFERA_ADMIN_PASSWORD"]
        else:
            os.environ["GOLFERA_ADMIN_PASSWORD"]=original
        cls.http=GolfServer(("127.0.0.1",0),cls.db,base.ROOT/"dist")
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True)
        cls.thread.start()
        cls.base=f"http://127.0.0.1:{cls.http.server_port}"
        cls.admin=base.Client(cls.base)
        cls.admin.data("/api/auth/login","POST",{"username":"admin","password":cls.password})

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.mysql.close()

    def test_07_database_reopen_and_password_storage(self):
        client,state=self.mobile()
        order_id=state["orders"][0]["id"]
        reopened=Database(backend="mysql",mysql_config=self.mysql.config)
        with reopened.connection() as conn:
            self.assertEqual(conn.execute("SELECT people FROM orders WHERE id=?",(order_id,)).fetchone()[0],2)
            encoded=conn.execute("SELECT password_hash FROM admins").fetchone()[0]
            self.assertTrue(encoded.startswith("pbkdf2_sha256$"))
            self.assertNotIn(self.password,encoded)
            self.assertEqual(conn.execute("SELECT @@foreign_key_checks").fetchone()[0],1)
            self.assertTrue(conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]>0)
        self.assertEqual(client.data("/api/health")["database"],"MySQL")

    def test_10_admin_deleted_seed_record_stays_deleted_on_reopen(self):
        self.admin.data("/api/admin/lessons/strategy","DELETE",{})
        reopened=Database(backend="mysql",mysql_config=self.mysql.config)
        with reopened.connection() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM lessons WHERE id='strategy'").fetchone())
        self.assertEqual(self.admin.data("/api/admin/seed","POST",{})["inserted"],1)


@unittest.skipUnless(os.environ.get("GOLFERA_TEST_MYSQL")=="1","需要显式启用隔离 MySQL 测试")
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.mysql=TemporaryMySQL()
        self.temp=tempfile.TemporaryDirectory(prefix="golfera-migration-")
        self.source_path=Path(self.temp.name)/"source.db"
        original=os.environ.get("GOLFERA_ADMIN_PASSWORD")
        os.environ["GOLFERA_ADMIN_PASSWORD"]="MigrationOnly_"+secrets.token_urlsafe(16)
        self.source=Database(self.source_path,test_fixtures=True)
        if original is None:
            del os.environ["GOLFERA_ADMIN_PASSWORD"]
        else:
            os.environ["GOLFERA_ADMIN_PASSWORD"]=original
        with self.source.connection() as conn:
            with self.source.transaction(conn):
                visitor=self.source.create_visitor(conn)
                self.visitor_id=visitor["id"]
                state=self.source.project_state(conn,visitor)
                state["favorites"]=["qizhong","sheshan"]
                state["learned"]=["warmup"]
                state["joined"]=True
                self.source.mobile_save(conn,visitor,state)
                admin=conn.execute("SELECT * FROM admins").fetchone()
                conn.execute("INSERT INTO sessions(token_hash,admin_id,expires) VALUES(?,?,?)",("migration_session",admin["id"],2000000000.125))
                conn.execute("UPDATE settings SET value=? WHERE key='site_name'",("迁移保留的中文站点",))
                self.source.audit(conn,"migration-test","custom","database","source",{"note":"中文/JSON/用户修改均保留"})

    def tearDown(self):
        self.mysql.close()
        self.temp.cleanup()

    def test_01_every_table_row_and_admin_session_visitor_retained(self):
        report=migrate(self.source_path,Path(self.temp.name)/"backup.db",self.mysql.config)
        self.assertEqual(report["table_count"],len(table_columns(base.module.SCHEMA)))
        self.assertTrue(report["rows"]>350)
        target=Database(backend="mysql",mysql_config=self.mysql.config)
        with self.source.connection() as source,target.connection() as dest:
            for table,columns in table_columns(base.module.SCHEMA).items():
                key="key" if table in ("app_meta","settings") else "token_hash" if table in ("sessions","user_sessions") else "user_id" if table=="user_accounts" else "date" if table=="ai_daily_usage" else "id"
                select=f"SELECT {','.join('`'+column+'`' for column in columns)} FROM `{table}` ORDER BY `{key}`"
                self.assertEqual([dict(row) for row in source.execute(select)],[dict(row) for row in dest.execute(select)],table)
            original_visitor=source.execute("SELECT * FROM visitors WHERE id=?",(self.visitor_id,)).fetchone()
            new_visitor=dest.execute("SELECT * FROM visitors WHERE id=?",(self.visitor_id,)).fetchone()
            self.assertEqual(self.source.project_state(source,original_visitor),target.project_state(dest,new_visitor))
            expected_fks=sum(len({row["id"] for row in source.execute(f"PRAGMA foreign_key_list({table})")}) for table in table_columns(base.module.SCHEMA))
            self.assertEqual(dest.execute("SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=?",(self.mysql.name,)).fetchone()[0],expected_fks)
            self.assertEqual(dest.execute("SELECT @@foreign_key_checks").fetchone()[0],1)

    def test_02_nonempty_target_refused_without_overwriting(self):
        target=Database(backend="mysql",mysql_config=self.mysql.config,initialize=False)
        with target.connection() as conn:
            target.create_schema(conn)
            with target.transaction(conn):
                target.insert(conn,"users",{"id":"existing","name":"已有用户","phone":None,"role":"golfer","status":"active"})
        with self.assertRaisesRegex(RuntimeError,"已有数据"):
            migrate(self.source_path,Path(self.temp.name)/"backup.db",self.mysql.config)
        with target.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],1)

    def test_03_mid_import_failure_rolls_back_all_rows(self):
        with self.source.connection() as conn:
            conn.execute("UPDATE lessons SET title=? WHERE id='warmup'",("超长"*200,))
        with self.assertRaises(pymysql.DataError):
            migrate(self.source_path,Path(self.temp.name)/"backup.db",self.mysql.config)
        target=Database(backend="mysql",mysql_config=self.mysql.config,initialize=False)
        with target.connection() as conn:
            for table in table_columns(base.module.SCHEMA):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM `{table}`").fetchone()[0],0,table)


class CompilerTests(unittest.TestCase):
    def test_ddl_keywords_and_literals_untouched(self):
        ddl="CREATE TABLE `app_meta` (`key` VARCHAR(100) PRIMARY KEY, `value` TEXT NOT NULL)"
        self.assertEqual(compile_sql(ddl),ddl)
        self.assertEqual(compile_sql("SELECT * FROM settings WHERE key=? AND value='key ? rowid'"),"SELECT * FROM settings WHERE `key`=%s AND value='key ? rowid'")


# Exercise the same user-session behavior against MySQL as SQLite.
import accounts_test
MySQLHTTPTests.test_account_login_and_isolation=accounts_test.AccountsTests.test_account_login_and_isolation
MySQLHTTPTests.test_account_admin_only_and_password_reset=accounts_test.AccountsTests.test_account_admin_only_and_password_reset


import production_test
@unittest.skipUnless(os.environ.get("GOLFERA_TEST_MYSQL")=="1","requires isolated MySQL")
class ProductionMySQLTests(production_test.ProductionTests):
    @classmethod
    def setUpClass(cls):
        cls.mysql=TemporaryMySQL()
        os.environ['GOLFERA_ADMIN_PASSWORD']='ProductionTest_5678'
        cls.db=Database(backend='mysql',mysql_config=cls.mysql.config)
        cls.http=GolfServer(('127.0.0.1',0),cls.db,base.ROOT/'dist')
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.http.server_port}'
        cls.admin=base.Client(cls.base)
        cls.admin.data('/api/auth/login','POST',{'username':'admin','password':'ProductionTest_5678'})
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown();cls.http.server_close();cls.thread.join();cls.mysql.close()

if __name__=="__main__":
    unittest.main(verbosity=2)
