"""Copy Django data from db.sqlite3 into PostgreSQL (two-step dump/load)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VENV_PYTHON = BASE_DIR.parent / 'venv' / 'Scripts' / 'python.exe'
PYTHON = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
DUMP = BASE_DIR / '_sqlite_dump.json'
MANAGE = BASE_DIR / 'manage.py'


def run(env_extra: dict | None, *args: str) -> None:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    print('>', ' '.join(args))
    subprocess.run([PYTHON, str(MANAGE), *args], cwd=str(BASE_DIR), env=env, check=True)


def main() -> None:
    if not (BASE_DIR / 'db.sqlite3').exists():
        raise SystemExit('db.sqlite3 not found')

    print('1) Dumping from SQLite…')
    env_sqlite = {
        'SQLITE_MIGRATE': '1',
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONUTF8': '1',
    }
    env = os.environ.copy()
    env.update(env_sqlite)
    subprocess.run(
        [
            PYTHON,
            str(MANAGE),
            'dumpdata',
            '--natural-foreign',
            '--natural-primary',
            '--indent',
            '2',
            '-e',
            'contenttypes',
            '-e',
            'auth.Permission',
            '-e',
            'admin.LogEntry',
            '-e',
            'sessions',
            '-o',
            str(DUMP),
        ],
        cwd=str(BASE_DIR),
        env=env,
        check=True,
    )
    print(f'   Wrote {DUMP} ({DUMP.stat().st_size} bytes)')

    print('2) Ensuring Postgres schema…')
    run({'SQLITE_MIGRATE': '0'}, 'migrate', '--noinput')

    print('3) Clearing existing Postgres app data…')
    clear_script = r"""
import django
django.setup()
from django.apps import apps
from django.db import connection

tables = []
for model in apps.get_models():
    if model._meta.proxy or not model._meta.managed:
        continue
    label = model._meta.app_label
    name = model.__name__
    if label in {'contenttypes', 'sessions'}:
        continue
    if label == 'auth' and name == 'Permission':
        continue
    if label == 'admin':
        continue
    tables.append(model._meta.db_table)

if tables:
    with connection.cursor() as cursor:
        joined = ', '.join('"' + t + '"' for t in tables)
        cursor.execute(f'TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE;')
    print('truncated', len(tables), 'tables')
else:
    print('no tables')
"""
    subprocess.run(
        [PYTHON, str(MANAGE), 'shell', '-c', clear_script],
        cwd=str(BASE_DIR),
        env={**os.environ, 'SQLITE_MIGRATE': '0'},
        check=True,
    )

    print('4) Loading into PostgreSQL…')
    run({'SQLITE_MIGRATE': '0'}, 'loaddata', str(DUMP))

    print('5) Resetting Postgres sequences…')
    seq_script = r"""
import django
django.setup()
from django.apps import apps
from django.core.management.color import no_style
from django.db import connection

models = [m for m in apps.get_models() if not m._meta.proxy and m._meta.managed]
sql_list = connection.ops.sequence_reset_sql(no_style(), models)
with connection.cursor() as cursor:
    for sql in sql_list:
        cursor.execute(sql)
print('reset', len(sql_list), 'sequences')
"""
    subprocess.run(
        [PYTHON, str(MANAGE), 'shell', '-c', seq_script],
        cwd=str(BASE_DIR),
        env={**os.environ, 'SQLITE_MIGRATE': '0'},
        check=True,
    )

    try:
        DUMP.unlink(missing_ok=True)
    except OSError:
        pass

    print('Done. SQLite data is now in PostgreSQL.')


if __name__ == '__main__':
    main()
