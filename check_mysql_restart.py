import json
from pathlib import Path
import sys
from server import Database

db = Database(backend='mysql', initialize=False)
path = Path('/www/wwwroot/gaoqiu/data/mysql-restart-check.json')
with db.connection() as connection:
    actual = [dict(row) for row in connection.execute(
        "SELECT id,status,checked,total FROM orders WHERE id IN (?,?) ORDER BY id",
        ('GE1791807777782', 'GE-LIVE-QA-1791812086')).fetchall()]
assert len(actual) == 2
if sys.argv[1] == 'before':
    path.write_text(json.dumps({'before': actual}, ensure_ascii=False))
    print('重启前已读取两条订单。')
else:
    report = json.loads(path.read_text())
    report['after'] = actual
    report['equal'] = report['before'] == actual
    assert report['equal'], '重启后订单不一致'
    path.write_text(json.dumps(report, ensure_ascii=False))
    print(json.dumps(report, ensure_ascii=False))
