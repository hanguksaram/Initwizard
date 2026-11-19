#!/usr/bin/env python3
"""Шаг 2 мастера: сбор фактов о проекте. Только чтение. Ничего не пишет ни в репозитории, ни на инстанс, ни на стенды.

  python3 scripts/project_probe.py detect        — какие внешние системы подключены к инстансу (GitLab, Jira, каналы общения)
  python3 scripts/project_probe.py repos         — кодовая база: репозитории, стеки, ветки, README
  python3 scripts/project_probe.py ci            — сборки: конфигурации CI, последние сборки и причины отказов
  python3 scripts/project_probe.py conventions   — правила кода: документы стандартов и настройки линтеров (содержимое сохраняется)
  python3 scripts/project_probe.py tracker       — трекер задач: типы, статусы, переходы (сейчас — Jira)
  python3 scripts/project_probe.py env           — среда исполнения: пределы и инструменты пода исполнителя
  python3 scripts/project_probe.py stands        — стенды продукта: ответ адресов, поды в пространствах стендов
  python3 scripts/project_probe.py ui [--where local|pod] [--no-login]  — обход интерфейса продукта браузером без окна
  python3 scripts/project_probe.py show          — что уже собрано

Откуда читается код: через прокси Gina из пода ядра (тем же путём, что агенты; нужно право exec) либо, если так не
получилось, напрямую токеном из gitlab.env. Источник записывается в факты.

Результат: state/project/facts.json (разделы по командам, у каждого — время и источник), state/project/conventions/…,
state/project/ui.json. Это сырьё: навыки проекта по нему составляет агент (wizard/2-project.md).
"""
import json, os, re, sys, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c

OUT = os.path.join(c.STATE, 'project')
q = lambda s: urllib.parse.quote(str(s), safe='')
CONV_RX = re.compile(r'(lint|eslint|prettier|stylelint|steiger|editorconfig|golangci|revive|phpcs|ecs\.php|php-cs|php_cs|phpstan|psalm|rector|checkstyle|spotless|'
                     r'rubocop|flake8|ruff|pylintrc|mypy|pyproject\.toml|setup\.cfg|tox\.ini|clippy|rustfmt|ktlint|detekt|swiftlint|commitlint|\.husky/|'
                     r'CONTRIBUTING|CODEOWNERS|CODE_STYLE|STYLEGUIDE|docs?/standards?/|docs?/conventions?/|ARCHITECTURE)', re.I)
TEST_RX = re.compile(r'(_test\.go|Test\.java|Tests?\.kt|Cest\.php|Test\.php|\.(test|spec)\.[jt]sx?|(^|/)test_[^/]+\.py|_test\.py|_spec\.rb|Tests?\.cs)$')
SKIP_DIRS = re.compile(r'(^|/)(node_modules|vendor|\.yarn|dist|build|target|\.git)/')
TOOLS = ['git', 'node', 'npm', 'yarn', 'pnpm', 'python3', 'pip', 'go', 'java', 'mvn', 'gradle', 'php', 'composer', 'ruby', 'dotnet', 'cargo', 'make',
         'docker', 'kubectl', 'psql', 'jq', 'curl']


def facts():
    return c.load_state(os.path.join('project', 'facts.json'), {})


def save(section, data, source):
    os.makedirs(OUT, exist_ok=True)
    f = facts(); f[section] = {'ts': c.now(), 'source': source, 'data': data}
    c.save_state(os.path.join('project', 'facts.json'), f)


class Code:
    """Чтение GitLab: сначала через прокси Gina (как агенты), иначе напрямую токеном из gitlab.env."""
    def __init__(self):
        self.mode, self.note = None, ''
        try:
            r = c.proxy_fetch(['/gitlab/api/v4/user'])['/gitlab/api/v4/user']
            if r[0] == 200:
                self.mode, self.note = 'proxy', 'через прокси Gina, пользователь %s' % (r[1] or {}).get('username')
            else:
                self.note = 'прокси Gina к GitLab ответил HTTP %s' % r[0]
        except Exception as e:
            self.note = str(e)[:120]
        if not self.mode:
            try:
                self.g = c.GitLab(need_project=False)
                st, u = self.g.call('GET', '/user')
                if st == 200:
                    self.mode = 'direct'; self.note = 'напрямую токеном из gitlab.env, пользователь %s (%s)' % (u.get('username'), self.note)
            except SystemExit:
                pass
        if not self.mode:
            sys.exit('GitLab не читается ни через прокси Gina, ни напрямую: %s' % self.note)

    def many(self, paths):
        """{путь: (статус, данные, заголовки)}; пути — от /api/v4."""
        if self.mode == 'proxy':
            r = c.proxy_fetch(['/gitlab/api/v4' + p for p in paths], timeout=240)
            return {p: r['/gitlab/api/v4' + p] for p in paths}
        out = {}
        for p in paths:
            st, d = c.http('GET', self.g.base + p, self.g.h, as_text='/raw' in p)
            out[p] = (st, d, {})
        return out

    def one(self, path):
        return self.many([path])[path]


def code_base():
    cb = (c.settings().get('code_base') or '').strip('/')
    if not cb:
        sys.exit('не задана кодовая база: python3 scripts/wizard_state.py set code_base <путь группы или репозитория>')
    return cb


def list_repos(code, cb):
    st, d, _ = code.one('/groups/%s/projects?per_page=100&include_subgroups=true&simple=true&order_by=path&sort=asc' % q(cb))
    if st == 200 and isinstance(d, list) and d:
        return 'группа', d
    st, d, _ = code.one('/projects/%s' % q(cb))
    if st == 200 and isinstance(d, dict):
        return 'репозиторий', [d]
    sys.exit('по адресу «%s» не найдено ни группы, ни репозитория (HTTP %s) — уточни путь у оператора' % (cb, st))


def cmd_repos(_):
    code = Code(); cb = code_base(); kind, repos = list_repos(code, cb)
    repos = repos[:40]; res = []
    for r in repos:
        pid = r['id']; br = r.get('default_branch') or ''
        paths = {'lang': '/projects/%s/languages' % pid, 'tree': '/projects/%s/repository/tree?per_page=100&ref=%s' % (pid, q(br)),
                 'branches': '/projects/%s/repository/branches?per_page=100' % pid, 'tags': '/projects/%s/repository/tags?per_page=5' % pid,
                 'mrs': '/projects/%s/merge_requests?state=all&per_page=1' % pid, 'readme': '/projects/%s/repository/files/README.md/raw?ref=%s' % (pid, q(br))}
        got = code.many(list(paths.values())); g = lambda k: got[paths[k]]
        lang = g('lang')[1] if g('lang')[0] == 200 and isinstance(g('lang')[1], dict) else {}
        tree = g('tree')[1] if g('tree')[0] == 200 and isinstance(g('tree')[1], list) else []
        branches = g('branches')[1] if g('branches')[0] == 200 and isinstance(g('branches')[1], list) else []
        readme = g('readme')[1] if g('readme')[0] == 200 and isinstance(g('readme')[1], str) else ''
        res.append({'id': pid, 'path': r['path_with_namespace'], 'default_branch': br, 'last_activity': (r.get('last_activity_at') or '')[:10],
                    'languages': {k: round(v) for k, v in list(lang.items())[:5]}, 'top': [t['name'] + ('/' if t['type'] == 'tree' else '') for t in tree],
                    'branches': [b['name'] for b in branches][:20], 'branch_count': len(branches),
                    'tags_sample': [t['name'] for t in (g('tags')[1] if g('tags')[0] == 200 and isinstance(g('tags')[1], list) else [])],
                    'merge_requests_total': (g('mrs')[2] or {}).get('total'), 'has_ci_file': any(t['name'] == '.gitlab-ci.yml' for t in tree),
                    'readme_head': re.sub(r'\n{3,}', '\n\n', readme)[:1800]})
    save('repos', {'code_base': cb, 'kind': kind, 'repos': res}, code.note)
    print('Кодовая база «%s» (%s), репозиториев: %d. Источник: %s' % (cb, kind, len(res), code.note))
    for r in res:
        print('  %-40s ветка %-8s веток %-3s MR %-4s CI-файл %-3s %s' % (r['path'][-40:], r['default_branch'], r['branch_count'], r['merge_requests_total'],
              'да' if r['has_ci_file'] else 'нет', ', '.join('%s %s%%' % kv for kv in list(r['languages'].items())[:3])))
    print('README и состав каталогов сохранены в state/project/facts.json — назначение репозиториев выводи из них, а не из названий.')


def repos_or_exit():
    f = facts().get('repos')
    if not f:
        sys.exit('сначала: python3 scripts/project_probe.py repos')
    return f['data']['repos']


def cmd_ci(_):
    code = Code(); res = []
    for r in repos_or_exit():
        pid = r['id']
        paths = {'ci': '/projects/%s/repository/files/.gitlab-ci.yml/raw?ref=%s' % (pid, q(r['default_branch'])), 'pl': '/projects/%s/pipelines?per_page=5' % pid}
        got = code.many(list(paths.values()))
        ci = got[paths['ci']][1] if got[paths['ci']][0] == 200 and isinstance(got[paths['ci']][1], str) else ''
        pls = got[paths['pl']][1] if got[paths['pl']][0] == 200 and isinstance(got[paths['pl']][1], list) else []
        item = {'path': r['path'], 'has_ci': bool(ci), 'pipelines': [{'id': p['id'], 'status': p['status'], 'ref': p['ref'], 'created': p['created_at'][:16]} for p in pls]}
        if ci:
            m = re.search(r'^stages:\s*\n((?:\s+- .*\n)+)', ci, re.M)
            item['stages'] = re.findall(r'- *["\']?([^"\'\n]+)', m.group(1)) if m else []
            item['jobs'] = [j for j in re.findall(r'^([A-Za-z][\w:. -]*):\s*$', ci, re.M) if j not in ('variables', 'stages', 'default', 'workflow', 'include', 'cache', 'before_script', 'after_script', 'services', 'image')][:40]
            item['runner_tags'] = sorted({(a or b).strip(' "\'') for a, b in re.findall(r'tags:\s*\n\s+- ([^\n]+)|tags:\s*\[([^\]]+)\]', ci)})
            item['images'] = sorted({i.split('@')[0] for i in re.findall(r'image:\s*"?([^\s"]+)', ci)})[:10]
            item['commands'] = list(dict.fromkeys(re.findall(r'^\s+- ((?:go|make|composer|yarn|npm|pnpm|mvn|gradle|\./gradlew|php|vendor/bin|pytest|python|ruff|flake8|tox|cargo|dotnet|bundle|rake|\./|bash|sh|golangci|eslint)[^\n]{0,140})', ci, re.M)))[:30]
            item['includes'] = re.findall(r'^\s*-?\s*(?:project|local|remote|template):\s*["\']?([^"\'\n]+)', ci, re.M)[:10]
        bad = next((p for p in pls if p['status'] in ('failed', 'canceled')), None)
        if bad:
            st, jobs, _ = code.one('/projects/%s/pipelines/%s/jobs?per_page=50' % (pid, bad['id']))
            item['last_failed'] = {'pipeline': bad['id'], 'jobs': [{'name': j['name'], 'stage': j['stage'], 'status': j['status'], 'failure_reason': j.get('failure_reason')} for j in (jobs if st == 200 and isinstance(jobs, list) else [])][:30]}
        res.append(item)
    save('ci', res, code.note)
    print('Сборки по %d репозиториям. Источник: %s' % (len(res), code.note))
    for i in res:
        last = i['pipelines'][0] if i['pipelines'] else None
        why = sorted({j['failure_reason'] for j in (i.get('last_failed') or {}).get('jobs', []) if j.get('failure_reason')})
        print('  %-40s CI %-3s последняя сборка: %-28s %s' % (i['path'][-40:], 'да' if i['has_ci'] else 'нет',
              ('%s от %s (%s)' % (last['status'], last['created'][:10], last['ref'])) if last else 'сборок не было', ('причины отказов: ' + ', '.join(why)) if why else ''))


def cmd_conventions(_):
    code = Code(); res = []; base = os.path.join(OUT, 'conventions')
    for r in repos_or_exit():
        pid, br = r['id'], r['default_branch']; files = []; page = '1'
        while page and len(files) < 3000:
            st, t, h = code.one('/projects/%s/repository/tree?recursive=true&per_page=100&page=%s&ref=%s' % (pid, page, q(br)))
            if st != 200 or not isinstance(t, list) or not t:
                break
            files += [x['path'] for x in t if x['type'] == 'blob' and not SKIP_DIRS.search(x['path'])]
            page = (h or {}).get('next') or (str(int(page) + 1) if len(t) == 100 and code.mode == 'direct' else '')
        found = sorted([f for f in files if CONV_RX.search(f) and f.count('/') <= 3], key=lambda f: (0 if re.search(r'(?i)standards?/|conventions?/|CONTRIBUTING|CODE_STYLE|STYLEGUIDE|ARCHITECTURE', f) else 1, f))
        conv = found[:30]
        got = code.many(['/projects/%s/repository/files/%s/raw?ref=%s' % (pid, q(f), q(br)) for f in conv]) if conv else {}
        saved = []
        for f in conv:
            st, body, _ = got['/projects/%s/repository/files/%s/raw?ref=%s' % (pid, q(f), q(br))]
            if st == 200 and isinstance(body, (str, dict, list)):
                text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=1)
                d = os.path.join(base, r['path'].replace('/', '__')); os.makedirs(d, exist_ok=True)
                open(os.path.join(d, f.replace('/', '__')), 'w', encoding='utf-8').write(text[:8000]); saved.append(f)
        st, commits, _ = code.one('/projects/%s/repository/commits?per_page=15&ref_name=%s' % (pid, q(br)))
        res.append({'path': r['path'], 'files_total': len(files), 'test_files': sum(1 for f in files if TEST_RX.search(f)), 'convention_files': saved,
                    'convention_files_not_read': [f for f in found if f not in saved],
                    'top_dirs': sorted({f.split('/')[0] for f in files if '/' in f})[:25],
                    'commit_titles': [x['title'][:100] for x in (commits if st == 200 and isinstance(commits, list) else [])]})
    save('conventions', res, code.note)
    print('Правила кода по %d репозиториям. Источник: %s' % (len(res), code.note))
    for i in res:
        print('  %-40s файлов %-5s тестовых %-4s правила: %s' % (i['path'][-40:], i['files_total'], i['test_files'], ', '.join(i['convention_files']) or 'не найдены'))
        if i['convention_files_not_read']:
            print('  %-40s НЕ прочитано (сверх предела 30 файлов или отказ чтения): %s' % ('', ', '.join(i['convention_files_not_read'][:12]) + (' и ещё %d' % (len(i['convention_files_not_read']) - 12) if len(i['convention_files_not_read']) > 12 else '')))
    print('Содержимое прочитанных файлов правил (первые 8 КБ каждого): state/project/conventions/<путь репозитория, где «/» заменён на «__»>/ — читай их перед составлением навыка.')


def cmd_detect(_):
    try:
        st, d, _ = c.proxy_fetch(['/api/adapters/binding-usage'])['/api/adapters/binding-usage']; source = 'изнутри пространства инстанса'
    except Exception as e:
        gi = c.Gina(); st, d = gi.call('GET', '/api/adapters/binding-usage'); source = 'через внешний API (изнутри не получилось: %s)' % str(e)[:80]
    if st != 200 or not isinstance(d, dict):
        sys.exit('список подключений инстанса не читается: HTTP %s' % st)
    items = d.get('items') or []
    out = [{'name': x.get('display_name'), 'direction': x.get('direction'), 'state': x.get('state'), 'alias': (x.get('alias_url') or '').rsplit('/', 1)[-1] if x.get('alias_url') else None} for x in items]
    save('connections', out, source)
    aliases = {x['alias'] for x in out if x['alias'] and x['state'] == 'active'}
    print('Подключения инстанса (%s):' % source)
    for x in out:
        print('  %-40s %-13s %s%s' % (str(x['name'])[:40], x['direction'], x['state'], (' — короткий адрес /' + x['alias']) if x['alias'] else ''))
    print('GitLab через прокси: %s. Jira через прокси: %s. Каналы общения: %s.' % (
        'есть' if 'gitlab' in aliases else 'НЕТ', 'есть' if 'jira' in aliases else 'НЕТ', ', '.join(str(x['name']) for x in out if x['direction'] == 'conversation') or 'нет'))
    print('Запиши признаки: python3 scripts/wizard_state.py feature gitlab on|off "<основание>" и то же для jira.')


def cmd_tracker(_):
    key = c.settings().get('tracker_project')
    if not key:
        sys.exit('не задан ключ проекта в трекере: python3 scripts/wizard_state.py set tracker_project <КЛЮЧ>')
    P = '/jira/rest/api/2'
    paths = [P + '/myself', P + '/project/' + q(key), P + '/project/%s/statuses' % q(key), P + '/search?jql=project%%3D%s%%20ORDER%%20BY%%20created%%20DESC&maxResults=5&fields=summary,status,issuetype' % q(key)]
    r = c.proxy_fetch(paths)
    proj, sts, srch = r[paths[1]], r[paths[2]], r[paths[3]]
    if proj[0] != 200:
        sys.exit('проект «%s» в Jira через прокси Gina не читается: HTTP %s' % (key, proj[0]))
    issues = (srch[1] or {}).get('issues') or [] if srch[0] == 200 else []
    trans = []
    if issues:
        t = c.proxy_fetch([P + '/issue/%s/transitions' % issues[0]['key']])[P + '/issue/%s/transitions' % issues[0]['key']]
        trans = [{'name': x['name'], 'to': x['to']['name']} for x in ((t[1] or {}).get('transitions') or [])] if t[0] == 200 else []
    data = {'key': key, 'name': proj[1].get('name'), 'user': (r[paths[0]][1] or {}).get('name') if r[paths[0]][0] == 200 else None,
            'issue_types': [x['name'] for x in proj[1].get('issueTypes') or []],
            'statuses': sorted({s['name'] for t in (sts[1] if sts[0] == 200 and isinstance(sts[1], list) else []) for s in t.get('statuses', [])}),
            'issues_total': (srch[1] or {}).get('total') if srch[0] == 200 else None,
            'sample': [{'key': i['key'], 'status': i['fields']['status']['name'], 'type': i['fields']['issuetype']['name'], 'summary': i['fields']['summary'][:80]} for i in issues],
            'sample_transitions': {'issue': issues[0]['key'] if issues else None, 'transitions': trans}}
    save('tracker', data, 'через прокси Gina, пользователь %s' % data['user'])
    print('Jira, проект %s «%s»: типы %s; статусы %s; задач %s' % (key, data['name'], ', '.join(data['issue_types']), ', '.join(data['statuses']), data['issues_total']))
    if trans:
        print('Переходы, доступные техническому пользователю у %s: %s' % (issues[0]['key'], ', '.join('%s → %s' % (x['name'], x['to']) for x in trans)))
    print('Какие переходы РАЗРЕШЕНЫ агентам — решает оператор: python3 scripts/wizard_state.py set agent_transitions "<перечень или «не разрешено»>"')


def cmd_env(_):
    rc, out, err = c.kubectl(['get', 'pods', '-o', 'json'])
    if rc != 0:
        sys.exit('поды не читаются: %s' % (err or out).strip()[:160])
    res = []
    for p in json.loads(out)['items']:
        name = p['metadata']['name']
        if not re.search(r'(jcode|opencode)-agent', name):
            continue
        cont = p['spec']['containers'][0]; lim = (cont.get('resources') or {}).get('limits') or {}
        item = {'pod': name, 'container': cont['name'], 'memory_limit': lim.get('memory'), 'cpu_limit': lim.get('cpu'), 'tools': None}
        cmd = 'for t in %s; do command -v $t >/dev/null 2>&1 && echo "+$t" || echo "-$t"; done; (node -v; python3 -V; java -version 2>&1 | head -1; go version; php -v | head -1) 2>/dev/null; python3 -c "import playwright; print(\\"+playwright\\")" 2>/dev/null' % ' '.join(TOOLS)
        rc2, o, e = c.kubectl(['exec', name, '-c', cont['name'], '--', 'sh', '-c', cmd], timeout=60)
        if rc2 == 0:
            lines = o.split('\n')
            item['tools'] = {'есть': [l[1:] for l in lines if l.startswith('+')], 'нет': [l[1:] for l in lines if l.startswith('-')]}
            item['versions'] = [l for l in lines if l and l[0] not in '+-'][:6]
        else:
            item['tools_note'] = 'состав инструментов не прочитан (нет права exec): %s' % (e or o).strip()[:80]
        res.append(item)
    save('env', res, 'kubectl, пространство инстанса')
    for i in res:
        print('%s: память %s, процессор %s' % (i['pod'], i['memory_limit'] or 'без лимита', i['cpu_limit'] or 'без лимита'))
        if i['tools']:
            print('  есть: %s\n  нет:  %s\n  версии: %s' % (', '.join(i['tools']['есть']), ', '.join(i['tools']['нет']), '; '.join(i.get('versions', []))))
        else:
            print('  ' + i['tools_note'])
    if not res:
        print('подов исполнителя (jcode-agent / opencode-agent) в пространстве не найдено')


def cmd_stands(_):
    s = c.settings(); urls = [u.strip() for u in (s.get('ui_urls') or '').split(',') if u.strip()]; nss = [n.strip() for n in (s.get('product_namespaces') or '').split(',') if n.strip()]
    res = {'urls': [], 'namespaces': []}
    for u in urls:
        st, body = c.http('GET', u, timeout=15, as_text=True)
        title = re.search(r'<title>([^<]{0,80})', body or '') if isinstance(body, str) else None
        res['urls'].append({'url': u, 'status': st, 'title': title.group(1).strip() if title else None})
        print('%s → HTTP %s%s' % (u, st or 'нет связи', (' «%s»' % title.group(1).strip()) if title else ''))
    env = c.load_env('k8s.env', must=True)
    for ns in nss:
        import subprocess
        r = subprocess.run(['kubectl', '--kubeconfig', os.path.expanduser(env['KUBECONFIG_PATH']), '-n', ns, 'get', 'pods', '-o', 'json'], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            res['namespaces'].append({'namespace': ns, 'error': (r.stderr or r.stdout).strip()[:120]}); print('пространство %s: не читается (%s)' % (ns, (r.stderr or r.stdout).strip()[:80])); continue
        pods = [{'name': p['metadata']['name'], 'phase': p['status'].get('phase'), 'image': p['spec']['containers'][0]['image'].split('/')[-1][:60]} for p in json.loads(r.stdout)['items']]
        res['namespaces'].append({'namespace': ns, 'pods': pods}); print('пространство %s: подов %d (%s)' % (ns, len(pods), ', '.join('%s:%s' % (p['name'].rsplit('-', 2)[0], p['phase']) for p in pods)[:160]))
    if not urls and not nss:
        print('адреса интерфейса и пространства стендов не заданы (wizard_state.py set ui_urls / product_namespaces)')
    save('stands', res, 'запросы с машины мастера и kubectl')


CRAWL = r'''
import json, sys
from urllib.parse import urlsplit, urljoin
from playwright.sync_api import sync_playwright
cfg = json.loads(sys.stdin.read()); out = []
def snap(pg):
    return {"url": pg.url.split("?")[0], "title": pg.title()[:80],
            "headings": pg.eval_on_selector_all("h1,h2,h3", "e=>e.map(x=>x.innerText.trim()).filter(Boolean).slice(0,12)"),
            "buttons": pg.eval_on_selector_all("button,[role=button]", "e=>[...new Set(e.map(x=>x.innerText.trim()).filter(t=>t&&t.length<40))].slice(0,20)"),
            "inputs": pg.eval_on_selector_all("input,select,textarea", "e=>e.map(x=>(x.name||x.id||x.placeholder||x.type)).filter(Boolean).slice(0,15)")}
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    for start in cfg["urls"]:
        ctx = b.new_context(ignore_https_errors=True); pg = ctx.new_page(); rec = {"start": start, "pages": []}
        try:
            r = pg.goto(start, wait_until="domcontentloaded", timeout=30000); pg.wait_for_timeout(2500)
            rec["status"] = r.status if r else None; rec["landed"] = pg.url.split("?")[0]; rec["first"] = snap(pg)
            has_pw = pg.locator("input[type=password]").count() > 0
            rec["login_form"] = has_pw; rec["logged_in"] = False
            if has_pw and cfg.get("user"):
                pg.locator("input[type=text],input[type=email],input[name=username],input:not([type])").first.fill(cfg["user"])
                pg.locator("input[type=password]").first.fill(cfg["password"])
                pg.locator("input[type=password]").first.press("Enter"); pg.wait_for_timeout(5000)
                rec["logged_in"] = pg.locator("input[type=password]").count() == 0; rec["after_login"] = snap(pg)
            origin = urlsplit(pg.url).netloc
            links = pg.eval_on_selector_all("a[href]", "e=>e.map(x=>[x.href,x.innerText.trim().slice(0,40)])")
            seen, todo = set(), []
            for href, text in links:
                u = urlsplit(href)
                if u.netloc == origin and u.path not in seen and not any(w in (text + u.path).lower() for w in ("logout", "signout", "выйти", "выход", "delete", "удал")):
                    seen.add(u.path); todo.append((href, text))
            rec["nav"] = [{"path": urlsplit(h).path, "text": t} for h, t in todo][:60]
            for href, text in todo[:cfg.get("max_pages", 20)]:      # только переходы по ссылкам: формы не отправляются, кнопки не нажимаются
                try:
                    pg.goto(href, wait_until="domcontentloaded", timeout=20000); pg.wait_for_timeout(1500)
                    s = snap(pg); s["link_text"] = text; rec["pages"].append(s)
                except Exception as e:
                    rec["pages"].append({"url": href, "error": str(e)[:100]})
        except Exception as e:
            rec["error"] = str(e)[:200]
        out.append(rec); ctx.close()
    b.close()
print("\n@@UI@@" + json.dumps(out, ensure_ascii=False))
'''


def cmd_ui(a):
    urls = [u.strip() for u in (c.settings().get('ui_urls') or '').split(',') if u.strip()]
    if not urls:
        sys.exit('не заданы адреса интерфейса: python3 scripts/wizard_state.py set ui_urls <адрес[,адрес]>')
    cred = {} if a.no_login else c.load_env('ui.env'); cfg = json.dumps({'urls': urls, 'user': cred.get('UI_USER') or None, 'password': cred.get('UI_PASSWORD') or None, 'max_pages': 20})
    import subprocess
    where = a.where
    if where == 'auto':
        where = 'local' if subprocess.run([sys.executable, '-c', 'import playwright'], capture_output=True).returncode == 0 else 'pod'
    if where == 'local':
        r = subprocess.run([sys.executable, '-c', CRAWL], input=cfg, capture_output=True, text=True, timeout=600); out, err, rc = r.stdout, r.stderr, r.returncode
    else:
        rc0, pods, _ = c.kubectl(['get', 'pods', '-o', 'name'])
        pod = next((x.split('/')[-1] for x in pods.split() if re.search(r'(jcode|opencode)-agent', x)), None)
        if not pod:
            sys.exit('на этой машине нет Playwright, а пода исполнителя в пространстве не найдено — обход интерфейса выполнить нечем')
        loader = 'import sys,io;src,cfg=sys.stdin.read().split(chr(10)+"@@CFG@@"+chr(10));sys.stdin=io.StringIO(cfg);exec(src)'
        rc, out, err = c.kubectl(['exec', '-i', pod, '--', 'python3', '-c', loader], timeout=600, stdin=CRAWL + '\n@@CFG@@\n' + cfg)
    if '@@UI@@' not in out:
        sys.exit('обход не выполнен (%s): %s' % (where, (err or out).strip()[-300:]))
    data = json.loads(out.split('@@UI@@', 1)[1])
    os.makedirs(OUT, exist_ok=True); c.save_state(os.path.join('project', 'ui.json'), data)
    save('ui', [{'start': d.get('start'), 'status': d.get('status'), 'landed': d.get('landed'), 'login_form': d.get('login_form'), 'logged_in': d.get('logged_in'),
                 'nav_links': len(d.get('nav') or []), 'pages_visited': len(d.get('pages') or []), 'error': d.get('error')} for d in data],
         'headless-браузер, %s; учётная запись %s' % ('на машине мастера' if where == 'local' else 'в поде исполнителя', 'использована' if cred.get('UI_USER') else 'не задана'))
    for d in data:
        print('%s → HTTP %s, открылось %s' % (d.get('start'), d.get('status'), d.get('landed')))
        if d.get('error'):
            print('  ошибка: %s' % d['error']); continue
        print('  страница входа: %s; вход выполнен: %s; ссылок навигации: %d; страниц пройдено: %d' % (
            'да' if d.get('login_form') else 'нет', 'да' if d.get('logged_in') else 'нет', len(d.get('nav') or []), len(d.get('pages') or [])))
        for pg in (d.get('pages') or [])[:8]:
            print('    %-40s %s' % (str(pg.get('url', ''))[-40:], '; '.join(pg.get('headings') or [])[:80]))
    print('Полный результат: state/project/ui.json. Обход только открывает ссылки: формы не отправляет, кнопки не нажимает.')


def cmd_show(_):
    f = facts()
    if not f:
        print('фактов о проекте ещё нет'); return
    for k, v in f.items():
        n = len(v['data']) if isinstance(v['data'], list) else len(v['data'].get('repos', v['data'])) if isinstance(v['data'], dict) else 1
        print('%-12s собрано %s UTC; источник: %s; записей: %s' % (k, v['ts'][:16].replace('T', ' '), v['source'], n))


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest='cmd', required=True)
    for n in ('detect', 'repos', 'ci', 'conventions', 'tracker', 'env', 'stands', 'show'):
        sub.add_parser(n)
    pu = sub.add_parser('ui'); pu.add_argument('--where', default='auto', choices=['auto', 'local', 'pod']); pu.add_argument('--no-login', action='store_true', help='не входить, даже если в ui.env есть учётная запись')
    a = ap.parse_args()
    {'detect': cmd_detect, 'repos': cmd_repos, 'ci': cmd_ci, 'conventions': cmd_conventions, 'tracker': cmd_tracker, 'env': cmd_env, 'stands': cmd_stands,
     'ui': cmd_ui, 'show': cmd_show}[a.cmd](a)
