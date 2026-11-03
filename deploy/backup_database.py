"""使用应用专属 MySQL 账号做一致性备份，凭证只写入临时0600文件。"""
import datetime
import gzip
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

base = Path(os.environ.get('GOLFERA_BACKUP_DIR', '/www/wwwroot/gaoqiu/data/backups'))
base.mkdir(mode=0o700, parents=True, exist_ok=True)
path = base / ('gaoqiu-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.sql.gz')
partial = path.with_suffix('.partial')
dump = shutil.which('mysqldump') or '/www/server/mysql/bin/mysqldump'
options = {
    'user': os.environ['GOLFERA_MYSQL_USER'],
    'password': os.environ['GOLFERA_MYSQL_PASSWORD'],
    'host': os.environ.get('GOLFERA_MYSQL_HOST', '127.0.0.1'),
    'port': os.environ.get('GOLFERA_MYSQL_PORT', '3306'),
}
fd, config_path = tempfile.mkstemp(prefix='gaoqiu-backup-', suffix='.cnf')
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as config:
        config.write('[client]\n')
        for key, value in options.items():
            escaped = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
            config.write(key + '="' + escaped + '"\n')
    with open(partial, 'wb') as target:
        os.chmod(partial, 0o600)
        with gzip.GzipFile(fileobj=target, mode='wb') as compressed:
            process = subprocess.Popen([dump, '--defaults-extra-file=' + config_path,
                '--single-transaction', '--quick', '--skip-lock-tables',
                '--default-character-set=utf8mb4', os.environ['GOLFERA_MYSQL_DATABASE']],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            shutil.copyfileobj(process.stdout, compressed)
            error = process.stderr.read()
            if process.wait() != 0:
                raise RuntimeError('MySQL备份失败：' + error.decode(errors='replace'))
    partial.replace(path)
    print(path)
    for old in sorted(base.glob('gaoqiu-*.sql.gz'))[:-14]:
        old.unlink()
finally:
    os.unlink(config_path)
    if partial.exists():
        partial.unlink()
