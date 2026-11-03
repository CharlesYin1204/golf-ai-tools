"""MySQL 5.7/8.0 的小型兼容层；业务验证仍使用 server.py 的同一份实现。

PyMySQL 按需导入，SQLite 模式不需要第三方依赖。标识符只来自固定建表规格，
用户输入始终通过参数传入。MySQL 保留 InnoDB 外键与有效记录的唯一约束。
"""
import re
from decimal import Decimal


def split_definitions(text):
    parts, start, depth, quote = [], 0, 0, None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in ("'", '"', "`"):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return parts


def table_definitions(sqlite_schema):
    """固定 SQLite 规格的表名、列定义；供建表及完整迁移共用。"""
    return [(match.group(1), split_definitions(match.group(2))) for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS (\w+)\((.*?)\);", sqlite_schema, re.S)]


def table_columns(sqlite_schema):
    return {name: [definition.split()[0] for definition in definitions
                  if not definition.startswith(("FOREIGN KEY", "UNIQUE", "CHECK"))]
            for name, definitions in table_definitions(sqlite_schema)}


def mysql_schema(sqlite_schema, models):
    """转换固定规格；TEXT 索引、内联 FK 和部分唯一索引改为 MySQL 的等价约束。"""
    statements = []
    id_columns = {"id", "visitor_id", "user_id", "admin_id", "course_id", "team_id",
                  "captain_id", "event_id", "order_id", "caddie_id", "record_id", "owner_id", "token_hash"}
    long_columns = {"data_json", "state_json", "tags", "skills", "players", "scores", "steps", "clubs", "details"}
    for name, definitions in table_definitions(sqlite_schema):
        converted, foreign = [], []
        for definition in definitions:
            if definition.startswith("FOREIGN KEY"):
                # 所有列名/表名来自固定 SCHEMA，此处不使用用户值拼接 SQL。
                converted.append(definition)
                continue
            if definition.startswith("UNIQUE"):
                converted.append(definition)
                continue
            key, rest = definition.split(" ", 1)
            reference = re.search(r"\s+REFERENCES (\w+)\((\w+)\)", rest)
            if reference:
                foreign.append(f"FOREIGN KEY (`{key}`) REFERENCES `{reference.group(1)}`(`{reference.group(2)}`)")
                rest = re.sub(r"\s+REFERENCES \w+\(\w+\)", "", rest)
            # MySQL 5.7 不执行 CHECK；同样的约束已在 API validator 中明确执行。
            rest = re.sub(r"\s+CHECK\(.*\)$", "", rest)
            if rest.startswith("TEXT"):
                if key in id_columns:
                    datatype = "VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin"
                elif key == "key":
                    datatype = "VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin"
                elif key == "phone":
                    datatype = "VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin"
                elif key in long_columns:
                    datatype = "LONGTEXT"
                elif key in {"description", "notes", "value"}:
                    datatype = "TEXT"
                elif key == "password_hash":
                    datatype = "VARCHAR(255)"
                elif key == "date":
                    datatype = "CHAR(10) CHARACTER SET ascii COLLATE ascii_bin"
                elif key == "time":
                    datatype = "CHAR(5) CHARACTER SET ascii COLLATE ascii_bin"
                elif key in {"created_at", "updated_at"}:
                    datatype = "VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin"
                else:
                    datatype = "VARCHAR(300)"
                rest = datatype + rest[4:]
                if datatype in {"TEXT", "LONGTEXT"}:
                    rest = re.sub(r" DEFAULT '[^']*'", "", rest)
            elif rest.startswith("REAL"):
                rest = ("DOUBLE" if key == "expires" else "DECIMAL(14,2)") + rest[4:]
            converted.append(f"`{key}` {rest}")
        if name in models:
            converted.append("`_seq` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE")
        if name == "orders":
            converted += [
                "`_active_visitor` VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin GENERATED ALWAYS AS (IF(status='confirmed',visitor_id,NULL)) STORED",
                "UNIQUE KEY orders_visitor_unique (_active_visitor,course_id,date,time)",
                "KEY orders_inventory (course_id,date,time,status)",
            ]
        if name == "registrations":
            converted += [
                "`_active_user` VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin GENERATED ALWAYS AS (IF(status='registered',user_id,NULL)) STORED",
                "UNIQUE KEY registrations_unique (event_id,_active_user)",
            ]
        if name == "audit_logs":
            converted.append("KEY audit_created (created_at)")
        converted.extend(foreign)
        statements.append(f"CREATE TABLE IF NOT EXISTS `{name}` ({','.join(converted)}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
    statements.append("CREATE TABLE IF NOT EXISTS `_write_guard` (`id` TINYINT PRIMARY KEY) ENGINE=InnoDB")
    return statements


def compile_sql(sql):
    """只转换引号外的占位符/保留字/方言；字符串常量不会被误改。"""
    if re.match(r"\s*(CREATE|ALTER|DROP)\b",sql,re.I):
        # 建表规格已经是 MySQL 方言；PRIMARY/UNIQUE KEY 属于语法，不能改成列名。
        return sql
    chunks = re.split(r"('(?:[^']|'')*'|`[^`]*`)", sql)
    for index in range(0, len(chunks), 2):
        text = chunks[index].replace("?", "%s")
        text = re.sub(r"\browid\b", "_seq", text, flags=re.I)
        text = re.sub(r"\bAS TEXT\b", "AS CHAR", text, flags=re.I)
        text = re.sub(r"\bkey\b", "`key`", text, flags=re.I)
        chunks[index] = text
    return "".join(chunks)


class Row(dict):
    """支持 sqlite3.Row 的数字索引和字典访问，DECIMAL 转为既有 JSON 数字接口。"""
    def __init__(self, keys, values):
        values = tuple(float(value) if isinstance(value, Decimal) else value for value in values)
        super().__init__(zip(keys, values))
        self.values_tuple = values

    def __getitem__(self, key):
        return self.values_tuple[key] if isinstance(key, int) else super().__getitem__(key)


class Result:
    def __init__(self, cursor=None, rows=None):
        self.keys = tuple(column[0] for column in cursor.description) if cursor and cursor.description else ()
        self.rows = list(cursor.fetchall()) if cursor and cursor.description else list(rows or [])
        self.index = 0
        self.rowcount = cursor.rowcount if cursor else 0
        if cursor:
            cursor.close()

    def fetchone(self):
        if self.index >= len(self.rows):
            return None
        row = self.rows[self.index]
        self.index += 1
        return row if isinstance(row, Row) else Row(self.keys, row)

    def fetchall(self):
        result = [row if isinstance(row, Row) else Row(self.keys, row) for row in self.rows[self.index:]]
        self.index = len(self.rows)
        return result

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row


class Connection:
    def __init__(self, raw, driver):
        self.raw, self.driver = raw, driver
        self.total_changes = 0
        self.in_transaction = False

    def execute(self, sql, args=()):
        pragma = re.fullmatch(r"PRAGMA table_info\((\w+)\)", sql.strip(), re.I)
        if pragma:
            with self.raw.cursor() as cursor:
                cursor.execute(f"SHOW COLUMNS FROM `{pragma.group(1)}`")
                rows = [Row(("name",), (row[0],)) for row in cursor.fetchall() if not row[0].startswith("_")]
            return Result(rows=rows)
        ignored = bool(re.match(r"INSERT OR IGNORE\b", sql, re.I))
        sql = re.sub(r"^INSERT OR IGNORE\b", "INSERT", sql, flags=re.I)
        cursor = self.raw.cursor()
        try:
            cursor.execute(compile_sql(sql), tuple(args) if args else None)
        except self.driver.IntegrityError as error:
            cursor.close()
            # 不使用 INSERT IGNORE，以免悄悄跳过外键失败或截断输入。
            if ignored and error.args[0] == 1062:
                return Result()
            raise
        except BaseException:
            cursor.close()
            raise
        if cursor.rowcount > 0 and not cursor.description:
            self.total_changes += cursor.rowcount
        return Result(cursor)

    def begin(self):
        self.raw.begin()
        self.in_transaction = True
        # 跨线程/进程的 InnoDB 行锁，锁内读取+校验+写入容量，不能发生超卖。
        self.execute("SELECT id FROM _write_guard WHERE id=1 FOR UPDATE").fetchone()

    def commit(self):
        self.raw.commit()
        self.in_transaction = False

    def rollback(self):
        self.raw.rollback()
        self.in_transaction = False

    def close(self):
        self.raw.close()


def connect(config):
    try:
        import pymysql
    except ImportError as error:
        raise RuntimeError("MySQL 模式需要 PyMySQL，请先安装 requirements.txt。") from error
    raw = pymysql.connect(charset="utf8mb4", autocommit=True, connect_timeout=10,
                          read_timeout=30, write_timeout=30, **config)
    with raw.cursor() as cursor:
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cursor.execute("SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'")
    return Connection(raw, pymysql)
