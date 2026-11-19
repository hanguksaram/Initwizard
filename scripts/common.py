"""Общие функции мастера установки: секреты из файлов, журнал, HTTP, kubectl.

Секреты лежат в каталоге GINA_POC_SECRETS_DIR (по умолчанию ~/.gina-poc, права 0700), каждый файл 0600,
формат KEY=VALUE. Значения секретов никогда не печатаются и не передаются в аргументах командной строки.
"""
import json, os, stat, subprocess, sys, urllib.error, urllib.parse, urllib.request

PACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS = os.environ.get('GINA_POC_SECRETS_DIR', os.path.expanduser('~/.gina-poc'))
STATE = os.path.join(PACK, 'state')

# имя файла -> (обязательность, ключи, для чего)
SECRET_FILES = {
    'gitlab.env': ('required', ['GITLAB_URL', 'GITLAB_TOKEN'],
                   'репозиторий методологии: создание ветки, коммиты, merge request-ы'),
    'gina.env': ('required', ['GINA_URL', 'GINA_OWNER_TOKEN'],
                 'API инстанса Gina. Токен владельца даёт полный доступ: им мастер читает настройки и каталог, проверяет ветку '
                 'без применения, переключает инстанс на ветку POC и создаёт/включает регламент — каждое изменение только после согласия'),
    'k8s.env': ('required', ['KUBECONFIG_PATH', 'K8S_NAMESPACE'],
                'поды и логи инстанса: сторож и проверки'),
    'db.env': ('optional', ['PG_DSN'], 'PostgreSQL инстанса на чтение: очередь, дела (можно заменить доступом через k8s)'),
    'clickhouse.env': ('optional', ['CH_URL', 'CH_USER', 'CH_PASSWORD', 'CH_DATABASE'], 'телеметрия LLM на чтение'),
    'notify.env': ('optional', ['NOTIFY_KIND', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'MATTERMOST_WEBHOOK_URL'],
                   'куда сторож шлёт тревоги: telegram | mattermost | none'),
    'jira.env': ('optional', ['JIRA_URL', 'JIRA_TOKEN'], 'чтение задач Jira напрямую из сессии агента (агентам Gina не нужно: они ходят через прокси)'),
    'ui.env': ('optional', ['UI_USER', 'UI_PASSWORD'], 'тестовая учётная запись для обхода интерфейса продукта на шаге 3'),
}


def load_env(name, must=False):
    """Читает KEY=VALUE из файла секретов. Возвращает {} если файла нет (или падает при must=True)."""
    path = os.path.join(SECRETS, name)
    if not os.path.exists(path):
        if must:
            sys.exit('нет файла секретов %s — вернись к шагу 1 мастера' % path)
        return {}
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        sys.exit('файл %s доступен другим пользователям (права %o) — выполни chmod 600' % (path, mode))
    env = {}
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def http(method, url, headers=None, body=None, timeout=60, as_text=False):
    """Возвращает (status, данные): JSON, если разобрался, иначе текст (as_text=True — всегда текст).
    Сетевую ошибку отдаёт как (0, текст)."""
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        headers.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except Exception as e:  # сеть, DNS, TLS
        return 0, '%s: %s' % (type(e).__name__, e)
    text = raw.decode('utf-8', 'replace')
    if as_text:
        return status, text
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


class GitLab:
    """Репозиторий методологии. Адрес и токен — из gitlab.env; проект и ветки — из настроек журнала мастера."""
    def __init__(self, need_project=True):
        e = load_env('gitlab.env', must=True)
        if not e.get('GITLAB_URL') or not e.get('GITLAB_TOKEN'):
            sys.exit('в gitlab.env не заполнены GITLAB_URL и GITLAB_TOKEN — вернись к шагу 1 мастера')
        self.url = e['GITLAB_URL'].rstrip('/')
        self.base = self.url + '/api/v4'
        self._token = e['GITLAB_TOKEN']
        self.h = {'PRIVATE-TOKEN': self._token}
        s = settings()
        self.project = (s.get('methodology_project') or '').strip('/')
        if need_project and not self.project:
            sys.exit('не задан репозиторий методологии: python3 scripts/wizard_state.py set methodology_project <группа/проект>')
        self.default_branch = s.get('default_branch') or ''
        self.branch = s.get('poc_branch') or ''          # ветка POC: в неё идут все merge request-ы мастера
        self.pid = urllib.parse.quote(self.project, safe='')

    def call(self, method, path, body=None):
        return http(method, self.base + path, self.h, body)

    def p(self, method, path, body=None):
        return self.call(method, '/projects/%s%s' % (self.pid, path), body)

    def raw(self, file_path, ref=None):
        """Содержимое файла как текст; None, если файла нет. Любой другой сбой — исключение, а не «файла нет»."""
        st, d = http('GET', self.base + '/projects/%s/repository/files/%s/raw?ref=%s' % (
            self.pid, urllib.parse.quote(file_path, safe=''), urllib.parse.quote(ref or self.branch, safe='')), self.h, as_text=True)
        if st == 200:
            return d
        if st == 404:
            return None
        raise RuntimeError('GitLab не отдал %s@%s: HTTP %s' % (file_path, ref or self.branch, st or d))

    def git_env(self):
        """Окружение для git: токен передаётся заголовком через переменные окружения, не через аргументы."""
        import base64
        env = dict(os.environ)
        env.update({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.extraHeader',
                    'GIT_CONFIG_VALUE_0': 'Authorization: Basic ' + base64.b64encode(('oauth2:' + self._token).encode()).decode(),
                    'GIT_TERMINAL_PROMPT': '0'})
        return env


class Gina:
    def __init__(self):
        e = load_env('gina.env', must=True)
        self.base = e['GINA_URL'].rstrip('/')
        self.h = {'Authorization': 'Bearer ' + e['GINA_OWNER_TOKEN']} if e.get('GINA_OWNER_TOKEN') else {}

    def call(self, method, path, body=None, auth=True, timeout=120):
        return http(method, self.base + path, self.h if auth else None, body, timeout)


def kubectl(args, timeout=60, stdin=None):
    """Запускает kubectl с kubeconfig и namespace из k8s.env. Возвращает (код, stdout, stderr)."""
    e = load_env('k8s.env', must=True)
    cmd = ['kubectl', '--kubeconfig', os.path.expanduser(e['KUBECONFIG_PATH']), '-n', e['K8S_NAMESPACE']] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, '', 'timeout %ss' % timeout
    except FileNotFoundError:
        return 127, '', 'kubectl не установлен'


WIZARD = 'wizard.json'
STEPS = {'1': 'доступы', '2': 'подключения', '3': 'проект', '4': 'методология', '5': 'сторож инстанса', '6': 'итоговая проверка', '7': 'итог и FAQ', '8': 'запуск сторожа'}


def now():
    import time
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def wizard():
    return load_state(WIZARD, {'version': 1, 'started': now(), 'completed': False, 'position': '', 'steps': {}, 'settings': {},
                               'decisions': [], 'confirmations': []})


def settings():
    return wizard().get('settings') or {}


def feature(name):
    """Включён ли признак (gitlab, jira, code_conventions, product_ui). Не задан — выключен."""
    return bool(((settings().get('features') or {}).get(name) or {}).get('on'))


def proxy_fetch(paths, timeout=120):
    """Читает адреса изнутри пространства инстанса, через прокси Gina — так же, как ходят агенты.
    paths — пути вида '/gitlab/api/v4/...', '/jira/rest/...', '/api/...'. Возвращает {путь: (статус, данные)}.
    Нужно право exec в под gina-evd. Только GET."""
    script = (r'const L="http://localhost:8080";let s="";process.stdin.on("data",d=>s+=d).on("end",async()=>{const out={};'
              r'for(const p of JSON.parse(s)){try{const r=await fetch(L+p);const t=await r.text();let b;try{b=JSON.parse(t)}catch{b=t.slice(0,200000)};'
              r'out[p]={s:r.status,b:b,h:{total:r.headers.get("x-total"),next:r.headers.get("x-next-page")}}}catch(e){out[p]={s:0,b:String(e.message)}}}'
              r'process.stdout.write(JSON.stringify(out))})')
    rc, out, err = kubectl(['exec', '-i', 'deploy/gina-evd', '--', 'node', '-e', script], timeout=timeout, stdin=json.dumps(list(paths)))
    if rc != 0:
        raise RuntimeError('чтение через прокси Gina не удалось (нужно право exec в под gina-evd): %s' % (err or out).strip()[:160])
    data = json.loads(out)
    return {p: (v.get('s'), v.get('b'), v.get('h') or {}) for p, v in data.items()}


def core_call(requests, timeout=120):
    """Запросы к ядру Gina изнутри его пода (kubectl exec), по внутреннему адресу — так же, как к ядру обращаются
    агенты анклава. requests — список {'method', 'path', 'body'?}; возвращает список (статус, данные) в том же порядке.
    Весь список, включая тела с секретами, уходит в под через стандартный ввод: в аргументах командной строки и
    в журналах kubectl секретов нет. Нужно право exec в под gina-evd."""
    script = (r'const L="http://localhost:8080";let s="";process.stdin.on("data",d=>s+=d).on("end",async()=>{const out=[];'
              r'for(const q of JSON.parse(s)){try{const h=Object.assign({},q.headers||{});if(q.body!==undefined)h["content-type"]="application/json";'
              r'const r=await fetch(L+q.path,{method:q.method||"GET",headers:h,body:q.body!==undefined?JSON.stringify(q.body):undefined});'
              r'const t=await r.text();let b;try{b=JSON.parse(t)}catch{b=t.slice(0,200000)};out.push({s:r.status,b:b})}'
              r'catch(e){out.push({s:0,b:String(e.message)})}}process.stdout.write(JSON.stringify(out))})')
    rc, out, err = kubectl(['exec', '-i', 'deploy/gina-evd', '--', 'node', '-e', script], timeout=timeout, stdin=json.dumps(list(requests)))
    if rc != 0:
        raise RuntimeError('обращение к ядру Gina через kubectl не удалось (нужно право exec в под gina-evd): %s' % (err or out).strip()[:160])
    return [(v.get('s'), v.get('b')) for v in json.loads(out)]


def confirm(action, text):
    """Записать в журнал дословное согласие оператора на конкретное действие."""
    w = wizard(); w['confirmations'].append({'ts': now(), 'action': action, 'text': text}); save_state(WIZARD, w)


def save_state(name, data):
    os.makedirs(STATE, exist_ok=True)
    path = os.path.join(STATE, name)
    json.dump(data, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return path


def load_state(name, default=None):
    path = os.path.join(STATE, name)
    return json.load(open(path, encoding='utf-8')) if os.path.exists(path) else default
