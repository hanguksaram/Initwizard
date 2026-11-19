#!/usr/bin/env python3
"""Шаг 1 мастера: проверка доступов. Ничего не меняет, секреты не печатает.

Запуск: python3 scripts/access_check.py [--only gina|gitlab|k8s|optional] [--json]
Код возврата 0 — все обязательные проверки прошли; 1 — есть провал обязательной.
Статусы: OK, FAIL (обязательное не работает), MISS (необязательное не задано), WARN (работает, но есть замечание).
"""
import json, os, stat, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c

R = []  # (группа, проверка, статус, пояснение)


def add(group, name, status, note=''):
    R.append({'group': group, 'check': name, 'status': status, 'note': note})


def check_secret_files():
    if not os.path.isdir(c.SECRETS):
        add('секреты', 'каталог ' + c.SECRETS, 'FAIL', 'каталога нет: mkdir -m 700 ' + c.SECRETS)
        return
    mode = stat.S_IMODE(os.stat(c.SECRETS).st_mode)
    add('секреты', 'каталог ' + c.SECRETS, 'OK' if not mode & 0o077 else 'FAIL', 'права %o' % mode)
    for name, (need, keys, what) in c.SECRET_FILES.items():
        path = os.path.join(c.SECRETS, name)
        if not os.path.exists(path):
            add('секреты', name, 'FAIL' if need == 'required' else 'MISS', 'нет файла; нужен для: ' + what)
            continue
        env = c.load_env(name)
        filled = [k for k in keys if env.get(k) and not (k == 'NOTIFY_KIND' and env[k].lower() == 'none')]
        if need == 'required':
            missing = [k for k in keys if not env.get(k)]
            add('секреты', name, 'FAIL' if missing else 'OK', ('не заполнено: ' + ', '.join(missing)) if missing else 'заполнен')
        else:
            add('секреты', name, 'OK' if filled else 'MISS', 'заполнен' if filled else 'шаблон не заполнен; нужен для: ' + what)


def check_gitlab():
    e = c.load_env('gitlab.env')
    if not e.get('GITLAB_URL') or not e.get('GITLAB_TOKEN'):
        return
    g = c.GitLab(need_project=False)
    st, u = g.call('GET', '/user')
    if st != 200 or not isinstance(u, dict):
        add('GitLab', 'токен', 'FAIL', 'GET /user → %s; проверь GITLAB_URL и токен (нужна область api)' % st)
        return
    add('GitLab', 'токен', 'OK', 'пользователь ' + str(u.get('username')))
    if not g.project:
        add('GitLab', 'репозиторий методологии', 'FAIL', 'не задан: python3 scripts/wizard_state.py set methodology_project <группа/проект> '
            '(подсказка — строка «источник промптов» выше: какой репозиторий читает инстанс)')
        return
    st, p = g.p('GET', '')
    if st != 200 or not isinstance(p, dict):
        add('GitLab', 'репозиторий методологии ' + g.project, 'FAIL', 'GET проекта → %s; проверь путь репозитория и права токена' % st)
        return
    perm = p.get('permissions') or {}
    level = max((perm.get('project_access') or {}).get('access_level') or 0, (perm.get('group_access') or {}).get('access_level') or 0)
    add('GitLab', 'репозиторий методологии ' + g.project, 'OK' if level >= 30 else 'FAIL',
        'уровень доступа %s (нужно не ниже 30 — Developer: создавать ветки и merge request-ы)' % level)
    add('GitLab', 'рабочая ветка репозитория', 'OK', '%s — мастер её не читает и не меняет' % p.get('default_branch'))
    if g.branch:
        st, b = g.p('GET', '/repository/branches/' + c.urllib.parse.quote(g.branch, safe=''))
        add('GitLab', 'ветка POC ' + g.branch, 'OK' if st == 200 else 'WARN', ('голова ' + b['commit']['short_id']) if st == 200 else 'в журнале записана, но в репозитории её нет')
    else:
        add('GitLab', 'ветка POC', 'MISS', 'будет создана в начале шага 4')


def check_gina():
    env = c.load_env('gina.env')
    if not env.get('GINA_URL'):
        return
    g = c.Gina()
    st, h = g.call('GET', '/api/health', auth=False)
    ok = st == 200 and isinstance(h, dict) and h.get('ok')
    add('Gina', 'API ' + g.base, 'OK' if ok else 'FAIL', 'отвечает, база данных: %s' % h.get('database') if ok else 'health → %s' % (st or h))
    if not ok:
        return
    if not env.get('GINA_OWNER_TOKEN'):
        add('Gina', 'токен владельца', 'FAIL', 'не вписан в gina.env — шаги 4 и 6 без него невозможны')
        return
    st, ps = g.call('GET', '/api/prompt-source')
    if st != 200 or not isinstance(ps, dict):
        add('Gina', 'токен владельца', 'FAIL', 'GET /api/prompt-source → %s; это не токен владельца инстанса или он не действует' % st)
        return
    cfg, status = ps.get('config') or {}, ps.get('status') or {}
    add('Gina', 'токен владельца', 'OK', 'действует')
    mode = cfg.get('mode')
    add('Gina', 'источник промптов', 'OK' if mode == 'git' else 'WARN',
        'инстанс читает репозиторий %s, ветку %s (режим %s, состояние %s)' % (cfg.get('project'), cfg.get('branch'), mode, status.get('state'))
        + ('' if mode == 'git' else ' — режим не git: методология из Git не применяется, переключение на ветку POC включит режим git'))
    s = c.settings()
    if s.get('methodology_project') and (cfg.get('project') or '').strip('/') != s['methodology_project'].strip('/'):
        add('Gina', 'репозиторий инстанса и репозиторий мастера', 'WARN', 'инстанс читает %s, а мастеру задан %s — при переключении инстанс сменит и репозиторий; '
            'убедись у оператора, что это намеренно' % (cfg.get('project'), s['methodology_project']))
    c.save_state('prompt_source.json', {'config': cfg, 'status': {k: status.get(k) for k in ('state', 'sha', 'synced_at', 'error')}})


def check_k8s():
    if not c.load_env('k8s.env'):
        return
    rc, out, err = c.kubectl(['get', 'pods', '-o', 'json'])
    if rc != 0:
        add('k8s', 'чтение подов', 'FAIL', (err or out).strip()[:200])
        return
    pods = json.loads(out)['items']
    names = [p['metadata']['name'] for p in pods]
    add('k8s', 'чтение подов', 'OK', 'подов: %d (%s)' % (len(names), ', '.join(names)[:120]))
    for what, args, need in (('чтение логов', ['auth', 'can-i', 'get', 'pods/log'], 'FAIL'),
                             ('exec в поды (счётчик операций jcode, прокси Gina)', ['auth', 'can-i', 'create', 'pods/exec'], 'WARN'),
                             ('события', ['auth', 'can-i', 'list', 'events'], 'MISS')):
        rc, out, _ = c.kubectl(args)
        add('k8s', what, 'OK' if out.strip() == 'yes' else need, out.strip() or 'нет ответа')
    if not any(n.startswith('gina-evd') for n in names):
        add('k8s', 'под ядра Gina (gina-evd)', 'FAIL', 'в пространстве нет пода gina-evd — это не пространство инстанса Gina; проверь K8S_NAMESPACE')


def check_optional():
    db = c.load_env('db.env')
    if db.get('PG_DSN'):
        try:
            import psycopg2  # noqa
            conn = psycopg2.connect(db['PG_DSN'], connect_timeout=10)
            cur = conn.cursor(); cur.execute('select current_database(), current_setting(\'transaction_read_only\')')
            d, ro = cur.fetchone(); conn.close()
            add('PostgreSQL', 'подключение', 'OK', 'база %s, только чтение: %s' % (d, ro))
        except ImportError:
            add('PostgreSQL', 'подключение', 'WARN', 'нет модуля psycopg2: pip install psycopg2-binary (в отдельное окружение)')
        except Exception as e:   # текст ошибки не печатаем: в нём может оказаться строка подключения
            add('PostgreSQL', 'подключение', 'WARN', 'не подключается (%s) — проверь PG_DSN в db.env' % type(e).__name__)
    else:
        add('PostgreSQL', 'подключение', 'MISS', 'не задано: сторож возьмёт очередь и дела из логов и API')
    ch = c.load_env('clickhouse.env')
    if ch.get('CH_URL'):
        import base64
        auth = base64.b64encode(('%s:%s' % (ch.get('CH_USER', ''), ch.get('CH_PASSWORD', ''))).encode()).decode()
        st, d = c.http('GET', ch['CH_URL'].rstrip('/') + '/?query=SELECT%201', {'Authorization': 'Basic ' + auth}, timeout=15)
        add('ClickHouse', 'подключение', 'OK' if st == 200 else 'WARN', 'HTTP %s' % st)
    else:
        add('ClickHouse', 'подключение', 'MISS', 'не задано: раздел «токены по делам» сторож пропустит')
    n = c.load_env('notify.env')
    kind = (n.get('NOTIFY_KIND') or 'none').lower()
    if kind == 'telegram':
        st, d = c.http('GET', 'https://api.telegram.org/bot%s/getMe' % n.get('TELEGRAM_BOT_TOKEN', ''), timeout=15)
        ok = st == 200 and isinstance(d, dict) and d.get('ok')
        add('уведомления', 'Telegram', 'OK' if ok and n.get('TELEGRAM_CHAT_ID') else 'WARN',
            ('бот @%s' % d['result'].get('username')) if ok else 'getMe → %s' % st)
    elif kind == 'mattermost':
        add('уведомления', 'Mattermost webhook', 'OK' if n.get('MATTERMOST_WEBHOOK_URL') else 'WARN', 'проверяется тестовым сообщением на шаге 4')
    else:
        add('уведомления', 'канал', 'MISS', 'не задан: тревоги будут только в файлах и в сессии агента')
    j = c.load_env('jira.env'); key = c.settings().get('tracker_project')
    if j.get('JIRA_URL') and j.get('JIRA_TOKEN'):
        path = '/rest/api/2/project/' + key if key else '/rest/api/2/myself'
        st, d = c.http('GET', j['JIRA_URL'].rstrip('/') + path, {'Authorization': 'Bearer ' + j.get('JIRA_TOKEN', '')}, timeout=20)
        add('Jira', 'прямой доступ' + (' к проекту ' + key if key else ''), 'OK' if st == 200 else 'WARN', 'HTTP %s' % st)
    else:
        add('Jira', 'прямой доступ', 'MISS', 'не задано: агентам Gina не нужно (они ходят через прокси), мастеру — только для удобства')
    u = c.load_env('ui.env')
    add('интерфейс', 'тестовая учётная запись', 'OK' if u.get('UI_USER') and u.get('UI_PASSWORD') else 'MISS',
        'задана' if u.get('UI_USER') and u.get('UI_PASSWORD') else 'не задана: обход интерфейса продукта на шаге 3 дойдёт только до страницы входа')


def main():
    groups = {'secrets': check_secret_files, 'gina': check_gina, 'gitlab': check_gitlab, 'k8s': check_k8s, 'optional': check_optional}
    only = sys.argv[sys.argv.index('--only') + 1] if '--only' in sys.argv else None
    if only and only not in groups:
        sys.exit('--only: одно из %s' % ', '.join(groups))
    for name, f in groups.items():
        if only and name != only:
            continue
        try:
            f()
        except SystemExit as e:
            add('секреты', name, 'FAIL', str(e))
        except Exception as e:
            add('сбой проверки', name, 'FAIL', type(e).__name__)
    if not only:
        c.save_state('access_check.json', R)
    if '--json' in sys.argv:
        print(json.dumps(R, ensure_ascii=False, indent=1))
    else:
        for r in R:
            print('%-5s %-12s %-46s %s' % (r['status'], r['group'], r['check'][:46], r['note']))
    sys.exit(1 if any(r['status'] == 'FAIL' for r in R) else 0)


if __name__ == '__main__':
    main()
