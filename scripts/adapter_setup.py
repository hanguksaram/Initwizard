#!/usr/bin/env python3
"""Шаг 2 мастера: подключения инстанса Gina к внешним системам (хостинг кода, трекер задач и другие).

Подключение («адаптер») — это запись на инстансе: адрес внешней системы и ссылка на секрет с учётными данными.
После него агенты ходят во внешнюю систему через Gina по внутреннему адресу, а учётные данные подставляет ядро.

Скрипт обращается к ядру Gina изнутри его пода (kubectl exec) — токен владельца для этого не нужен.
Секрет внешней системы читается из файла в каталоге секретов и уходит в под через стандартный ввод:
в аргументах командной строки, в выводе и в журнале мастера его нет.

  python3 scripts/adapter_setup.py list                      — что уже подключено к инстансу (только чтение)
  python3 scripts/adapter_setup.py types [--all]             — какие системы умеет подключать этот инстанс (только чтение)
  python3 scripts/adapter_setup.py describe <тип>            — поля настройки и вид секрета для типа (только чтение)
  python3 scripts/adapter_setup.py secret-file <тип> <имя>   — создать пустой файл секрета <каталог секретов>/adapters/<имя>.env
  python3 scripts/adapter_setup.py connect <тип> --name "…" --base-url https://… --secret-file <имя> [--alias a]
                                   [--set поле=значение]… [--description "…"] [--dry-run] --confirmed-by "<ответ оператора>"
  python3 scripts/adapter_setup.py check [--id <id> | --name "…"]   — проверить подключения запросом во внешнюю систему
  python3 scripts/adapter_setup.py remove (--id <id> | --name "…") [--foreign] --confirmed-by "<ответ оператора>"

Что скрипт умеет создавать: типы, которым не нужна настройка привязки (исходящие API: GitLab, Jira, GitHub и т. п.).
Типы с настройкой привязки (входящие очереди задач, каналы чата) он показывает, но не создаёт — см. wizard/2-connections.md.
"""
import argparse, getpass, json, os, re, stat, sys
import common as c

STATE_FILE = 'adapters.json'
ADAPTER_SECRETS = os.path.join(c.SECRETS, 'adapters')
ACTOR = 'installation-wizard/' + (re.sub(r'[^A-Za-z0-9._-]', '', getpass.getuser()) or 'operator')   # заголовок принимает только ASCII

# Подсказки для разговора с оператором. Список типов и их поля всегда берутся с инстанса; здесь только назначение.
PURPOSE = {
    'gitlab-outbound': 'хостинг кода', 'github-outbound': 'хостинг кода', 'gitea-outbound': 'хостинг кода',
    'bitbucket-outbound': 'хостинг кода', 'gitverse-outbound': 'хостинг кода', 'azure-devops-outbound': 'хостинг кода и задачи',
    'jira-outbound': 'трекер задач', 'yandex-tracker-outbound': 'трекер задач',
    'confluence-outbound': 'документация', 'jenkins-outbound': 'сервер сборок', 'teamcity-outbound': 'сервер сборок',
    'sonarqube-outbound': 'качество кода', 'figma-outbound': 'макеты',
}
# Для каких типов в пакете есть готовый навык агентов.
BASE_SKILL = {'gitlab-outbound': 'gitlab', 'jira-outbound': 'jira'}
# Запрос «кто я» для проверки учётной записи. Первые два проверены на живом инстансе; остальные — стандартные пути API систем.
WHOAMI = {
    'gitlab-outbound': ('/api/v4/user', 'username', True), 'jira-outbound': ('/rest/api/2/myself', 'name', True),
    'github-outbound': ('/user', 'login', False), 'gitea-outbound': ('/api/v1/user', 'login', False),
}


def core(requests, timeout=120):
    try:
        return c.core_call(requests, timeout=timeout)
    except RuntimeError as e:
        sys.exit(str(e))


def get(path):
    return core([{'path': path}])[0]


def write(method, path, body):
    return core([{'method': method, 'path': path, 'body': body, 'headers': {'x-gina-on-behalf-of': ACTOR}}])[0]


def err_text(body):
    if isinstance(body, dict):
        return '%s%s' % (body.get('error') or body.get('message') or json.dumps(body, ensure_ascii=False)[:300],
                         ' (код %s)' % body['code'] if body.get('code') else '')
    return str(body)[:300]


def load_types():
    st, d = get('/api/adapters/types')
    if st == 404:
        sys.exit('ядро этого инстанса не отдаёт список типов подключений (/api/adapters/types → 404): версия ядра не поддерживается этим шагом')
    if st != 200 or not isinstance(d, list):
        sys.exit('список типов подключений не читается: HTTP %s %s' % (st, err_text(d)))
    return {t['key']: t for t in d}


def type_or_exit(types, key):
    t = types.get(key)
    if not t:
        sys.exit('типа «%s» на этом инстансе нет. Список: python3 scripts/adapter_setup.py types --all' % key)
    if not t.get('enabled'):
        sys.exit('тип «%s» на этом инстансе выключен' % key)
    return t


def fields(t):
    return {k: v for k, v in (t.get('config_schema') or {}).items() if k != 'secret_refs' and isinstance(v, dict)}


def slots(t):
    return dict((t.get('config_schema') or {}).get('secret_refs') or {})


def secret_hint(text):
    """Описание секрета из ядра без служебной приставки о форме ссылки."""
    return re.sub(r'^secret:<uuid>(?:,[^ ]+ or [^ ]+)?\s*(?:containing|with)?\s*', '', str(text)) or str(text)


def one_call(t):
    """Тип создаётся одним запросом, если его привязке не нужны настройки."""
    return not (t.get('binding_schema') or {})


def live_instances(type_key=None):
    st, d = get('/api/adapters/instances?exclude_archived=true&limit=200' + ('&type=' + type_key if type_key else ''))
    if st != 200 or not isinstance(d, dict):
        sys.exit('список подключений не читается: HTTP %s %s' % (st, err_text(d)))
    return [i for i in d.get('items', []) if i.get('state') != 'archived']


def usage():
    st, d = get('/api/adapters/binding-usage')
    return d.get('items', []) if st == 200 and isinstance(d, dict) else []


def mine():
    return c.load_state(STATE_FILE, {'items': []})


def find_instance(a):
    items = live_instances()
    if a.id:
        hit = [i for i in items if i['id'] == a.id]
    elif a.name:
        hit = [i for i in items if i['name'] == a.name]
    else:
        return None
    if len(hit) != 1:
        sys.exit('подключение не найдено среди действующих (или найдено несколько). Список: python3 scripts/adapter_setup.py list')
    return hit[0]


# ---------- чтение ----------

def cmd_list(_):
    items = live_instances(); use = usage(); own = {x['instance_id'] for x in mine()['items']}
    print('Подключения инстанса (действующие). Источник: ядро Gina, изнутри пространства инстанса.')
    for i in items:
        addrs = [u.get('alias_url') or u.get('url') for u in use if u.get('instance_id') == i['id']]
        print('  %-24s %-42s %-9s %s%s' % (i['type'], i['name'][:42], i['state'], ', '.join(a for a in addrs if a) or 'адреса для агентов нет',
                                           '   [создано мастером]' if i['id'] in own else ''))
        print('  %-24s id %s' % ('', i['id']))
    kinds = {i['type'] for i in items}
    print('Хостинг кода: %s. Трекер задач: %s.' % (
        ', '.join(sorted(k for k in kinds if PURPOSE.get(k, '').startswith('хостинг'))) or 'не подключён',
        ', '.join(sorted(k for k in kinds if PURPOSE.get(k) == 'трекер задач')) or 'не подключён'))


def cmd_types(a):
    types = load_types()
    print('Типы подключений этого инстанса. «одним запросом» — мастер может создать; «в интерфейсе» — нужна настройка привязки, мастер не создаёт.')
    for key in sorted(types, key=lambda k: (PURPOSE.get(k, 'я'), k)):
        t = types[key]
        if not t.get('enabled') or (not one_call(t) and not a.all):
            continue
        if not a.all and not (key in PURPOSE or key.endswith('-outbound')):
            continue        # встроенные и служебные типы — только по --all
        print('  %-26s %-22s %-14s секрет: %s%s' % (key, PURPOSE.get(key, 'прочее'), 'одним запросом' if one_call(t) else 'в интерфейсе',
                                                    ', '.join(slots(t)) or 'не нужен',
                                                    '   [в пакете есть навык %s]' % BASE_SKILL[key] if key in BASE_SKILL else ''))
    print('Подробно о типе: python3 scripts/adapter_setup.py describe <тип>')


def cmd_describe(a):
    t = type_or_exit(load_types(), a.type)
    print('%s — %s' % (t['key'], t.get('display_name')))
    print('Описание из ядра: %s' % t.get('description'))
    print('Назначение: %s. Готовый навык агентов в пакете: %s.' % (PURPOSE.get(t['key'], 'прочее'), BASE_SKILL.get(t['key'], 'нет')))
    print('Создаётся мастером: %s' % ('да, одним запросом' if one_call(t) else 'НЕТ — привязке нужны настройки (%s); настраивается в интерфейсе Gina' % ', '.join(t['binding_schema'])))
    print('Поля настройки (--base-url для base_url, остальные через --set поле=значение):')
    for k, v in fields(t).items():
        extra = '; '.join(x for x in ('по умолчанию %s' % json.dumps(v['default'], ensure_ascii=False) if 'default' in v else '',
                                      'пример %s' % v['example'] if 'example' in v else '') if x)
        print('  %-18s %-8s %s%s' % (k, v.get('type', ''), v.get('description', ''), ' (%s)' % extra if extra else ''))
    print('Секрет (кладётся в файл, не в команду):')
    for k, v in slots(t).items():
        print('  %-18s %s' % (k, secret_hint(v)))
    if not slots(t):
        print('  не нужен')
    live = live_instances(t['key'])
    if live:
        print('Уже подключено этого типа: %s. Второму подключению обязательно задай свой короткий адрес (--alias).' % ', '.join('«%s»' % i['name'] for i in live))


def cmd_secret_file(a):
    t = type_or_exit(load_types(), a.type)
    if not re.match(r'^[a-z0-9][a-z0-9._-]{0,63}$', a.name):
        sys.exit('имя файла: строчные латинские буквы, цифры, точка, дефис, подчёркивание')
    os.makedirs(ADAPTER_SECRETS, mode=0o700, exist_ok=True)
    path = os.path.join(ADAPTER_SECRETS, a.name + '.env')
    if os.path.exists(path):
        sys.exit('файл уже есть: %s' % path)
    lines = ['# Учётные данные для подключения типа %s. Значения вписывает оператор. Файл не покидает эту машину,' % t['key'],
             '# кроме одной передачи в хранилище секретов инстанса при создании подключения.']
    for k, v in slots(t).items():
        lines += ['# %s: %s' % (k, secret_hint(v)), '# Одно значение — строкой: %s=<значение>. Несколько полей — по строке на поле: %s.username=…, %s.password=…' % (k, k, k), '%s=' % k]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('создан %s (права 600). Оператор вписывает значение сам; в чат его присылать не нужно.' % path)


# ---------- запись ----------

def read_secret_file(name, t):
    path = name if os.path.sep in name else os.path.join(ADAPTER_SECRETS, name if name.endswith('.env') else name + '.env')
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        sys.exit('нет файла секрета %s — создай его: python3 scripts/adapter_setup.py secret-file %s <имя>' % (path, t['key']))
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        sys.exit('файл %s доступен другим пользователям (права %o) — выполни chmod 600' % (path, mode))
    known = slots(t); out = {}; empty = []
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1); k = k.strip(); v = v.strip()
        slot, _, sub = k.partition('.')
        if slot not in known:
            sys.exit('в файле секрета ключ «%s», а тип %s ждёт: %s' % (k, t['key'], ', '.join(known) or 'ничего'))
        if not v:
            empty.append(k); continue
        if sub:
            if not isinstance(out.setdefault(slot, {}), dict):
                sys.exit('в файле секрета «%s» задан и строкой, и по полям — оставь одно' % slot)
            out[slot][sub] = v
        else:
            if slot in out:
                sys.exit('в файле секрета «%s» задан дважды' % slot)
            out[slot] = v
    if empty and not out:
        sys.exit('файл секрета %s не заполнен: пусто у %s' % (path, ', '.join(empty)))
    if known and not out:
        sys.exit('в файле секрета %s нет значений' % path)
    for slot, v in list(out.items()):
        if isinstance(v, str) and v.startswith('{'):
            try:
                out[slot] = json.loads(v)
            except ValueError:
                sys.exit('значение «%s» начинается с «{», но это не JSON' % slot)
    return out, path


def typed(field, schema, raw):
    kind = schema.get('type')
    if kind == 'boolean':
        if raw.lower() not in ('true', 'false'):
            sys.exit('поле %s: нужно true или false' % field)
        return raw.lower() == 'true'
    if kind == 'integer':
        if not re.match(r'^-?\d+$', raw):
            sys.exit('поле %s: нужно целое число' % field)
        return int(raw)
    if kind == 'array':
        return [x.strip() for x in raw.split(',') if x.strip()]
    return raw


def build_config(a, t):
    known = fields(t); cfg = {}
    pairs = list(a.set or [])
    if a.base_url:
        pairs.append('base_url=' + a.base_url)
    if a.alias:
        pairs.append('alias=' + a.alias)
    for p in pairs:
        if '=' not in p:
            sys.exit('--set ждёт поле=значение, получено: %s' % p)
        k, v = p.split('=', 1); k = k.strip()
        if k not in known:
            sys.exit('у типа %s нет поля «%s». Поля: %s' % (t['key'], k, ', '.join(known)))
        if re.search(r'(?:^|_)(token|password|secret|private_key|api_key|apikey|credentials?)$', k, re.I):
            sys.exit('поле «%s» похоже на секрет: секреты кладутся только в файл секрета' % k)
        cfg[k] = typed(k, known[k], v.strip())
    if 'base_url' in known and 'base_url' not in cfg:
        sys.exit('не задан адрес внешней системы: --base-url https://…')
    return cfg


def after_create(inst_id, binding_id, t):
    """Проверка ядром и запросом «кто я». Возвращает (код выхода, сведения для журнала)."""
    reqs = [{'method': 'POST', 'path': '/api/adapters/instances/%s/healthcheck' % inst_id}, {'path': '/api/adapters/binding-usage'}]
    who = WHOAMI.get(t['key'])
    if who and binding_id:
        reqs.append({'path': '/proxy/%s%s' % (binding_id, who[0])})
    res = core(reqs)
    (hs, hc), (us, ub) = res[0], res[1]
    addr = None
    for u in (ub.get('items', []) if us == 200 and isinstance(ub, dict) else []):
        if u.get('instance_id') == inst_id:
            addr = u.get('alias_url') or u.get('url')
    print('Адрес для агентов: %s' % (addr or 'не опубликован (привязки нет)'))
    info = {'address': addr}
    rc = 0
    if hs == 200 and isinstance(hc, dict):
        info['health'] = hc.get('status')
        print('Проверка ядром: %s — %s%s' % (hc.get('status'), hc.get('message'), ' (ответ внешней системы: HTTP %s)' % hc['provider_status_code'] if hc.get('provider_status_code') else ''))
        if hc.get('status') == 'unhealthy':
            rc = 3
        elif hc.get('status') != 'healthy':
            print('  для этого типа ядро не умеет проверять связь само — НЕ проверено')
    else:
        print('Проверка ядром не выполнена: HTTP %s %s' % (hs, err_text(hc))); rc = 3
    if who and binding_id:
        ws, wb = res[2]
        ident = wb.get(who[1]) if isinstance(wb, dict) else None
        info['account'] = ident
        print('Проверка запросом: GET %s → HTTP %s%s%s' % (who[0], ws, ', учётная запись во внешней системе: %s' % ident if ident else '',
                                                           '' if who[2] else ' (путь стандартный для этой системы; через Gina на живом инстансе пакетом не проверялся)'))
        if ws != 200:
            rc = 3
    else:
        print('Проверка запросом: для типа %s запрос «кто я» в пакете не задан — учётная запись не названа' % t['key'])
    return rc, info


def cmd_connect(a):
    types = load_types(); t = type_or_exit(types, a.type)
    if not one_call(t):
        sys.exit('тип %s мастер не создаёт: его привязке нужны настройки (%s). Настройте подключение в интерфейсе Gina; после этого '
                 'python3 scripts/adapter_setup.py list покажет его.' % (t['key'], ', '.join(t['binding_schema'])))
    cfg = build_config(a, t)
    same = live_instances(t['key'])
    if any(i['name'] == a.name for i in same):
        sys.exit('подключение типа %s с именем «%s» уже есть. Возьми другое имя или проверь существующее: python3 scripts/adapter_setup.py check --name "%s"' % (t['key'], a.name, a.name))
    if same and 'alias' in fields(t) and 'alias' not in cfg:
        sys.exit('уже есть подключение этого типа (%s). У типа один общий короткий адрес; второе подключение без собственного адреса сделает его '
                 'неоднозначным, и агенты перестанут по нему ходить. Задай свой короткий адрес: --alias <строчные латинские буквы, цифры, дефис; 2–32 знака>'
                 % ', '.join('«%s»' % i['name'] for i in same))
    if slots(t) and not a.secret_file:
        sys.exit('типу %s нужен секрет (%s): укажи --secret-file <имя>. Создать пустой файл: python3 scripts/adapter_setup.py secret-file %s <имя>'
                 % (t['key'], ', '.join(slots(t)), t['key']))
    secrets, spath = read_secret_file(a.secret_file, t) if slots(t) else ({}, None)
    body = {'type_key': t['key'], 'name': a.name, 'description': a.description or '', 'config': cfg, 'secrets': secrets}
    print('Будет создано подключение:')
    print('  тип:      %s (%s)' % (t['key'], t.get('display_name')))
    print('  имя:      %s' % a.name)
    print('  настройки: %s' % json.dumps(cfg, ensure_ascii=False))
    print('  секрет:   %s' % ('из файла %s: %s (значения не показываются)' % (spath, ', '.join(
        '%s — %s' % (k, 'строка' if isinstance(v, str) else 'поля ' + ', '.join(v)) for k, v in secrets.items())) if secrets else 'не нужен'))
    print('  что произойдёт: секрет уйдёт в хранилище секретов инстанса; появится подключение и адрес, по которому агенты ходят во внешнюю систему.')
    if a.dry_run:
        print('Пробный прогон: ничего не создано.'); return
    if not a.confirmed_by:
        sys.exit('нужно согласие оператора на создание этого подключения: --confirmed-by "<его ответ>"')
    st, d = write('POST', '/api/adapters/connect', {})
    if st == 404:
        sys.exit('ядро этого инстанса не поддерживает создание подключения одним запросом (/api/adapters/connect → 404). Ничего не создано. '
                 'Подключение нужно настроить в интерфейсе Gina.')
    if st in (401, 403):
        sys.exit('ядро не принимает вызов изнутри пода без токена владельца (HTTP %s). Ничего не создано. Подключение нужно настроить в интерфейсе Gina.' % st)
    c.confirm('создание подключения «%s» (%s) к %s' % (a.name, t['key'], cfg.get('base_url', '—')), a.confirmed_by)
    st, d = write('POST', '/api/adapters/connect', body)
    if st != 201 or not isinstance(d, dict) or not d.get('instance_id'):
        sys.exit('Ядро отклонило: HTTP %s %s\nНичего не создано: при отказе ядро само отзывает секрет, который успело сохранить.' % (st, err_text(d)))
    print('Подключение создано: id %s, привязка %s' % (d['instance_id'], d.get('binding_id') or 'нет'))
    rc, info = after_create(d['instance_id'], d.get('binding_id'), t)
    s = mine(); s['items'].append({'instance_id': d['instance_id'], 'binding_id': d.get('binding_id'), 'type': t['key'], 'name': a.name,
                                  'alias': cfg.get('alias'), 'base_url': cfg.get('base_url'), 'created': c.now(), **info})
    c.save_state(STATE_FILE, s)
    if rc:
        print('ИТОГ: подключение создано, но проверка НЕ прошла. Агенты уже видят его в списке подключений и получат отказ. Варианты: '
              'исправить причину и повторить проверку (check), либо убрать подключение (remove) и создать заново с верным секретом.')
    else:
        print('ИТОГ: подключение работает.')
    sys.exit(rc)


def cmd_check(a):
    types = load_types(); one = find_instance(a)
    items = [one] if one else [i for i in live_instances() if slots(types.get(i['type'], {})) and one_call(types.get(i['type'], {}))]
    if not items:
        print('подключений к внешним системам нет'); return
    use = usage(); bad = 0
    for i in items:
        print('%s — «%s»' % (i['type'], i['name']))
        b = [u['binding_id'] for u in use if u.get('instance_id') == i['id'] and u.get('state') == 'active']
        rc, _ = after_create(i['id'], b[0] if b else None, types[i['type']])
        bad += 1 if rc else 0
    sys.exit(3 if bad else 0)


def cmd_remove(a):
    if not a.confirmed_by:
        sys.exit('нужно согласие оператора: --confirmed-by "<его ответ>"')
    i = find_instance(a)
    if not i:
        sys.exit('укажи подключение: --id <id> или --name "…"')
    own = [x for x in mine()['items'] if x['instance_id'] == i['id']]
    if not own and not a.foreign:
        sys.exit('подключение «%s» создано не этим мастером. Убирать чужое можно только по прямому слову оператора: добавь --foreign' % i['name'])
    st, d = get('/api/adapters/instances/%s' % i['id'])
    if st != 200 or not isinstance(d, dict):
        sys.exit('подключение не читается: HTTP %s %s' % (st, err_text(d)))
    c.confirm('архивация подключения «%s» (%s)' % (i['name'], i['type']), a.confirmed_by)
    for b in d.get('bindings', []):
        if b.get('state') == 'archived':
            continue
        st2, d2 = write('DELETE', '/api/adapters/bindings/%s' % b['id'], {'expected_version': b['version']})
        if st2 != 200:
            sys.exit('привязка %s не архивирована: HTTP %s %s. Подключение не тронуто.' % (b['id'], st2, err_text(d2)))
    st3, d3 = write('DELETE', '/api/adapters/instances/%s' % i['id'], {'expected_version': d['version']})
    if st3 != 200:
        sys.exit('подключение не архивировано: HTTP %s %s' % (st3, err_text(d3)))
    s = mine(); s['items'] = [x for x in s['items'] if x['instance_id'] != i['id']]; c.save_state(STATE_FILE, s)
    print('Подключение «%s» переведено в архив: адрес для агентов снят, из списка действующих оно исчезло.' % i['name'])
    refs = ', '.join((d.get('secret_refs') or {}).values())
    if refs:
        print('Секрет остался в хранилище секретов инстанса (%s). Удалить его может владелец инстанса; мастер этого не делает.' % refs)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    p = sub.add_parser('types'); p.add_argument('--all', action='store_true')
    sub.add_parser('describe').add_argument('type')
    p = sub.add_parser('secret-file'); p.add_argument('type'); p.add_argument('name')
    p = sub.add_parser('connect'); p.add_argument('type'); p.add_argument('--name', required=True); p.add_argument('--description')
    p.add_argument('--base-url'); p.add_argument('--alias'); p.add_argument('--set', action='append'); p.add_argument('--secret-file')
    p.add_argument('--dry-run', action='store_true'); p.add_argument('--confirmed-by')
    p = sub.add_parser('check'); p.add_argument('--id'); p.add_argument('--name')
    p = sub.add_parser('remove'); p.add_argument('--id'); p.add_argument('--name'); p.add_argument('--foreign', action='store_true'); p.add_argument('--confirmed-by')
    a = ap.parse_args()
    {'list': cmd_list, 'types': cmd_types, 'describe': cmd_describe, 'secret-file': cmd_secret_file,
     'connect': cmd_connect, 'check': cmd_check, 'remove': cmd_remove}[a.cmd](a)
