#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
D=/opt/tvoe
APP=$D/ui2027_bot.py
MOD=$D/campaigns_wordstat.py
UNIT=tvoe.service
BASE=https://raw.githubusercontent.com/senyaav27/Wortstat-senyaa-/59f8aa255cd68796c3c4acdd32b7fdb085eee49a/ui2027_bot.py
FEAT=https://raw.githubusercontent.com/senyaav27/Wortstat-senyaa-/1ad6ed98bb9e06e500ca31fbe2e1c240bcfbe454
abort(){ echo "ОШИБКА: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || abort "Нужны права root"
for tool in curl git python3 flock systemctl;do command -v "$tool" >/dev/null || abort "Нет программы $tool";done
[ -f "$APP" ] || abort "Не найден $APP"
systemctl is-active --quiet "$UNIT" || abort "Служба не запущена"
systemctl show "$UNIT" -p ExecStart --value | grep -Fq "$APP" || abort "Служба запускает другой файл"
exec 9>$D/.direct-import-install.lock
flock -n 9 || abort "Другое обновление выполняется"
if grep -q '^from campaigns_wordstat import' "$APP";then echo "Импорт уже установлен. Изменений нет.";exit 0;fi
T=$(mktemp -d "$D/.direct-import.XXXXXXXX")
B=""
DB=$D/data/tvoe.sqlite3
CHANGED=0
finish(){
  rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ] && [ "$CHANGED" -eq 1 ];then
    echo "Обновление не прошло проверку. Откатываем изменения..." >&2
    systemctl stop "$UNIT" >/dev/null 2>&1 || true
    cp -p "$B/ui2027_bot.py" "$APP"
    if [ -f "$B/module-present" ];then cp -p "$B/campaigns_wordstat.py" "$MOD";else rm -f "$MOD";fi
    rm -f "$DB-wal" "$DB-shm"
    cp -p "$B/tvoe.sqlite3" "$DB"
    systemctl start "$UNIT" || echo "ТРЕБУЕТСЯ ПРОВЕРКА: служба не запустилась после отката" >&2
  fi
  rm -rf "$T"
  exit "$rc"
}
trap finish EXIT
# Resolve actual DB path without printing environment secrets.
DB=$(/usr/bin/python3 - <<'PY'
import os,subprocess
pid=subprocess.check_output(['systemctl','show','tvoe.service','-p','MainPID','--value'],text=True).strip()
db='/opt/tvoe/data/tvoe.sqlite3'
try:
  for p in open('/proc/'+pid+'/environ','rb').read().split(b'\0'):
    if p.startswith(b'DATABASE_PATH='):
      v=os.fsdecode(p.split(b'=',1)[1]);db=v if os.path.isabs(v) else os.path.join('/opt/tvoe',v)
except OSError:pass
print(os.path.realpath(db))
PY
)
[ -f "$DB" ] || abort "Не найдена база $DB"
case "$DB" in /opt/tvoe/*) ;; *) abort "База находится за пределами /opt/tvoe";; esac
echo "[1/5] Получаем код изменений с GitHub..."
curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 "$BASE" -o "$T/base.py"
curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 "$FEAT/ui2027_bot.py" -o "$T/feature.py"
curl -fsSL --retry 2 --connect-timeout 10 --max-time 60 "$FEAT/campaigns_wordstat.py" -o "$T/campaigns_wordstat.py"
[ -s "$T/base.py" ] && [ -s "$T/feature.py" ] && [ -s "$T/campaigns_wordstat.py" ] || abort "Загрузка неполная"
echo "[2/5] Совмещаем код с текущим интерфейсом сервера..."
if ! git merge-file -p "$APP" "$T/base.py" "$T/feature.py" > "$T/merged.py";then
  abort "Конфликт при объединении. Рабочая версия НЕ заменена. Пришлите этот вывод."
fi
PYTHONPYCACHEPREFIX="$T/cache" /usr/bin/python3 -m py_compile "$T/merged.py" "$T/campaigns_wordstat.py" || abort "Ошибка синтаксиса, сервер не менялся"
echo "[3/5] Резервируем SQLite и проверяем отображение на копии..."
B=$D/backups/direct-import-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$B"
cp -p "$APP" "$B/ui2027_bot.py"
if [ -f "$MOD" ];then cp -p "$MOD" "$B/campaigns_wordstat.py";touch "$B/module-present";fi
/usr/bin/python3 - "$DB" "$B/tvoe.sqlite3" <<'PY'
import sqlite3,sys
src=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True,timeout=30)
dst=sqlite3.connect(sys.argv[2],timeout=30)
src.backup(dst)
assert dst.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
src.close();dst.close()
PY
cp -p "$B/tvoe.sqlite3" "$T/test.sqlite3"
PYTHONPATH="$T" /usr/bin/python3 - "$T/merged.py" "$T/test.sqlite3" <<'PY'
import sys,importlib.util
spec=importlib.util.spec_from_file_location('tvoe_preflight',sys.argv[1])
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
db=app.Store(sys.argv[2])
page=app.dashboard_document(db,'ads','','nonce',csrf='csrf')
assert 'campaign-import' in page and 'campaign_file' in page
assert '/catalog-feed' in open(sys.argv[1],encoding='utf-8').read()
db.db.close()
print('Проверка страницы и таблиц: OK')
PY
echo "[4/5] Устанавливаем код..."
CHANGED=1
systemctl stop "$UNIT"
install -m 644 "$T/merged.py" "$APP.new-campaign"
install -m 644 "$T/campaigns_wordstat.py" "$MOD.new-campaign"
mv -f "$APP.new-campaign" "$APP"
mv -f "$MOD.new-campaign" "$MOD"
systemctl start "$UNIT"
echo "[5/5] Проверяем работу..."
PORT=$(/usr/bin/python3 - <<'PY'
import subprocess
pid=subprocess.check_output(['systemctl','show','tvoe.service','-p','MainPID','--value'],text=True).strip()
port='8080'
try:
  for entry in open('/proc/'+pid+'/environ','rb').read().split(b'\0'):
    if entry.startswith(b'PORT='):
      port=entry.split(b'=',1)[1].decode()
except (OSError,ValueError):pass
print(port if port.isdecimal() and 1<=int(port)<=65535 else '8080')
PY
)
for i in 1 2 3 4 5 6 7 8 9 10 11 12;do
  if systemctl is-active --quiet "$UNIT" && curl -fsS --max-time 3 http://127.0.0.1:$PORT/health 2>/dev/null | grep -qx ok;then
    CHANGED=0
    echo "УСПЕШНО: обновление установлено и служба работает."
    echo "Открой https://tvoe.78-17-38-166.sslip.io/?view=ads и загрузи Excel."
    echo "Резервная копия: $B"
    exit 0
  fi
  sleep 2
done
abort "Сайт не отвечает на /health, включаем откат."
