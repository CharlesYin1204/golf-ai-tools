#!/usr/bin/env python3
"""高尔夫AI工具演示服务：SQLite 或 MySQL 保存前后台业务记录。

运行：GOLFERA_ADMIN_PASSWORD='set-a-strong-password' python3 server.py
MySQL：GOLFERA_DB_TYPE=mysql，连接配置见 mysql_config_from_env；需要 PyMySQL。
可选：PORT、HOST、GOLFERA_DB、GOLFERA_STATIC_DIR、GOLFERA_COOKIE_SECURE。
所有初始化数据均为模拟运营数据，预约不连接真实球场或支付。
"""

import csv
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

BASE = Path(__file__).resolve().parent
# 兼容测试通过 importlib 从任意工作目录加载此模块。
import sys
if str(BASE) not in sys.path:
    sys.path.insert(0,str(BASE))
import ai_service
import ai_features

CHINA = timezone(timedelta(hours=8))
SESSION_TTL = 8 * 60 * 60
MAX_BODY = 1024 * 1024
PASSWORD_ITERATIONS = 260000
PARS = [4, 4, 3, 5, 4, 4, 3, 5, 4, 4, 3, 5, 4, 4, 3, 5, 4, 4]
SAMPLE_SCORES = [5, 5, 4, 6, 5, 5, 4, 5, 5, 5, 3, 6, 5, 5, 4, 5, 4, 5]
LOG = logging.getLogger("golfera")


def now():
    return datetime.now(CHINA).isoformat(timespec="seconds")


def day(offset=0):
    return (datetime.now(CHINA).date() + timedelta(days=offset)).isoformat()


def uid(prefix):
    return prefix + secrets.token_hex(10)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest.hex()}"


def password_ok(password, encoded):
    try:
        algorithm, count, salt, digest = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(count)).hex()
        return hmac.compare_digest(actual, digest)
    except (ValueError, TypeError):
        return False


def choices(*values):
    return [{"value": value, "label": label} for value, label in values]


def field(key, label, kind="text", required=False, options=None, **extra):
    result = {"key": key, "label": label, "type": kind, "required": required}
    if options:
        result["options"] = options
    result.update(extra)
    return result


STATUS = choices(("active", "启用"), ("inactive", "停用"))
MODELS = {
    "courses": {"label": "球场管理", "prefix": "C", "fields": [
        field("name", "球场名称", required=True), field("short", "简称", required=True),
        field("address", "地址", required=True), field("holes", "洞数", "number", True, min=9, max=36),
        field("price", "参考单人价（元）", "number", True, min=0), field("distance", "距离（km）", "number", True, min=0),
        field("tags", "标签数组", "json"), field("status", "状态", "select", True, STATUS)],
        "columns": ["id", "name", "address", "holes", "price", "status"]},
    "slots": {"label": "时段库存", "prefix": "S", "fields": [
        field("course_id", "球场", required=True, relation="courses"), field("date", "日期", "date", True),
        field("time", "开球时间", "time", True), field("capacity", "总名额", "number", True, min=1, max=200),
        field("price", "单人价（元）", "number", True, min=0),
        field("status", "状态", "select", True, choices(("open", "可订"), ("closed", "关闭")))],
        "columns": ["id", "course_id", "date", "time", "capacity", "booked", "price", "status"]},
    "users": {"label": "用户管理", "prefix": "U", "fields": [
        field("name", "姓名 / 昵称", required=True), field("phone", "手机号"),
        field("role", "用户角色", "select", True, choices(("golfer", "球友"), ("caddie", "球童"), ("manager", "运营"))),
        field("status", "状态", "select", True, STATUS)], "columns": ["id", "name", "phone", "role", "status"]},
    "caddies": {"label": "球童与排班", "prefix": "CD", "fields": [
        field("user_id", "用户", required=True, relation="users"), field("name", "球童姓名", required=True),
        field("shift", "排班", required=True), field("skills", "服务技能数组", "json"),
        field("status", "状态", "select", True, choices(("available", "可安排"), ("busy", "服务中"), ("off", "休息")))],
        "columns": ["id", "name", "user_id", "shift", "status"]},
    "orders": {"label": "预约订单", "prefix": "GE", "fields": [
        field("user_id", "球友", required=True, relation="users"), field("course_id", "球场", required=True, relation="courses"),
        field("date", "日期", "date", True), field("time", "开球时间", "time", True),
        field("people", "人数", "number", True, min=1, max=4),
        field("status", "状态", "select", True, choices(("confirmed", "已确认"), ("cancelled", "已取消"))),
        field("checked", "已到场", "boolean")], "columns": ["id", "user_id", "course_id", "date", "time", "people", "total", "status", "checked"]},
    "rounds": {"label": "球局成绩", "prefix": "R", "fields": [
        field("user_id", "球友", required=True, relation="users"), field("course_id", "球场", required=True, relation="courses"),
        field("date", "日期", "date", True), field("tee", "发球台", "select", True, choices(("蓝Tee", "蓝Tee"), ("白Tee", "白Tee"), ("红Tee", "红Tee"), ("黑Tee", "黑Tee"))),
        field("players", "同行昵称数组", "json", True), field("pk", "玩法", "select", True, choices(("比杆赛", "比杆赛"), ("练习球局", "练习球局"))),
        field("scores", "18 洞成绩数组（未记填 null）", "json", True), field("complete", "已完成", "boolean")],
        "columns": ["id", "user_id", "course_id", "date", "tee", "total", "complete"]},
    "teams": {"label": "球队管理", "prefix": "T", "fields": [
        field("name", "球队名称", required=True), field("captain_id", "队长", required=True, relation="users"),
        field("city", "城市", required=True), field("member_count", "成员数", "number", True, min=1),
        field("description", "介绍", "textarea"), field("status", "状态", "select", True, STATUS)],
        "columns": ["id", "name", "captain_id", "city", "member_count", "status"]},
    "events": {"label": "活动赛事", "prefix": "E", "fields": [
        field("title", "活动名称", required=True), field("team_id", "组织球队", required=True, relation="teams"),
        field("course_id", "球场", required=True, relation="courses"), field("date", "日期", "date", True),
        field("time", "时间", "time", True), field("capacity", "总名额", "number", True, min=1),
        field("fee", "单人参考费（元）", "number", True, min=0), field("description", "活动安排", "textarea"),
        field("status", "状态", "select", True, choices(("open", "报名中"), ("closed", "已截止"), ("completed", "已结束")))],
        "columns": ["id", "title", "team_id", "date", "time", "registered", "capacity", "status"]},
    "registrations": {"label": "活动报名", "prefix": "RG", "fields": [
        field("event_id", "活动", required=True, relation="events"), field("user_id", "球友", required=True, relation="users"),
        field("people", "报名人数", "number", True, min=1, max=4),
        field("status", "状态", "select", True, choices(("registered", "已报名"), ("cancelled", "已退出")))],
        "columns": ["id", "event_id", "user_id", "people", "status"]},
    "service_tickets": {"label": "服务异常", "prefix": "TK", "fields": [
        field("order_id", "关联订单", relation="orders"), field("user_id", "报告用户", required=True, relation="users"),
        field("caddie_id", "处理球童", relation="caddies"), field("type", "问题类型", "select", True, choices(("装备数量差异", "装备数量差异"), ("客人服务需求", "客人服务需求"), ("失物登记", "失物登记"))),
        field("description", "问题说明", "textarea", True), field("priority", "优先级", "select", True, choices(("normal", "普通"), ("high", "紧急"))),
        field("status", "处理状态", "select", True, choices(("open", "待处理"), ("resolved", "已处理"))),
        field("reported_time", "记录时间")], "columns": ["id", "order_id", "type", "description", "priority", "status"]},
    "bag_records": {"label": "球包交接", "prefix": "BG", "fields": [
        field("order_id", "预约订单", required=True, relation="orders"), field("user_id", "球友", required=True, relation="users"),
        field("bag_code", "球包编号", required=True),
        field("action", "交接动作", "select", True, choices(("领取球包", "领取球包"), ("归还球包", "归还球包"))),
        field("clubs", "清点项目数组", "json", True), field("staff", "服务球童", required=True), field("record_time", "交接时间", required=True),
        field("notes", "备注", "textarea")], "columns": ["id", "order_id", "bag_code", "action", "staff", "record_time"]},
    "lessons": {"label": "教学内容", "prefix": "L", "fields": [
        field("title", "标题", required=True), field("category", "分类", "select", True, choices(("技巧", "技巧"), ("木杆", "木杆"), ("切杆", "切杆"), ("推杆", "推杆"), ("铁杆", "铁杆"))),
        field("duration", "时长", required=True), field("coach", "教练", required=True), field("description", "课程介绍", "textarea", True),
        field("steps", "教学步骤数组", "json", True), field("status", "发布状态", "select", True, choices(("published", "已发布"), ("draft", "草稿")))],
        "columns": ["id", "title", "category", "duration", "coach", "status"]},
    "audit_logs": {"label": "操作审计", "prefix": "A", "editable": False, "fields": [],
        "columns": ["id", "actor", "action", "model", "record_id", "created_at"]},
}
for _model in MODELS.values():
    _model.setdefault("editable", True)
    if _model["editable"]:
        _model["fields"].insert(0, field("id", "编号（创建时留空自动生成）"))

JSON_FIELDS = {"tags", "skills", "players", "scores", "clubs", "steps", "details"}
BOOL_FIELDS = {"checked", "complete", "pending"}
DEFAULTS = {
    "courses": {"tags": [], "status": "active", "holes": 18, "distance": 0},
    "slots": {"capacity": 16, "status": "open"},
    "users": {"phone": "", "role": "golfer", "status": "active"},
    "caddies": {"skills": [], "status": "available"},
    "orders": {"status": "confirmed", "checked": False},
    "rounds": {"tee": "蓝Tee", "players": ["我"], "pk": "比杆赛", "scores": [None] * 18, "complete": False},
    "teams": {"city": "上海", "member_count": 1, "description": "", "status": "active"},
    "events": {"description": "", "status": "open", "capacity": 8, "fee": 0},
    "registrations": {"people": 1, "status": "registered"},
    "service_tickets": {"priority": "normal", "status": "open", "reported_time": "", "order_id": None, "caddie_id": None},
    "bag_records": {"bag_code": "B-028", "staff": "球童小林", "notes": ""},
    "lessons": {"status": "draft", "steps": []},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_daily_usage(date TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ai_records(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admins(id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, admin_id TEXT NOT NULL REFERENCES admins(id), expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE, role TEXT NOT NULL CHECK(role IN ('golfer','caddie','manager')), status TEXT NOT NULL CHECK(status IN ('active','inactive')), source TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_accounts(user_id TEXT PRIMARY KEY REFERENCES users(id), username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_sessions(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS visitors(id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE REFERENCES users(id), state_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS courses(id TEXT PRIMARY KEY, name TEXT NOT NULL, short TEXT NOT NULL, address TEXT NOT NULL, holes INTEGER NOT NULL CHECK(holes IN (9,18,27,36)), price REAL NOT NULL CHECK(price>=0), distance REAL NOT NULL CHECK(distance>=0), tags TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL CHECK(status IN ('active','inactive')), source TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS slots(id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id), date TEXT NOT NULL, time TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity>0), price REAL NOT NULL CHECK(price>=0), status TEXT NOT NULL CHECK(status IN ('open','closed')), source TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(course_id,date,time));
CREATE TABLE IF NOT EXISTS caddies(id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE REFERENCES users(id), name TEXT NOT NULL, shift TEXT NOT NULL, skills TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL CHECK(status IN ('available','busy','off')), source TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, visitor_id TEXT REFERENCES visitors(id), user_id TEXT NOT NULL REFERENCES users(id), course_id TEXT NOT NULL REFERENCES courses(id), date TEXT NOT NULL, time TEXT NOT NULL, people INTEGER NOT NULL CHECK(people BETWEEN 1 AND 4), total REAL NOT NULL CHECK(total>=0), status TEXT NOT NULL CHECK(status IN ('confirmed','cancelled')), checked INTEGER NOT NULL DEFAULT 0 CHECK(checked IN (0,1)), source TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(course_id,date,time) REFERENCES slots(course_id,date,time));
CREATE INDEX IF NOT EXISTS orders_inventory ON orders(course_id,date,time,status);
CREATE UNIQUE INDEX IF NOT EXISTS orders_visitor_unique ON orders(visitor_id,course_id,date,time) WHERE visitor_id IS NOT NULL AND status='confirmed';
CREATE TABLE IF NOT EXISTS rounds(id TEXT PRIMARY KEY, visitor_id TEXT REFERENCES visitors(id), user_id TEXT NOT NULL REFERENCES users(id), course_id TEXT NOT NULL REFERENCES courses(id), date TEXT NOT NULL, tee TEXT NOT NULL, players TEXT NOT NULL, pk TEXT NOT NULL, scores TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0 CHECK(complete IN (0,1)), total INTEGER NOT NULL DEFAULT 0, hole INTEGER NOT NULL DEFAULT 0 CHECK(hole BETWEEN 0 AND 17), pending INTEGER NOT NULL DEFAULT 0 CHECK(pending IN (0,1)), source TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS teams(id TEXT PRIMARY KEY, name TEXT NOT NULL, captain_id TEXT NOT NULL REFERENCES users(id), city TEXT NOT NULL, member_count INTEGER NOT NULL CHECK(member_count>0), description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK(status IN ('active','inactive')), source TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, title TEXT NOT NULL, team_id TEXT NOT NULL REFERENCES teams(id), course_id TEXT NOT NULL REFERENCES courses(id), date TEXT NOT NULL, time TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity>0), fee REAL NOT NULL CHECK(fee>=0), description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK(status IN ('open','closed','completed')), source TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS registrations(id TEXT PRIMARY KEY, visitor_id TEXT REFERENCES visitors(id), event_id TEXT NOT NULL REFERENCES events(id), user_id TEXT NOT NULL REFERENCES users(id), people INTEGER NOT NULL DEFAULT 1 CHECK(people BETWEEN 1 AND 4), status TEXT NOT NULL CHECK(status IN ('registered','cancelled')), source TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS registrations_unique ON registrations(event_id,user_id) WHERE status='registered';
CREATE TABLE IF NOT EXISTS service_tickets(id TEXT PRIMARY KEY, visitor_id TEXT REFERENCES visitors(id), order_id TEXT REFERENCES orders(id), user_id TEXT NOT NULL REFERENCES users(id), caddie_id TEXT REFERENCES caddies(id), type TEXT NOT NULL, description TEXT NOT NULL, priority TEXT NOT NULL CHECK(priority IN ('normal','high')), status TEXT NOT NULL CHECK(status IN ('open','resolved')), reported_time TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bag_records(id TEXT PRIMARY KEY, visitor_id TEXT REFERENCES visitors(id), order_id TEXT NOT NULL REFERENCES orders(id), user_id TEXT NOT NULL REFERENCES users(id), bag_code TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('领取球包','归还球包')), clubs TEXT NOT NULL, staff TEXT NOT NULL, record_time TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lessons(id TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL, duration TEXT NOT NULL, coach TEXT NOT NULL, description TEXT NOT NULL, steps TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('published','draft')), source TEXT NOT NULL DEFAULT 'admin', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_logs(id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, model TEXT NOT NULL, record_id TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_created ON audit_logs(created_at);
"""


def mysql_config_from_env():
    config={"host":os.environ.get("GOLFERA_MYSQL_HOST","127.0.0.1"),
            "port":int(os.environ.get("GOLFERA_MYSQL_PORT","3306")),
            "user":os.environ.get("GOLFERA_MYSQL_USER","gaoqiu"),
            "password":os.environ.get("GOLFERA_MYSQL_PASSWORD",""),
            "database":os.environ.get("GOLFERA_MYSQL_DATABASE","gaoqiu")}
    if not config["password"]:
        raise RuntimeError("MySQL 模式必须设置 GOLFERA_MYSQL_PASSWORD。")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}",config["database"]):
        raise RuntimeError("MySQL 数据库名称无效。")
    return config


class Database:
    """每请求独立连接；SQLite立即事务/MySQL行锁保持相同库存与隔离规则。"""
    def __init__(self, path=None, backend=None, mysql_config=None, initialize=True, test_fixtures=False):
        self.test_fixtures=test_fixtures
        self.backend=backend or ("sqlite" if path is not None else os.environ.get("GOLFERA_DB_TYPE","sqlite"))
        if self.backend not in ("sqlite","mysql"):
            raise RuntimeError("GOLFERA_DB_TYPE 只支持 sqlite 或 mysql。")
        self.integrity_errors=(sqlite3.IntegrityError,)
        self.path=None
        self.mysql_config=mysql_config
        if self.backend=="mysql":
            import mysql_support
            self.mysql_support=mysql_support
            self.mysql_config=mysql_config or mysql_config_from_env()
            # 驱动按需导入；SQLite 测试与静态原型仍无需 pip 依赖。
            try:
                import pymysql
            except ImportError as error:
                raise RuntimeError("MySQL 模式需要先安装 requirements.txt。") from error
            self.integrity_errors+=(pymysql.IntegrityError,)
        else:
            self.path=Path(path or os.environ.get("GOLFERA_DB",str(BASE/"data"/"golfera.sqlite3"))).resolve()
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if not initialize:
            return
        with self.connection() as conn:
            self.create_schema(conn)
            with self.transaction(conn):
                if not conn.execute("SELECT 1 FROM admins LIMIT 1").fetchone():
                    password = os.environ.get("GOLFERA_ADMIN_PASSWORD", "")
                    if len(password) < 12:
                        raise RuntimeError("首次启动必须设置至少12位的 GOLFERA_ADMIN_PASSWORD；服务不会使用默认密码。")
                    username = os.environ.get("GOLFERA_ADMIN_USERNAME", "admin")
                    conn.execute("INSERT INTO admins(id,username,password_hash,created_at) VALUES(?,?,?,?)", (uid("AD"), username, password_hash(password), now()))
                if self.test_fixtures and not conn.execute("SELECT 1 FROM app_meta WHERE key='initial_seed'").fetchone():
                    self.seed(conn)
                    conn.execute("INSERT INTO app_meta(key,value) VALUES('initial_seed','1')")
                for key,value in {"site_name":"高尔夫AI工具","city":"","support_phone":"","opening_time":""}.items():
                    conn.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",(key,value,now()))
        if self.path:
            os.chmod(self.path, 0o600)

    def create_schema(self,conn):
        if self.backend=="mysql":
            for statement in self.mysql_support.mysql_schema(SCHEMA,MODELS):
                conn.execute(statement)
            conn.execute("INSERT OR IGNORE INTO _write_guard(id) VALUES(1)")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        if self.backend=="mysql":
            conn=self.mysql_support.connect(self.mysql_config)
        else:
            conn = sqlite3.connect(str(self.path), timeout=20, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=20000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self, conn):
        if self.backend=="mysql":
            conn.begin()
        else:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def audit(conn, actor, action, model, record_id, details=None):
        conn.execute("INSERT INTO audit_logs(id,actor,action,model,record_id,details,created_at) VALUES(?,?,?,?,?,?,?)", (uid("A"), actor, action, model, str(record_id), dumps(details or {}), now()))

    @staticmethod
    def bump_visitor(conn, visitor_id):
        if visitor_id:
            conn.execute("UPDATE visitors SET revision=revision+1,updated_at=? WHERE id=?", (now(), visitor_id))

    @staticmethod
    def insert(conn, model, record, source="seed", ignore=False):
        data = dict(record)
        data.setdefault("source", source)
        data.setdefault("created_at", now())
        data.setdefault("updated_at", now())
        values = [dumps(value) if key in JSON_FIELDS else int(value) if isinstance(value, bool) else value for key, value in data.items()]
        sql = f"INSERT {'OR IGNORE ' if ignore else ''}INTO {model}({','.join(data)}) VALUES({','.join('?' for _ in data)})"
        conn.execute(sql, values)

    def seed(self, conn):
        """稳定编号幂等补齐模拟资料，不覆盖管理员修改，也不清除访客记录。"""
        before = conn.total_changes
        for name, short, ident, price, distance, district in [
            ("上海旗忠高尔夫俱乐部", "旗忠", "qizhong", 980, 24, "闵行区"),
            ("上海佘山国际高尔夫俱乐部", "佘山", "sheshan", 1680, 36, "松江区"),
            ("上海银涛高尔夫俱乐部", "银涛", "yintao", 880, 28, "青浦区")]:
            self.insert(conn, "courses", dict(id=ident, name=name, short=short, address=f"上海市{district} · 演示地址", holes=18, price=price, distance=distance,
                tags=["18 洞球场", "含球童服务", "模拟运营数据"], status="active"), ignore=True)
        slot_times = {"qizhong": ["07:30", "08:00", "09:00", "10:30", "13:00", "14:00"], "sheshan": ["07:30", "08:30", "09:00", "10:30", "13:00", "14:00"], "yintao": ["07:30", "08:00", "09:30", "10:30", "13:00", "14:30"]}
        for course in conn.execute("SELECT * FROM courses WHERE id IN ('qizhong','sheshan','yintao')"):
            for n in range(15):
                for slot_time in slot_times[course["id"]]:
                    self.insert(conn, "slots", dict(id=f"S_{course['id']}_{day(n)}_{slot_time.replace(':','')}", course_id=course["id"], date=day(n), time=slot_time, capacity=16, price=course["price"], status="open"), ignore=True)
        names = ["林先生", "王女士", "陈先生", "张女士", "刘先生", "李女士", "赵先生", "周女士", "吴先生", "郑女士", "孙先生", "徐女士"]
        for n in range(1, 31):
            self.insert(conn, "users", dict(id=f"U{n:03d}", name=f"{names[(n-1)%len(names)]}（模拟{n:02d}）", phone=f"1380000{n:04d}", role="golfer", status="active"), ignore=True)
        self.insert(conn, "users", dict(id="U_MANAGER", name="演示运营主管", phone="13800009999", role="manager", status="active"), ignore=True)
        for n in range(1, 7):
            user_id = f"U_CADDIE{n}"
            self.insert(conn, "users", dict(id=user_id, name=f"球童{['小林','小陈','小王','小李','小周','小吴'][n-1]}", phone=f"1390000000{n}", role="caddie", status="active"), ignore=True)
            self.insert(conn, "caddies", dict(id=f"CD{n:03d}", user_id=user_id, name=f"球童{['小林','小陈','小王','小李','小周','小吴'][n-1]}", shift="07:30—15:30" if n%2 else "10:30—18:30", skills=["装备清点", "球洞引导", "记分协助"], status="busy" if n<=2 else "available"), ignore=True)
        for n in range(1, 16):
            course_id = ["qizhong", "sheshan", "yintao"][(n-1)%3]
            date_value, slot_time = day(1 + (n-1)//3), slot_times[course_id][(n-1)%6]
            slot = conn.execute("SELECT * FROM slots WHERE course_id=? AND date=? AND time=?", (course_id, date_value, slot_time)).fetchone()
            if not conn.execute("SELECT 1 FROM orders WHERE id=?", (f"GESEED{n:03d}",)).fetchone():
                booked = self.booked(conn, course_id, date_value, slot_time)
                # 再次导入遇到已售满的时段时保留示例为已取消，库存始终有效。
                status = "confirmed" if n%7 and booked+2<=slot["capacity"] and slot["status"] == "open" else "cancelled"
                self.insert(conn, "orders", dict(id=f"GESEED{n:03d}", user_id=f"U{n:03d}", course_id=course_id, date=date_value, time=slot_time, people=2, total=slot["price"]*2, status=status, checked=False))
        for n in range(1, 13):
            scores = [min(20, value + (n%3)-1) for value in SAMPLE_SCORES]
            self.insert(conn, "rounds", dict(id=f"RSEED{n:03d}", user_id=f"U{n:03d}", course_id=["qizhong","sheshan","yintao"][n%3], date=day(-n), tee="蓝Tee", players=["我", "模拟同行"], pk="比杆赛", scores=scores, complete=True, total=sum(scores), hole=17, pending=False), ignore=True)
        for n, name in enumerate(["周末高球俱乐部", "城市球友会", "新手练习队"], 1):
            self.insert(conn, "teams", dict(id=f"T{n:03d}", name=name, captain_id=f"U{n:03d}", city="上海", member_count=12+n*3, description="模拟球队，欢迎以轻松的节奏约球交流。", status="active"), ignore=True)
        for n, (ident, title, capacity, course_id) in enumerate([("event_weekend", "一起打场周末球", 8, "qizhong"), ("E002", "城市球友友谊赛", 24, "sheshan"), ("E003", "新手下场练习", 12, "yintao")], 1):
            self.insert(conn, "events", dict(id=ident, title=title, team_id=f"T{n:03d}", course_id=course_id, date=day(3+n), time="09:00", capacity=capacity, fee={"qizhong":980,"sheshan":1680,"yintao":880}[course_id], description="08:30集合，09:00开球。18洞练习结束后一起复盘。模拟活动不产生真实报名或付款。", status="open"), ignore=True)
        for n in range(1, 9):
            event_id = "event_weekend" if n<=5 else "E002"
            if not conn.execute("SELECT 1 FROM registrations WHERE id=?", (f"RGSEED{n:03d}",)).fetchone():
                event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
                if self.registered(conn, event_id) < event["capacity"]:
                    self.insert(conn, "registrations", dict(id=f"RGSEED{n:03d}", event_id=event_id, user_id=f"U{n:03d}", people=1, status="registered"), ignore=True)
        for n in range(1, 5):
            self.insert(conn, "service_tickets", dict(id=f"TKSEED{n:03d}", order_id=f"GESEED{n:03d}", user_id=f"U{n:03d}", caddie_id=f"CD{n:03d}", type="装备数量差异" if n%2 else "客人服务需求", description="模拟记录：装备清点需要再次核对。" if n%2 else "模拟记录：希望增加饮水服务。", priority="high" if n==1 else "normal", status="open" if n<=2 else "resolved", reported_time=now()), ignore=True)
        for n in range(1, 4):
            for action, suffix in [("领取球包", "A"), ("归还球包", "B")]:
                self.insert(conn, "bag_records", dict(id=f"BGSEED{n}{suffix}", order_id=f"GESEED{n:03d}", user_id=f"U{n:03d}", bag_code=f"B-{n:03d}", action=action, clubs=[True]*4, staff="球童小林", record_time=now(), notes="模拟交接记录，不连接扫码设备。"), ignore=True)
        lessons = [
            ("warmup","开球前，做好 5 分钟热身","技巧","5 分钟","陈教练","用轻松的活动和半挥杆找到节奏，让第一洞从容开始。", ["轻柔活动肩部、髋部与脚踝。", "用短铁做半挥杆，保持身体平衡。", "逐步增加幅度，找到稳定节奏。", "检查手套、球和球 Tee。"]),
            ("driver","一号木开球：先求稳，再求远","木杆","8 分钟","王教练","建立可重复的开球准备动作。",["选定清晰的落球目标。","先瞄准杆面，再完成站位。","用舒适速度挥杆，保持收杆平衡。","记录落球方向与稳定性。"]),
            ("chip","果岭边切杆：选好落点","切杆","6 分钟","陈教练","把切杆拆成落点与滚动距离。",["观察球位与果岭坡度。","选择清晰落点与适合球杆。","保持动作平稳。","每组练习5球并记录距离。"]),
            ("putt","三尺推杆，建立你的准备流程","推杆","7 分钟","林教练","用同一套读线与瞄准流程练习短推。",["从球后观察坡度。","瞄准方向后试挥一次。","保持节奏观察滚动。","记录方向与速度并调整。"]),
            ("iron","七号铁练习：稳定击球与距离","铁杆","9 分钟","王教练","用固定目标和分组练习找到铁杆节奏。",["完成杆面与身体瞄准。","先用小幅挥杆观察触球。","每5球一组记录落球范围。","根据本组表现调整目标。"]),
            ("strategy","下场策略：如何选择安全的目标","技巧","6 分钟","林教练","为下一杆留出安全空间。",["确认球洞障碍与安全区域。","按常用距离选杆，留出偏差余量。","不理想球位先回安全区域。","记录一次有效决策。"]),
        ]
        for ident,title,category,duration,coach,description,steps in lessons:
            self.insert(conn, "lessons", dict(id=ident,title=title,category=category,duration=duration,coach=coach,description=description,steps=steps,status="published"), ignore=True)
        settings = {"site_name":"高尔夫AI工具", "city":"上海", "support_phone":"400-000-0000（模拟）", "demo_notice":"本系统为可交互演示，数据库记录及运营资料均为模拟，不连接真实预订或支付；AI功能使用后台配置的真实模型。", "opening_time":"07:00—18:30"}
        for key,value in settings.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)", (key,value,now()))
        return conn.total_changes - before

    @staticmethod
    def booked(conn, course_id, date_value, slot_time, exclude=None):
        sql = "SELECT COALESCE(SUM(people),0) FROM orders WHERE course_id=? AND date=? AND time=? AND status='confirmed'"
        args = [course_id,date_value,slot_time]
        if exclude:
            sql += " AND id<>?"
            args.append(exclude)
        return conn.execute(sql,args).fetchone()[0]

    @staticmethod
    def registered(conn, event_id, exclude=None):
        sql = "SELECT COALESCE(SUM(people),0) FROM registrations WHERE event_id=? AND status='registered'"
        args = [event_id]
        if exclude:
            sql += " AND id<>?"
            args.append(exclude)
        return conn.execute(sql,args).fetchone()[0]

    @staticmethod
    def record(row):
        if row is None:
            return None
        result = dict(row)
        result={key:value for key,value in result.items() if not key.startswith("_")}
        for key,value in list(result.items()):
            if key in JSON_FIELDS and isinstance(value,str):
                try:
                    result[key] = json.loads(value)
                except ValueError:
                    result[key] = [] if key != "details" else {}
            elif key in BOOL_FIELDS:
                result[key] = bool(value)
        return result

    def enrich(self, conn, model, row):
        result = self.record(row)
        if model == "slots":
            result["booked"] = self.booked(conn,result["course_id"],result["date"],result["time"])
            result["available"] = max(0,result["capacity"]-result["booked"])
        elif model == "events":
            result["registered"] = self.registered(conn,result["id"])
        return result

    def validate(self, conn, model, payload, current=None):
        """统一表单验证。金额、总杆与库存从服务端数据计算，不接受客户端计算结果。"""
        if not isinstance(payload, dict):
            raise APIError(400, "提交内容必须是 JSON 对象。")
        metadata = MODELS[model]
        accepted = {item["key"] for item in metadata["fields"]}
        unknown = set(payload) - accepted
        if unknown:
            raise APIError(400, "包含不允许修改的字段：" + ", ".join(sorted(unknown)))
        original = self.record(current) if current else {}
        data = {**DEFAULTS.get(model, {}), **{key:value for key,value in original.items() if key in accepted}, **payload}
        for item in metadata["fields"]:
            key, kind = item["key"], item["type"]
            value = data.get(key)
            if key == "id":
                if value and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value)):
                    raise APIError(400, "编号只允许字母、数字、下划线和短横线，最多80字符。")
                if current and value != current["id"]:
                    raise APIError(400, "记录编号不能修改。")
                continue
            if value in (None, ""):
                if item["required"]:
                    raise APIError(400, f"请填写{item['label']}。")
                data[key] = [] if kind == "json" else False if kind == "boolean" else None if item.get("relation") else ""
                continue
            if kind == "number":
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not float("-inf") < value < float("inf"):
                    raise APIError(400, f"{item['label']}必须是有效数字。")
                low, high = item.get("min", 0), item.get("max", 1000000)
                if not low <= value <= high:
                    raise APIError(400, f"{item['label']}应在{low}—{high}之间。")
                if key in {"holes", "capacity", "people", "member_count"} and int(value) != value:
                    raise APIError(400, f"{item['label']}必须是整数。")
                data[key] = int(value) if int(value) == value else round(value, 2)
            elif kind == "boolean":
                if not isinstance(value, bool) and value not in (0, 1):
                    raise APIError(400, f"{item['label']}必须是是/否。")
                data[key] = bool(value)
            elif kind == "json":
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        raise APIError(400, f"{item['label']}不是有效的 JSON。")
                if not isinstance(value, list) or len(value) > 100:
                    raise APIError(400, f"{item['label']}必须是数组，最多100项。")
                data[key] = value
            else:
                if not isinstance(value, str):
                    raise APIError(400, f"{item['label']}必须是文字。")
                value = value.strip()
                limit = 3000 if kind == "textarea" else 300
                if len(value) > limit or "\x00" in value:
                    raise APIError(400, f"{item['label']}过长或包含无效字符。")
                data[key] = value
                if kind == "select" and value not in {opt["value"] for opt in item["options"]}:
                    raise APIError(400, f"{item['label']}的选项无效。")
                if kind == "date":
                    try:
                        if date.fromisoformat(value).isoformat() != value:
                            raise ValueError()
                    except ValueError:
                        raise APIError(400, "日期应使用 YYYY-MM-DD 格式。")
                if kind == "time" and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                    raise APIError(400, "时间应使用 HH:MM 格式。")
            if item.get("relation") and data[key] is not None:
                if not conn.execute(f"SELECT 1 FROM {item['relation']} WHERE id=?", (data[key],)).fetchone():
                    raise APIError(400, f"{item['label']}对应的记录不存在。")
        if model == "courses" and data["holes"] not in (9,18,27,36):
            raise APIError(400, "洞数应为9、18、27或36。")
        if model == "users":
            if data.get("phone") and not re.fullmatch(r"[0-9+ -]{6,24}", data["phone"]):
                raise APIError(400, "手机号格式无效。")
            data["phone"] = data.get("phone") or None
            if current and data["role"] != "caddie" and conn.execute("SELECT 1 FROM caddies WHERE user_id=?",(current["id"],)).fetchone():
                raise APIError(409,"该用户已有球童排班，请先删除排班再修改角色。")
        if model == "caddies":
            user = conn.execute("SELECT role FROM users WHERE id=?", (data["user_id"],)).fetchone()
            if user["role"] != "caddie":
                raise APIError(400, "球童排班必须绑定球童角色的用户。")
        for key in ("tags", "skills", "players", "steps"):
            if key in data and any(not isinstance(value,str) or len(value)>500 for value in data[key]):
                raise APIError(400, f"{key}数组只能包含文字，单项最多500字符。")
        if model == "orders":
            slot = conn.execute("SELECT * FROM slots WHERE course_id=? AND date=? AND time=?", (data["course_id"], data["date"], data["time"])).fetchone()
            if not slot:
                raise APIError(400, "该日期和开球时间尚未配置库存，请先新增时段。")
            changed = not current or any(data[key] != current[key] for key in ("course_id", "date", "time", "people", "status"))
            schedule_changed=current and any(data[key] != current[key] for key in ("course_id","date","time","people"))
            if schedule_changed and conn.execute("SELECT 1 FROM bag_records WHERE order_id=?",(current["id"],)).fetchone():
                raise APIError(409,"此订单已有球包交接记录，请新建预约安排下一场。")
            if current and data["status"]=="cancelled" and current["status"]=="confirmed":
                received=conn.execute("SELECT 1 FROM bag_records WHERE order_id=? AND action='领取球包'",(current["id"],)).fetchone()
                returned=conn.execute("SELECT 1 FROM bag_records WHERE order_id=? AND action='归还球包'",(current["id"],)).fetchone()
                if received and not returned:
                    raise APIError(409,"球包尚未归还，请完成交接后再取消预约。")
            if data["status"] == "confirmed" and changed:
                course = conn.execute("SELECT status FROM courses WHERE id=?", (data["course_id"],)).fetchone()
                if slot["status"] != "open" or course["status"] != "active":
                    raise APIError(409, "这个时段或球场已关闭，请选择其他时间。")
                if self.booked(conn,data["course_id"],data["date"],data["time"], current["id"] if current else None)+data["people"]>slot["capacity"]:
                    raise APIError(409, "该时段剩余名额不足，请减少人数或更换时段。")
            data["total"] = round(slot["price"] * data["people"], 2) if changed else current["total"]
            if data["status"] == "cancelled":
                data["checked"] = False
            elif schedule_changed:
                data["checked"] = False
        if model == "rounds":
            scores = data["scores"]
            if len(scores) != 18 or any(value is not None and (isinstance(value,bool) or not isinstance(value,int) or not 1 <= value <= 20) for value in scores):
                raise APIError(400, "球局成绩必须包含18洞；每洞为1—20的整数，未记填 null。")
            if not 1 <= len(data["players"]) <= 4:
                raise APIError(400, "同行列表需包含1—4个昵称。")
            if data["complete"] and any(value is None for value in scores):
                raise APIError(400, "记满18洞后才能完成球局。")
            data["total"] = sum(value or 0 for value in scores)
        if model == "slots" and current:
            if any(data[key] != current[key] for key in ("course_id", "date", "time")) and conn.execute("SELECT 1 FROM orders WHERE course_id=? AND date=? AND time=?", (current["course_id"],current["date"],current["time"])).fetchone():
                raise APIError(409, "此时段已有订单；请新增时段并改期订单，不能直接移动库存记录。")
            if self.booked(conn,data["course_id"],data["date"],data["time"])>data["capacity"]:
                raise APIError(409, "总名额不能少于已有确认订单的人数。")
        if model == "events" and current and self.registered(conn,current["id"])>data["capacity"]:
            raise APIError(409, "活动名额不能少于已报名人数。")
        if model == "registrations" and data["status"] == "registered":
            event = conn.execute("SELECT * FROM events WHERE id=?", (data["event_id"],)).fetchone()
            changed = not current or any(data[key] != current[key] for key in ("event_id", "people", "status"))
            if changed and event["status"] != "open":
                raise APIError(409, "活动已停止报名。")
            if self.registered(conn,data["event_id"],current["id"] if current else None)+data["people"]>event["capacity"]:
                raise APIError(409, "活动剩余名额不足。")
        if model == "bag_records":
            if len(data["clubs"]) != 4 or any(not isinstance(value,bool) for value in data["clubs"]):
                raise APIError(400, "球杆清点需包含4个是/否值。")
            changed = not current or any(data[key] != current[key] for key in ("order_id", "action"))
            if changed:
                order = conn.execute("SELECT * FROM orders WHERE id=?", (data["order_id"],)).fetchone()
                if order["status"] != "confirmed" or not order["checked"]:
                    raise APIError(409, "有效预约完成到场核验后才能交接。")
                if not all(data["clubs"]):
                    raise APIError(400, "请先清点并确认所有球杆。")
                if conn.execute("SELECT 1 FROM service_tickets WHERE order_id=? AND status='open'", (data["order_id"],)).fetchone():
                    raise APIError(409, "尚有待处理异常，请处理后再交接。")
                received = conn.execute("SELECT 1 FROM bag_records WHERE order_id=? AND action='领取球包'", (data["order_id"],)).fetchone()
                returned = conn.execute("SELECT 1 FROM bag_records WHERE order_id=? AND action='归还球包'", (data["order_id"],)).fetchone()
                if data["action"] == "领取球包" and received or data["action"] == "归还球包" and (not received or returned):
                    raise APIError(409, "球包交接顺序不正确或已记录该动作。")
        data["id"] = data.get("id") or uid(metadata["prefix"])
        return data

    def write(self, conn, model, data, current=None, source="admin", visitor_id=None):
        data = dict(data)
        if visitor_id:
            data["visitor_id"] = visitor_id
        if model == "rounds":
            data.setdefault("hole", current["hole"] if current else 0)
            data.setdefault("pending", bool(current["pending"]) if current else False)
        if current:
            data["updated_at"] = now()
            ident = data.pop("id")
            values = [dumps(value) if key in JSON_FIELDS else int(value) if isinstance(value,bool) else value for key,value in data.items()]
            conn.execute(f"UPDATE {model} SET {','.join(key+'=?' for key in data)} WHERE id=?", [*values,ident])
            return ident
        self.insert(conn,model,data,source=source)
        return data["id"]

    def list_records(self, conn, model, params):
        try:
            page = max(1, int(params.get("page", ["1"])[0]))
            page_size = min(200, max(1, int(params.get("page_size", ["20"])[0])))
        except ValueError:
            raise APIError(400, "分页参数必须是整数。")
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({model})")}
        sort = params.get("sort", ["created_at"])[0]
        if sort not in columns:
            sort = "created_at"
        direction = "ASC" if params.get("direction", ["desc"])[0].lower() == "asc" else "DESC"
        query = params.get("q", [""])[0].strip()[:200]
        where, args = "", []
        if query:
            search_columns = [key for key in MODELS[model]["columns"] if key in columns]
            expressions = [f"CAST({model}.{key} AS TEXT) LIKE ? ESCAPE '!'" for key in search_columns]
            for item in MODELS[model]["fields"]:
                if item.get("relation") and item["key"] in columns:
                    related=item["relation"]
                    label="title" if related=="events" else "id" if related=="orders" else "name"
                    expressions.append(f"(SELECT {related}.{label} FROM {related} WHERE {related}.id={model}.{item['key']}) LIKE ? ESCAPE '!'")
            where = " WHERE " + " OR ".join(expressions)
            escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            args = [f"%{escaped}%"] * len(expressions)
        total = conn.execute(f"SELECT COUNT(*) FROM {model}{where}",args).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM {model}{where} ORDER BY {sort} {direction},rowid {direction} LIMIT ? OFFSET ?",[*args,page_size,(page-1)*page_size]).fetchall()
        return dict(items=[self.enrich(conn,model,row) for row in rows],total=total,page=page,page_size=page_size)

    def dashboard(self, conn):
        count = lambda sql, args=(): conn.execute(sql,args).fetchone()[0]
        stats = [
            {"label":"有效预约", "value":count("SELECT COUNT(*) FROM orders WHERE status='confirmed'"), "unit":"单"},
            {"label":"预约金额", "value":round(count("SELECT COALESCE(SUM(total),0) FROM orders WHERE status='confirmed'"),2), "unit":"元"},
            {"label":"球友用户", "value":count("SELECT COUNT(*) FROM users WHERE role='golfer'"), "unit":"人"},
            {"label":"待处理异常", "value":count("SELECT COUNT(*) FROM service_tickets WHERE status='open'"), "unit":"条"},
            {"label":"完成球局", "value":count("SELECT COUNT(*) FROM rounds WHERE complete=1"), "unit":"场"},
            {"label":"可安排球童", "value":count("SELECT COUNT(*) FROM caddies WHERE status='available'"), "unit":"人"},
        ]
        trend=[]
        for offset in range(-3, 11):
            date_value=day(offset)
            item = conn.execute("SELECT COUNT(*) AS orders, COALESCE(SUM(total),0) AS revenue FROM orders WHERE date=? AND status='confirmed'",(date_value,)).fetchone()
            trend.append({"date":date_value, "orders":item["orders"], "revenue":round(item["revenue"],2)})
        recent = [self.record(row) for row in conn.execute("SELECT o.*,u.name AS user_name,c.short AS course_name FROM orders o JOIN users u ON o.user_id=u.id JOIN courses c ON o.course_id=c.id ORDER BY o.updated_at DESC,o.rowid DESC LIMIT 8")]
        alerts = [self.record(row) for row in conn.execute("SELECT t.*,u.name AS user_name FROM service_tickets t JOIN users u ON t.user_id=u.id WHERE t.status='open' ORDER BY t.priority DESC,t.created_at DESC LIMIT 8")]
        return {"stats":stats,"trend":trend,"recent_orders":recent,"service_alerts":alerts,"data_notice":"预约金额按订单汇总，不代表已收款金额。"}

    def courses(self, conn, include_inactive=False):
        result=[]
        query="SELECT * FROM courses"+("" if include_inactive else " WHERE status='active'")+" ORDER BY price"
        for row in conn.execute(query):
            item=self.record(row)
            item["slots"]=[slot[0] for slot in conn.execute("SELECT DISTINCT time FROM slots WHERE course_id=? AND status='open' AND date>=? ORDER BY time",(row["id"],day()))]
            result.append(item)
        return result

    def lessons(self, conn):
        return [self.record(row) for row in conn.execute("SELECT * FROM lessons WHERE status='published' ORDER BY created_at,id")]

    def event_and_team(self, conn):
        event=conn.execute("SELECT * FROM events WHERE status='open' AND date>=? ORDER BY date,time LIMIT 1",(day(),)).fetchone()
        if not event:
            return {"event":None,"team":None}
        team=conn.execute("SELECT * FROM teams WHERE id=?",(event["team_id"],)).fetchone()
        return {"event":self.enrich(conn,"events",event),"team":self.record(team)}

    def blank_round(self):
        return {"id":uid("R"),"course":"qizhong" if self.test_fixtures else "","date":day(),"tee":"蓝Tee","players":["我"],"pk":"比杆赛","scores":[None]*18,"hole":0,"complete":False,"pending":False}

    def create_visitor(self, conn):
        visitor_id, user_id = secrets.token_urlsafe(24), uid("UV")
        self.insert(conn,"users",dict(id=user_id,name="演示球友·"+user_id[-6:],phone=None,role="golfer",status="active"),source="visitor")
        conn.execute("INSERT INTO visitors(id,user_id,state_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?)",(visitor_id,user_id,"{}",1,now(),now()))
        state=self.initialize_visitor(conn,visitor_id,user_id)
        conn.execute("UPDATE visitors SET state_json=? WHERE id=?",(dumps(state),visitor_id))
        self.audit(conn,"visitor:"+user_id,"initialize","visitors",user_id,{"source":"isolated demo"})
        return conn.execute("SELECT * FROM visitors WHERE id=?",(visitor_id,)).fetchone()

    def initialize_visitor(self, conn, visitor_id, user_id):
        state={"role":"golfer","favorites":["qizhong"],"joined":False,"reminders":True,"learned":[],"bag":{"orderId":"","stage":0,"clubs":[False]*4,"issues":[],"records":[]}}
        slot=conn.execute("SELECT s.* FROM slots s JOIN courses c ON c.id=s.course_id WHERE s.status='open' AND c.status='active' AND s.date>=? AND s.capacity-COALESCE((SELECT SUM(o.people) FROM orders o WHERE o.course_id=s.course_id AND o.date=s.date AND o.time=s.time AND o.status='confirmed'),0)>=2 ORDER BY CASE WHEN s.course_id='qizhong' THEN 0 ELSE 1 END,s.date,CASE WHEN s.time='08:00' THEN 0 ELSE 1 END,s.time LIMIT 1",(day(2),)).fetchone()
        if slot:
            order_id=uid("GE")
            self.insert(conn,"orders",dict(id=order_id,visitor_id=visitor_id,user_id=user_id,course_id=slot["course_id"],date=slot["date"],time=slot["time"],people=2,total=slot["price"]*2,status="confirmed",checked=False),source="visitor-demo")
            state["bag"]["orderId"]=order_id
        active=self.blank_round()
        active["scores"]=SAMPLE_SCORES[:6]+[None]*12
        active["hole"]=6
        state["round"]=active
        for value in (active,{**self.blank_round(),"date":day(-7),"scores":SAMPLE_SCORES,"complete":True,"hole":17}):
            self.insert(conn,"rounds",dict(id=value["id"],visitor_id=visitor_id,user_id=user_id,course_id=value["course"],date=value["date"],tee=value["tee"],players=value["players"],pk=value["pk"],scores=value["scores"],complete=value["complete"],total=sum(x or 0 for x in value["scores"]),hole=value["hole"],pending=False),source="visitor-demo")
        return state

    def project_state(self, conn, visitor):
        """以规范化业务表为准回填移动端；后台修订因此能在刷新后立即体现。"""
        state=json.loads(visitor["state_json"])
        visitor_id=visitor["id"]
        state["orders"]=[{"id":row["id"],"course":row["course_id"],"date":row["date"],"slot":row["time"],"people":row["people"],"total":row["total"],"status":row["status"],"checked":bool(row["checked"])} for row in conn.execute("SELECT * FROM orders WHERE visitor_id=? ORDER BY created_at DESC,rowid DESC",(visitor_id,))]
        rounds=[]
        for row in conn.execute("SELECT * FROM rounds WHERE visitor_id=? ORDER BY updated_at DESC,rowid DESC",(visitor_id,)):
            item=self.record(row)
            rounds.append({"id":item["id"],"course":item["course_id"],"date":item["date"],"tee":item["tee"],"players":item["players"],"pk":item["pk"],"scores":item["scores"],"complete":item["complete"],"hole":item["hole"],"pending":item["pending"]})
        active_id=state.get("round",{}).get("id")
        state["round"]=next((item for item in rounds if item["id"]==active_id),rounds[0] if rounds else self.blank_round())
        state["history"]=[item for item in rounds if item["complete"]]
        event=self.event_and_team(conn)["event"]
        state["eventId"]=event["id"] if event else None
        state["joined"]=bool(event and conn.execute("SELECT 1 FROM registrations WHERE visitor_id=? AND event_id=? AND status='registered'",(visitor_id,event["id"])).fetchone())
        bag=state.setdefault("bag",{})
        own_ids={item["id"] for item in state["orders"]}
        if bag.get("orderId") not in own_ids:
            bag={"orderId":next((item["id"] for item in state["orders"] if item["status"]=="confirmed"),state["orders"][0]["id"] if state["orders"] else ""),"stage":0,"clubs":[False]*4}
        order_id=bag.get("orderId","")
        rows=conn.execute("SELECT * FROM bag_records WHERE visitor_id=? AND order_id=? ORDER BY created_at,rowid",(visitor_id,order_id)).fetchall()
        bag["stage"]=2 if any(row["action"]=="归还球包" for row in rows) else 1 if any(row["action"]=="领取球包" for row in rows) else 0
        bag.setdefault("clubs",[False]*4)
        if bag["stage"] == 2:
            bag["clubs"]=[True]*4
        bag["issues"]=[{"id":row["id"],"type":row["type"],"text":row["description"],"time":row["reported_time"] or row["created_at"],"closed":row["status"]=="resolved"} for row in conn.execute("SELECT * FROM service_tickets WHERE visitor_id=? AND order_id=? ORDER BY created_at DESC,rowid DESC",(visitor_id,order_id))]
        bag["records"]=[{"id":row["id"],"orderId":row["order_id"],"action":row["action"],"time":row["record_time"],"staff":row["staff"],"bagCode":row["bag_code"]} for row in conn.execute("SELECT * FROM bag_records WHERE visitor_id=? ORDER BY created_at DESC,rowid DESC",(visitor_id,))]
        state["bag"]=bag
        state["_revision"]=visitor["revision"]
        if not self.test_fixtures:
            state["role"]=conn.execute("SELECT role FROM users WHERE id=?",(visitor["user_id"],)).fetchone()[0]
        state.setdefault("role","golfer")
        state.setdefault("reminders",True)
        state.setdefault("favorites",[])
        state.setdefault("learned",[])
        state["favorites"]=[ident for ident in state["favorites"] if conn.execute("SELECT 1 FROM courses WHERE id=?",(ident,)).fetchone()]
        state["learned"]=[ident for ident in state["learned"] if conn.execute("SELECT 1 FROM lessons WHERE id=?",(ident,)).fetchone()]
        return state

    def mobile_save(self, conn, visitor, state):
        if not isinstance(state,dict):
            raise APIError(400,"缺少有效的 state 对象。")
        if "_revision" in state and state["_revision"] != visitor["revision"]:
            raise APIError(409,"数据已由后台或另一页面更新，请刷新后继续操作。")
        visitor_id,user_id=visitor["id"],visitor["user_id"]
        actor=conn.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
        if not self.test_fixtures and state.get("role")!=actor["role"]:
            raise APIError(403,"不能自行更改账号角色。")
        if state.get("role") not in ("golfer","caddie","manager") or not isinstance(state.get("reminders"),bool):
            raise APIError(400,"角色或提醒设置无效。")
        for key,model in [("favorites","courses"),("learned","lessons")]:
            values=state.get(key,[])
            if not isinstance(values,list) or len(values)>100 or any(not isinstance(value,str) for value in values):
                raise APIError(400,"收藏和学习记录必须是编号数组。")
        orders=state.get("orders",[])
        history=state.get("history",[])
        if not isinstance(orders,list) or len(orders)>100 or not isinstance(history,list) or len(history)>200:
            raise APIError(400,"订单或历史球局数量无效。")
        seen=set()
        for item in orders:
            if not isinstance(item,dict) or not isinstance(item.get("id"),str) or item["id"] in seen:
                raise APIError(400,"订单编号缺失或重复。")
            seen.add(item["id"])
            current=conn.execute("SELECT * FROM orders WHERE id=?",(item["id"],)).fetchone()
            if current and current["visitor_id"] != visitor_id:
                raise APIError(403,"无权修改其他访客的订单。")
            if not self.test_fixtures and bool(item.get("checked"))!=bool(current["checked"] if current else False):
                raise APIError(403,"到场核验须由后台工作人员完成。")
            data=self.validate(conn,"orders",dict(id=item["id"],user_id=user_id,course_id=item.get("course"),date=item.get("date"),time=item.get("slot"),people=item.get("people"),status=item.get("status"),checked=item.get("checked",False)),current)
            changed=not current or any(data[key] != current[key] for key in ("course_id","date","time","people","status","checked"))
            self.write(conn,"orders",data,current,source="visitor",visitor_id=visitor_id)
            if changed:
                self.audit(conn,"visitor:"+user_id,"update" if current else "create","orders",data["id"],{"status":data["status"],"people":data["people"]})
        # 客户端遗漏的旧记录只取消，保留可审计的记录；不会硬删除业务数据。
        for row in conn.execute("SELECT * FROM orders WHERE visitor_id=? AND status='confirmed'",(visitor_id,)).fetchall():
            if row["id"] not in seen:
                cancelled=self.validate(conn,"orders",{"status":"cancelled"},row)
                self.write(conn,"orders",cancelled,row,source="visitor",visitor_id=visitor_id)
                self.audit(conn,"visitor:"+user_id,"cancel","orders",row["id"])
        current_round=state.get("round")
        if not isinstance(current_round,dict):
            raise APIError(400,"需要一条当前球局记录。")
        round_map={}
        for item in history + [current_round]:
            if not isinstance(item,dict) or not isinstance(item.get("id"),str):
                raise APIError(400,"球局编号无效。")
            round_map[item["id"]]=item
        for ident,item in round_map.items():
            if not self.test_fixtures and not item.get("course") and not any(x is not None for x in item.get("scores",[])):
                continue
            current=conn.execute("SELECT * FROM rounds WHERE id=?",(ident,)).fetchone()
            if current and current["visitor_id"] != visitor_id:
                raise APIError(403,"无权修改其他访客的球局。")
            data=self.validate(conn,"rounds",dict(id=ident,user_id=user_id,course_id=item.get("course"),date=item.get("date"),tee=item.get("tee"),players=item.get("players"),pk=item.get("pk"),scores=item.get("scores"),complete=item.get("complete",False)),current)
            hole=item.get("hole",17 if data["complete"] else 0)
            if isinstance(hole,bool) or not isinstance(hole,int) or not 0 <= hole <= 17 or not isinstance(item.get("pending",False),bool):
                raise APIError(400,"当前洞数或同步状态无效。")
            data["hole"],data["pending"]=hole,item.get("pending",False)
            self.write(conn,"rounds",data,current,source="visitor",visitor_id=visitor_id)
            if not current or any(current[key] != (dumps(data[key]) if key in JSON_FIELDS else int(data[key]) if isinstance(data[key],bool) else data[key]) for key in ("scores","complete","course_id","date","tee","players","pk")):
                self.audit(conn,"visitor:"+user_id,"update" if current else "create","rounds",data["id"],{"complete":data["complete"],"total":data["total"],"recorded_holes":sum(value is not None for value in data["scores"])})
        bag=state.get("bag")
        if not isinstance(bag,dict) or not isinstance(bag.get("clubs"),list) or len(bag["clubs"])!=4 or any(not isinstance(value,bool) for value in bag["clubs"]):
            raise APIError(400,"球包清点列表无效。")
        stage=bag.get("stage")
        if isinstance(stage,bool) or stage not in (0,1,2):
            raise APIError(400,"球包交接阶段无效。")
        order_id=bag.get("orderId","")
        order=conn.execute("SELECT * FROM orders WHERE id=? AND visitor_id=?",(order_id,visitor_id)).fetchone() if order_id else None
        if order_id and not order:
            raise APIError(403,"球包任务必须关联当前访客的订单。")
        issues=bag.get("issues",[])
        if not isinstance(issues,list) or len(issues)>100:
            raise APIError(400,"异常记录列表无效。")
        for issue in issues:
            if not isinstance(issue,dict) or not order:
                raise APIError(400,"异常记录需要关联预约。")
            current=conn.execute("SELECT * FROM service_tickets WHERE id=?",(issue.get("id",""),)).fetchone()
            if not current:
                current=conn.execute("SELECT * FROM service_tickets WHERE visitor_id=? AND order_id=? AND description=? AND type=? AND reported_time=?",(visitor_id,order_id,issue.get("text"),issue.get("type"),issue.get("time",""))).fetchone()
            if current and (current["visitor_id"] != visitor_id or current["order_id"] != order_id):
                raise APIError(403,"无权修改此异常记录。")
            if not isinstance(issue.get("text"),str) or len(issue["text"].strip())<5 or not isinstance(issue.get("closed",False),bool):
                raise APIError(400,"异常说明至少5个字，处理状态必须有效。")
            if not self.test_fixtures and bool(issue.get("closed"))!=bool(current and current["status"]=="resolved"):
                raise APIError(403,"服务问题须由后台工作人员处理。")
            data=self.validate(conn,"service_tickets",dict(id=current["id"] if current else issue.get("id"),order_id=order_id,user_id=user_id,caddie_id=current["caddie_id"] if current else None,type=issue.get("type"),description=issue.get("text"),priority=current["priority"] if current else "normal",status="resolved" if issue.get("closed") else "open",reported_time=issue.get("time",now())),current)
            self.write(conn,"service_tickets",data,current,source="visitor",visitor_id=visitor_id)
            if not current or current["status"] != data["status"]:
                self.audit(conn,"visitor:"+user_id,"update" if current else "create","service_tickets",data["id"],{"status":data["status"]})
        stored_records=conn.execute("SELECT action FROM bag_records WHERE visitor_id=? AND order_id=?",(visitor_id,order_id)).fetchall()
        old_stage=2 if any(row["action"]=="归还球包" for row in stored_records) else 1 if stored_records else 0
        prior_bag=json.loads(visitor["state_json"]).get("bag",{})
        if prior_bag.get("orderId") != order_id and stage==0:
            # 切换服务组时恢复该订单真实交接阶段，避免前端重置为0覆盖旧服务记录。
            stage=old_stage
        if stage<old_stage or stage>old_stage+1:
            raise APIError(409,"球包交接必须按领取、归还顺序操作，已完成记录不能撤销。")
        if stage>old_stage:
            if not self.test_fixtures:raise APIError(403,"球包交接须由后台工作人员登记。")
            action="领取球包" if stage==1 else "归还球包"
            # 前端领取后会将复选项清空，为归还重新清点；新记录是此前已完成清点的确认结果。
            data=self.validate(conn,"bag_records",dict(order_id=order_id,user_id=user_id,bag_code="B-028",action=action,clubs=[True]*4,staff="球童小林",record_time=now(),notes="访客确认的模拟交接"))
            self.write(conn,"bag_records",data,source="visitor",visitor_id=visitor_id)
            self.audit(conn,"visitor:"+user_id,"create","bag_records",data["id"],{"action":action})
        joined=state.get("joined",False)
        if not isinstance(joined,bool):
            raise APIError(400,"报名状态无效。")
        event=self.event_and_team(conn)["event"]
        event_id=event["id"] if event else ""
        registration=conn.execute("SELECT * FROM registrations WHERE visitor_id=? AND event_id=? ORDER BY created_at DESC LIMIT 1",(visitor_id,event_id)).fetchone()
        if joined and (not event or state.get("eventId")!=event_id):raise APIError(409,"活动已变化，请刷新后重新确认。")
        if joined or registration:
            data=self.validate(conn,"registrations",dict(id=registration["id"] if registration else None,event_id=event_id,user_id=user_id,people=1,status="registered" if joined else "cancelled"),registration)
            self.write(conn,"registrations",data,registration,source="visitor",visitor_id=visitor_id)
            if not registration or registration["status"] != data["status"]:
                self.audit(conn,"visitor:"+user_id,"update" if registration else "create","registrations",data["id"],{"status":data["status"]})
        favorites=[value for value in dict.fromkeys(state.get("favorites",[])) if conn.execute("SELECT 1 FROM courses WHERE id=?",(value,)).fetchone()]
        learned=[value for value in dict.fromkeys(state.get("learned",[])) if conn.execute("SELECT 1 FROM lessons WHERE id=?",(value,)).fetchone()]
        auxiliary={"role":state["role"],"favorites":favorites,"reminders":state["reminders"],"learned":learned,"round":{"id":current_round["id"]},"bag":{"orderId":order_id,"clubs":bag["clubs"]}}
        if "feedback" in state:
            auxiliary["feedback"]=str(state["feedback"])[:200]
        conn.execute("UPDATE visitors SET state_json=?,revision=revision+1,updated_at=? WHERE id=?",(dumps(auxiliary),now(),visitor_id))
        visitor=conn.execute("SELECT * FROM visitors WHERE id=?",(visitor_id,)).fetchone()
        return self.project_state(conn,visitor)

    def mobile_reset(self, conn, visitor):
        visitor_id,user_id=visitor["id"],visitor["user_id"]
        conn.execute("DELETE FROM ai_records WHERE owner_id=?",(visitor_id,))
        # 取消当前访客预约并保留操作审计；球局/服务样例删除仅属于显式的演示重置。
        conn.execute("DELETE FROM service_tickets WHERE visitor_id=?",(visitor_id,))
        conn.execute("DELETE FROM bag_records WHERE visitor_id=?",(visitor_id,))
        conn.execute("DELETE FROM registrations WHERE visitor_id=?",(visitor_id,))
        conn.execute("UPDATE orders SET status='cancelled',checked=0,updated_at=? WHERE visitor_id=?",(now(),visitor_id))
        conn.execute("DELETE FROM rounds WHERE visitor_id=?",(visitor_id,))
        state=self.initialize_visitor(conn,visitor_id,user_id)
        conn.execute("UPDATE visitors SET state_json=?,revision=revision+1,updated_at=? WHERE id=?",(dumps(state),now(),visitor_id))
        self.audit(conn,"visitor:"+user_id,"reset","visitors",user_id,{"note":"explicit demo reset; old orders retained cancelled"})
        return self.project_state(conn,conn.execute("SELECT * FROM visitors WHERE id=?",(visitor_id,)).fetchone())


class LoginLimiter:
    """按来源地址与用户名限制失败尝试，锁保证并发登录不能绕过计数。"""
    def __init__(self):
        self.attempts={}
        self.lock=threading.Lock()

    def check(self, key):
        with self.lock:
            recent=[stamp for stamp in self.attempts.get(key,[]) if stamp>time.time()-600]
            self.attempts[key]=recent
            if len(recent)>=8:
                raise APIError(429,"登录失败次数过多，请10分钟后重试。")

    def fail(self, key):
        with self.lock:
            self.attempts.setdefault(key,[]).append(time.time())
            if len(self.attempts)>5000:
                cutoff=time.time()-600
                self.attempts={ident:[stamp for stamp in times if stamp>cutoff] for ident,times in self.attempts.items() if times and times[-1]>cutoff}

    def clear(self,key):
        with self.lock:
            self.attempts.pop(key,None)


class GolfServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True

    def __init__(self, address, database, static_dir):
        self.database=database
        self.static_dir=Path(static_dir).resolve()
        self.login_limiter=LoginLimiter()
        self.started=time.time()
        self.secure_cookie=os.environ.get("GOLFERA_COOKIE_SECURE","").lower() in ("1","true","yes")
        super().__init__(address, Handler)
        with database.connection() as conn:
            with database.transaction(conn):
                legacy_usage={}
                for r in ai_features.rows(conn,'run'):
                    usage=legacy_usage.setdefault(r['created_at'][:10],{'requests':0,'tokens':0})
                    usage['requests']+=1
                    usage['tokens']+=r['data'].get('usage',{}).get('total_tokens',0)
                    if r['data']['status'] in ('queued','running'):
                        data=r['data'];data.update(status='failed',error='服务重启中断了生成，请重新提交。')
                        ai_features.put(conn,r['owner_id'],'run',data,r['id'])
                for usage_day,usage in legacy_usage.items():
                    conn.execute('INSERT OR IGNORE INTO ai_daily_usage(date,requests,tokens) VALUES(?,?,?)',(usage_day,usage['requests'],usage['tokens']))


class Handler(BaseHTTPRequestHandler):
    server_version="Golfera/1.0"
    sys_version=""

    def request_ip(self):
        if os.environ.get('GOLFERA_TRUST_PROXY')=='1' and self.client_address[0] in ('127.0.0.1','::1'):
            try:return str(ipaddress.ip_address(self.headers.get('X-Real-IP','')))
            except ValueError:pass
        return self.client_address[0]

    def log_message(self, fmt, *args):
        # 记录方法、路径、状态；不输出请求正文、凭据、Cookie 或完整查询参数。
        LOG.info("%s %s %s",self.client_address[0],getattr(self,"command","?"),urlsplit(getattr(self,"path","/")).path)

    def headers_common(self, content_type, length, cache="no-store"):
        self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(length))
        self.send_header("Cache-Control",cache)
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("X-Frame-Options","SAMEORIGIN")
        self.send_header("Referrer-Policy","same-origin")
        self.send_header("Content-Security-Policy","default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'self'; base-uri 'self'; form-action 'self'")
        for cookie in getattr(self,"response_cookies",[]):
            self.send_header("Set-Cookie",cookie)
        self.end_headers()

    def respond(self, status, data=None, message=None):
        payload={"ok":False,"error":{"message":message}} if message else {"ok":True,"data":data}
        body=dumps(payload).encode("utf-8")
        self.send_response(status)
        self.headers_common("application/json; charset=utf-8",len(body))
        if self.command!="HEAD":
            self.wfile.write(body)

    def cookie(self, name):
        try:
            parsed=SimpleCookie()
            parsed.load(self.headers.get("Cookie",""))
            return parsed[name].value if name in parsed else None
        except Exception:
            return None

    def set_cookie(self,name,value,max_age):
        attributes=f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        if self.server.secure_cookie:
            attributes+="; Secure"
        self.response_cookies.append(attributes)

    def body_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400,"不支持分块上传。")
        if self.headers.get("Content-Type","").split(";",1)[0].strip().lower()!="application/json":
            raise APIError(415,"请使用 application/json 提交数据。")
        try:
            length=int(self.headers.get("Content-Length","0"))
        except ValueError:
            raise APIError(400,"请求长度无效。")
        limit=12*1024*1024 if self.route_path in ('/api/mobile/ai/video','/api/mobile/ai/transcription') else MAX_BODY
        if length<0 or length>limit:
            raise APIError(413,"提交的数据过大，请缩小后重试。")
        try:
            value=json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value,dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise APIError(400,"请求正文必须是有效的 JSON 对象。")

    def require_origin(self):
        host=self.headers.get("Host","").lower()
        if not host or any(character in host for character in " /\\\t\r\n"):
            raise APIError(400,"Host 请求头无效。")
        origin=self.headers.get("Origin")
        referer=self.headers.get("Referer")
        if origin:
            parsed=urlsplit(origin)
            if parsed.scheme not in ("http","https") or parsed.netloc.lower()!=host:
                raise APIError(403,"仅允许同源页面提交修改。")
        elif referer:
            parsed=urlsplit(referer)
            if parsed.netloc.lower()!=host or parsed.scheme not in ("http","https"):
                raise APIError(403,"仅允许同源页面提交修改。")
        if self.headers.get("Sec-Fetch-Site","").lower() in ("cross-site","same-site"):
            raise APIError(403,"跨站修改已被拒绝。")

    def admin(self, conn):
        token=self.cookie("golfera_admin")
        if not token:
            raise APIError(401,"请先登录管理员后台。")
        token_hash=hashlib.sha256(token.encode()).hexdigest()
        row=conn.execute("SELECT a.id,a.username,s.token_hash FROM sessions s JOIN admins a ON s.admin_id=a.id WHERE s.token_hash=? AND s.expires>?",(token_hash,time.time())).fetchone()
        if not row:
            raise APIError(401,"登录已失效，请重新登录。")
        return dict(row)

    def visitor(self, conn, create=False):
        import user_accounts
        user=user_accounts.identity(self,conn)
        if user:
            row=conn.execute("SELECT * FROM visitors WHERE user_id=?",(user['id'],)).fetchone()
            if not row:raise APIError(401,"账号工作区不存在，请联系管理员。")
            return row
        if not self.server.database.test_fixtures:
            raise APIError(401,"请登录后继续。")
        token=self.cookie("golfera_visitor")
        row=conn.execute("SELECT * FROM visitors WHERE id=?",(token,)).fetchone() if token else None
        if row and conn.execute("SELECT user_id FROM user_accounts WHERE user_id=?",(row['user_id'],)).fetchone():
            raise APIError(401,"请使用账号密码登录。")
        if not row:
            if not create:
                raise APIError(401,"请先打开移动端页面，建立演示访客。")
            row=self.server.database.create_visitor(conn)
            self.set_cookie("golfera_visitor",row["id"],365*24*60*60)
        return row

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_PATCH(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def do_OPTIONS(self):
        self.respond(405,message="不允许跨站 API 访问。")

    def dispatch(self):
        self.response_cookies=[]
        parsed=urlsplit(self.path)
        path=unquote(parsed.path)
        self.route_path=path.rstrip("/") or "/"
        self.params=parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                if self.command not in ("GET","HEAD"):
                    self.require_origin()
                with self.server.database.connection() as conn:
                    self.api(conn)
            elif self.command in ("GET","HEAD"):
                self.static(path)
            else:
                raise APIError(405,"此地址不支持该请求方法。")
        except (APIError, ai_service.AIError) as error:
            self.respond(error.status,message=error.message)
        except self.server.database.integrity_errors as error:
            text=str(error).upper()
            message="记录仍被其他业务引用，不能删除或更改关联。" if "FOREIGN KEY" in text else "记录编号、手机号、时段或报名已存在，请检查重复资料。" if "UNIQUE" in text or "DUPLICATE ENTRY" in text else "数据不符合业务规则，请检查输入。"
            self.respond(409,message=message)
        except (BrokenPipeError,ConnectionResetError):
            LOG.info("客户端提前断开连接。")
        except Exception:
            LOG.exception("请求处理失败: %s %s",self.command,self.route_path)
            self.respond(500,message="服务器暂时无法完成操作，请稍后重试。")

    def api(self, conn):
        db=self.server.database
        path,method=self.route_path,self.command
        if method=="HEAD":
            method="GET"
        if path=="/api/health" and method=="GET":
            conn.execute("SELECT 1").fetchone()
            return self.respond(200,{"service":"golfera","database":"MySQL" if db.backend=="mysql" else "SQLite","ready":True,"time":now(),"uptime_seconds":int(time.time()-self.server.started)})
        if path=='/api/public/app' or path.startswith('/api/public/app/'):
            import app_release
            return app_release.public(self,method,path)
        if path.startswith('/api/user/'):
            import user_accounts
            return user_accounts.api(self,conn,path,method)
        if path.startswith("/api/auth/"):
            return self.auth_api(conn,path,method)
        if path=="/api/public/catalog" and method=="GET":
            selected=db.event_and_team(conn)
            event=selected["event"];team=selected["team"]
            if not team or team["status"]!="active":event=None;team=None
            return self.respond(200,{
                "courses":db.courses(conn),"lessons":db.lessons(conn),
                "event":{key:event[key] for key in ("id","title","course_id","date","time","capacity","fee","description","status","registered") if key in event} if event else None,
                "team":{key:team[key] for key in ("name","description","member_count","city") if key in team} if team and team["status"]=="active" else None})
        if path=="/api/public/courses" and method=="GET":
            return self.respond(200,db.courses(conn))
        if path=="/api/public/slots" and method=="GET":
            course_id=self.params.get("course_id",[""])[0]
            date_value=self.params.get("date",[day()])[0]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",date_value) or not conn.execute("SELECT 1 FROM courses WHERE id=?",(course_id,)).fetchone():
                raise APIError(400,"请提供有效的球场与日期。")
            rows=conn.execute("SELECT * FROM slots WHERE course_id=? AND date=? ORDER BY time",(course_id,date_value)).fetchall()
            return self.respond(200,[db.enrich(conn,"slots",row) for row in rows])
        if path=="/api/mobile/state" and method=="GET":
            with db.transaction(conn):
                visitor=self.visitor(conn,create=True)
                state=db.project_state(conn,visitor)
            return self.respond(200,{"state":state,"courses":db.courses(conn,include_inactive=True),"lessons":db.lessons(conn),**db.event_and_team(conn)})
        if path=="/api/mobile/state" and method=="PUT":
            body=self.body_json()
            with db.transaction(conn):
                visitor=self.visitor(conn)
                state=db.mobile_save(conn,visitor,body.get("state"))
            return self.respond(200,{"state":state,**db.event_and_team(conn)})
        if path=="/api/mobile/reset" and method=="POST":
            self.body_json()
            if not db.test_fixtures:raise APIError(403,"正式账号不支持重置业务记录。")
            with db.transaction(conn):
                visitor=self.visitor(conn)
                if conn.execute('SELECT user_id FROM user_accounts WHERE user_id=?',(visitor['user_id'],)).fetchone():raise APIError(403,'登录账号不能重置为演示资料。')
                state=db.mobile_reset(conn,visitor)
            return self.respond(200,{"state":state,"courses":db.courses(conn,include_inactive=True),"lessons":db.lessons(conn),**db.event_and_team(conn)})
        if path=="/api/mobile/ai" or path.startswith("/api/mobile/ai/"):
            visitor=self.visitor(conn)
            return ai_features.mobile(self,conn,visitor,path,method)
        if path.startswith("/api/admin/"):
            admin=self.admin(conn)
            return self.admin_api(conn,admin,path,method)
        raise APIError(404,"接口不存在或请求方法不正确。")

    def auth_api(self,conn,path,method):
        db=self.server.database
        if path=="/api/auth/login" and method=="POST":
            body=self.body_json()
            username,password=body.get("username",""),body.get("password","")
            if not isinstance(username,str) or not isinstance(password,str) or len(username)>100 or len(password)>256:
                raise APIError(400,"登录内容无效。")
            key=(self.request_ip(),username)
            self.server.login_limiter.check(key)
            row=conn.execute("SELECT * FROM admins WHERE username=?",(username,)).fetchone()
            encoded=row["password_hash"] if row else "pbkdf2_sha256$260000$unknown$"+"0"*64
            if not password_ok(password,encoded) or not row:
                self.server.login_limiter.fail(key)
                raise APIError(401,"账号或密码不正确。")
            self.server.login_limiter.clear(key)
            token=secrets.token_urlsafe(32)
            with db.transaction(conn):
                conn.execute("DELETE FROM sessions WHERE expires<?",(time.time(),))
                conn.execute("INSERT INTO sessions(token_hash,admin_id,expires) VALUES(?,?,?)",(hashlib.sha256(token.encode()).hexdigest(),row["id"],time.time()+SESSION_TTL))
                db.audit(conn,username,"login","auth",row["id"])
            self.set_cookie("golfera_admin",token,SESSION_TTL)
            return self.respond(200,{"user":{"id":row["id"],"username":username,"name":"系统管理员","role":"admin"}})
        if path=="/api/auth/me" and method=="GET":
            admin=self.admin(conn)
            return self.respond(200,{"user":{"id":admin["id"],"username":admin["username"],"name":"系统管理员","role":"admin"}})
        if path=="/api/auth/logout" and method=="POST":
            self.body_json()
            token=self.cookie("golfera_admin")
            if token:
                with db.transaction(conn):
                    conn.execute("DELETE FROM sessions WHERE token_hash=?",(hashlib.sha256(token.encode()).hexdigest(),))
            self.set_cookie("golfera_admin","",0)
            return self.respond(200,{"logged_out":True})
        if path=="/api/auth/password" and method=="PATCH":
            admin=self.admin(conn)
            body=self.body_json()
            old,new=body.get("old_password"),body.get("new_password")
            if not isinstance(old,str) or not isinstance(new,str) or not 12<=len(new)<=256:
                raise APIError(400,"新密码至少12位，最多256位。")
            record=conn.execute("SELECT * FROM admins WHERE id=?",(admin["id"],)).fetchone()
            if not password_ok(old,record["password_hash"]):
                raise APIError(400,"原密码不正确。")
            with db.transaction(conn):
                conn.execute("UPDATE admins SET password_hash=? WHERE id=?",(password_hash(new),admin["id"]))
                conn.execute("DELETE FROM sessions WHERE admin_id=? AND token_hash<>?",(admin["id"],admin["token_hash"]))
                db.audit(conn,admin["username"],"password_change","auth",admin["id"])
            return self.respond(200,{"updated":True})
        raise APIError(404,"登录接口不存在或请求方法不正确。")

    def admin_api(self,conn,admin,path,method):
        db=self.server.database
        parts=path.strip("/").split("/")[2:]
        if not parts:
            raise APIError(404,"管理接口不存在。")
        if parts[0]=='app':
            import app_release
            return app_release.admin(self,conn,admin,path,method)
        if parts==['accounts']:
            import user_accounts
            return user_accounts.admin(self,conn,admin,method)
        if parts and parts[0]=="ai" and len(parts)>1 and parts[1]!="test":
            return ai_features.admin(self,conn,admin,parts[1:],method)
        if parts==["ai"] and method=="GET":
            return self.respond(200,ai_service.public())
        if parts==["ai"] and method=="PATCH":
            result=ai_service.save(self.body_json())
            with db.transaction(conn):
                db.audit(conn,admin["username"],"update","ai","config",{"model":result["default_model"],"enabled":result["enabled"]})
            return self.respond(200,result)
        if parts==["ai","test"] and method=="POST":
            body=self.body_json()
            result=ai_features.run(self,conn,"admin:"+admin["id"],"test","模型连通测试",{},executor=lambda messages:ai_service.complete([{"role":"user","content":"请用中文简短介绍你能如何帮助高尔夫球友。"}],self.request_ip(),body.get("model"),test=True))
            with db.transaction(conn):
                db.audit(conn,admin["username"],"test","ai",result["model"],{"elapsed_ms":result["elapsed_ms"]})
            return self.respond(200,result)
        if parts[0]=="schema" and len(parts)==1 and method=="GET":
            return self.respond(200,{"models":MODELS,"data_notice":"业务记录保存到数据库，管理操作记入审计日志。","currency":"CNY","timezone":"Asia/Shanghai"})
        if parts[0]=="dashboard" and len(parts)==1 and method=="GET":
            return self.respond(200,db.dashboard(conn))
        if parts[0]=="settings" and len(parts)==1:
            if method=="GET":
                return self.respond(200,{row["key"]:row["value"] for row in conn.execute("SELECT * FROM settings")})
            if method=="PATCH":
                data=self.body_json()
                allowed={row["key"] for row in conn.execute("SELECT key FROM settings")}
                if set(data)-allowed or any(not isinstance(value,str) or len(value)>2000 for value in data.values()):
                    raise APIError(400,"设置项无效或文字过长。")
                with db.transaction(conn):
                    for key,value in data.items():
                        conn.execute("UPDATE settings SET value=?,updated_at=? WHERE key=?",(value.strip(),now(),key))
                    db.audit(conn,admin["username"],"update","settings","site",{"keys":list(data)})
                return self.respond(200,{row["key"]:row["value"] for row in conn.execute("SELECT * FROM settings")})
        if parts[0]=="seed" and len(parts)==1 and method=="POST":
            if not db.test_fixtures:raise APIError(403,"正式站点不提供样例初始化。")
            self.body_json()
            with db.transaction(conn):
                changes=db.seed(conn)
                db.audit(conn,admin["username"],"seed","database","demo",{"inserted":changes})
            return self.respond(200,{"inserted":changes,"message":"已幂等补齐模拟资料，现有记录与管理员修改均已保留。"})
        if parts[0]=="export" and len(parts)==2 and method=="GET":
            model=parts[1]
            if model not in MODELS:
                raise APIError(404,"导出模型不存在。")
            rows=[db.enrich(conn,model,row) for row in conn.execute(f"SELECT * FROM {model} ORDER BY created_at DESC")]
            columns=list(rows[0]) if rows else MODELS[model]["columns"]
            buffer=io.StringIO()
            writer=csv.DictWriter(buffer,fieldnames=columns)
            writer.writeheader()
            for row in rows:
                escaped={}
                for key,value in row.items():
                    text=dumps(value) if isinstance(value,(list,dict)) else str(value) if value is not None else ""
                    # CSV 导入电子表格时，用户输入不能变成公式。
                    escaped[key]="'"+text if text.startswith(("=","+","-","@","\t","\r")) else text
                writer.writerow(escaped)
            body=("\ufeff"+buffer.getvalue()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Disposition",f'attachment; filename="golfera-{model}-{day()}.csv"')
            self.headers_common("text/csv; charset=utf-8",len(body))
            if self.command!="HEAD":
                self.wfile.write(body)
            return
        model=parts[0]
        if model not in MODELS or len(parts)>2:
            raise APIError(404,"管理数据模型不存在。")
        ident=parts[1] if len(parts)==2 else None
        current=conn.execute(f"SELECT * FROM {model} WHERE id=?",(ident,)).fetchone() if ident else None
        if ident and not current:
            raise APIError(404,"记录不存在。")
        if method=="GET":
            return self.respond(200,db.enrich(conn,model,current) if ident else db.list_records(conn,model,self.params))
        if not MODELS[model]["editable"]:
            raise APIError(403,"审计日志只读，不能新增、修改或删除。")
        if method=="DELETE" and ident:
            with db.transaction(conn):
                conn.execute(f"DELETE FROM {model} WHERE id=?",(ident,))
                db.bump_visitor(conn,current["visitor_id"] if "visitor_id" in current.keys() else None)
                db.audit(conn,admin["username"],"delete",model,ident)
            return self.respond(200,{"deleted":True,"id":ident})
        if method=="POST" and not ident or method=="PATCH" and ident:
            body=self.body_json()
            with db.transaction(conn):
                # 在事务内重读，避免并发管理修改绕过库存验证。
                current=conn.execute(f"SELECT * FROM {model} WHERE id=?",(ident,)).fetchone() if ident else None
                if ident and not current:
                    raise APIError(404,"记录已被删除。")
                data=db.validate(conn,model,body,current)
                visitor_id=None
                if "user_id" in data and model in {"orders","rounds","registrations","service_tickets","bag_records"}:
                    visitor=conn.execute("SELECT id FROM visitors WHERE user_id=?",(data["user_id"],)).fetchone()
                    visitor_id=visitor["id"] if visitor else None
                    if current and current["visitor_id"] and visitor_id!=current["visitor_id"]:
                        raise APIError(409,"访客记录不能改配给其他用户。")
                if model in ("service_tickets","bag_records") and data.get("order_id"):
                    order=conn.execute("SELECT user_id,visitor_id FROM orders WHERE id=?",(data["order_id"],)).fetchone()
                    if data["user_id"] != order["user_id"]:
                        raise APIError(400,"球包/服务记录的球友必须与订单一致。")
                    visitor_id=order["visitor_id"]
                record_id=db.write(conn,model,data,current,visitor_id=visitor_id)
                db.bump_visitor(conn,visitor_id)
                db.audit(conn,admin["username"],"update" if current else "create",model,record_id,{"fields":list(body)})
                record=conn.execute(f"SELECT * FROM {model} WHERE id=?",(record_id,)).fetchone()
            return self.respond(200 if current else 201,db.enrich(conn,model,record))
        raise APIError(405,"此管理接口不支持该请求方法。")

    def static(self,path):
        if "\x00" in path or any(segment in ("..",".") for segment in path.split("/")):
            raise APIError(403,"无效的资源路径。")
        relative=path.lstrip("/")
        target=(self.server.static_dir/relative).resolve()
        if not target.is_relative_to(self.server.static_dir):
            raise APIError(403,"不能访问网站目录以外的资源。")
        if target.is_dir():
            # 目录入口必须有尾斜杠，避免浏览器把相对资源解析到父目录。
            if not path.endswith("/"):
                query=urlsplit(self.path).query
                self.send_response(302)
                self.send_header("Location",path+"/"+("?"+query if query else ""))
                self.headers_common("text/html; charset=utf-8",0)
                return
            target=target/"index.html"
        if not target.is_file():
            raise APIError(404,"页面或资源不存在。")
        if target.suffix.lower() not in {".html",".css",".js",".json",".jpg",".jpeg",".png",".svg",".webp",".ico",".woff",".woff2",".txt"}:
            raise APIError(403,"该资源不允许公开访问。")
        body=target.read_bytes()
        content_type=mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix in {".html",".css",".js",".json",".txt",".svg"}:
            content_type+="; charset=utf-8"
        self.send_response(200)
        self.headers_common(content_type,len(body),"no-cache" if target.suffix in {".html",".js",".css",".json"} else "public, max-age=3600")
        if self.command!="HEAD":
            self.wfile.write(body)


def main():
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    database=Database()
    host=os.environ.get("HOST","0.0.0.0")
    port=int(os.environ.get("PORT","8787"))
    server=GolfServer((host,port),database,os.environ.get("GOLFERA_STATIC_DIR",str(BASE/"dist")))
    LOG.info("高尔夫AI工具演示服务启动，地址 %s:%s；使用 %s 持久化。",host,port,database.backend)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("服务已停止。")
    finally:
        server.server_close()


if __name__=="__main__":
    main()

