#!/usr/bin/env python3
"""Ветка POC: методология ставится в ОТДЕЛЬНУЮ ветку репозитория, начатую с пустого каталога.
Существующая рабочая ветка репозитория (и всё, что в ней лежит) не читается, не меняется и не учитывается.

  python3 scripts/branch_setup.py inspect                              — что в репозитории и от чего можно начать
  python3 scripts/branch_setup.py create --name <ветка> --confirmed-by "<ответ оператора>"

Как выбирается начало ветки:
  1) у репозитория есть первый коммит с пустым каталогом (нет agents/, skills/, roles/, automations/) — ветка
     создаётся от него;
  2) такого коммита нет — создаётся ветка без истории (git orphan) с одним файлом README.md.
"""
import argparse, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c

CATALOG = {'agents', 'skills', 'roles', 'automations', 'interlocutors'}
README = '''# Методология — ветка POC

Ветка начата с пустого каталога мастером установки. В неё по одному merge request-у ставятся
навыки, агенты и правила ролей минимальной методологии. Инстанс Gina читает эту ветку только
после явного переключения источника промптов.
'''


def roots(g):
    """Первые коммиты рабочей ветки репозитория и признак «каталог пуст»."""
    st, p = g.p('GET', '')
    if st != 200:
        sys.exit('репозиторий %s не читается: HTTP %s' % (g.project, st))
    default = p.get('default_branch')
    out, page = [], 1
    while True:
        st, cs = g.p('GET', '/repository/commits?ref_name=%s&per_page=100&page=%d' % (default, page))
        if st != 200 or not cs:
            break
        out += [x for x in cs if not x.get('parent_ids')]
        page += 1
    res = []
    for r in out:
        st, tree = g.p('GET', '/repository/tree?ref=%s&per_page=100' % r['id'])
        names = {i['path'] for i in tree} if st == 200 and isinstance(tree, list) else set()
        res.append({'sha': r['id'], 'title': r['title'], 'date': r['created_at'][:10], 'bare': not (names & CATALOG), 'top': sorted(names)})
    return default, res


def cmd_inspect(_):
    g = c.GitLab()
    default, rs = roots(g)
    st, br = g.p('GET', '/repository/branches?per_page=100')
    print('Репозиторий: %s' % g.project)
    print('Рабочая ветка репозитория: %s (мастер её не читает и не меняет)' % default)
    print('Ветки: %s' % ', '.join(b['name'] for b in br) if st == 200 else 'ветки не читаются')
    for r in rs:
        print('Первый коммит %s от %s «%s»: %s' % (r['sha'][:8], r['date'], r['title'][:60],
              'каталог пуст — ветку POC можно начать от него' if r['bare'] else 'каталог НЕ пуст (%s)' % ', '.join(r['top'])))
    if not any(r['bare'] for r in rs):
        print('Пустого первого коммита нет. Ветка POC будет создана без истории (отдельное начало), с одним файлом README.md.')
    s = c.settings()
    if s.get('poc_branch'):
        st, b = g.p('GET', '/repository/branches/' + c.urllib.parse.quote(s['poc_branch'], safe=''))
        print('Ветка POC в журнале: %s — %s' % (s['poc_branch'], 'существует, голова ' + b['commit']['short_id'] if st == 200 else 'ещё не создана'))


def cmd_create(a):
    if not (a.confirmed_by or '').strip():
        sys.exit('нужен дословный ответ оператора: --confirmed-by "..."')
    g = c.GitLab()
    default, rs = roots(g)
    if a.name == default:
        sys.exit('имя совпадает с рабочей веткой репозитория — выбери другое')
    st, b = g.p('GET', '/repository/branches/' + c.urllib.parse.quote(a.name, safe=''))
    if st == 200:
        st2, tree = g.p('GET', '/repository/tree?ref=%s&per_page=100' % c.urllib.parse.quote(a.name, safe=''))
        names = {i['path'] for i in tree} if st2 == 200 else set()
        if names & CATALOG and not c.load_state('rollout.json'):
            sys.exit('ветка %s уже существует и в ней есть каталог (%s), а мастер её не создавал — выбери другое имя' % (a.name, ', '.join(sorted(names & CATALOG))))
        how = 'уже существовала'
    else:
        bare = [r for r in rs if r['bare']]
        if bare:
            st, r = g.p('POST', '/repository/branches?branch=%s&ref=%s' % (c.urllib.parse.quote(a.name, safe=''), bare[0]['sha']))
            if st >= 300:
                sys.exit('ветка не создана: HTTP %s %s' % (st, str(r)[:200]))
            how = 'создана от пустого первого коммита %s' % bare[0]['sha'][:8]
        else:
            remote = os.environ.get('GINA_POC_GIT_REMOTE') or '%s/%s.git' % (g.url, g.project)
            with tempfile.TemporaryDirectory() as d:
                def git(*args):
                    r = subprocess.run(['git', '-C', d] + list(args), capture_output=True, text=True, env=g.git_env(), timeout=120)
                    if r.returncode != 0:
                        sys.exit('git %s: %s' % (args[0], (r.stderr or r.stdout).strip()[:300]))
                git('init', '-q'); git('checkout', '-q', '--orphan', a.name)
                open(os.path.join(d, 'README.md'), 'w', encoding='utf-8').write(README)
                git('add', 'README.md')
                git('-c', 'user.name=gina-poc-wizard', '-c', 'user.email=gina-poc-wizard@localhost', 'commit', '-q', '-m', 'POC: начало ветки с пустого каталога')
                git('push', '-q', remote, 'HEAD:refs/heads/' + a.name)
            how = 'создана без истории (отдельное начало)'
        st, b = g.p('GET', '/repository/branches/' + c.urllib.parse.quote(a.name, safe=''))
        if st != 200 and not os.environ.get('GINA_POC_GIT_REMOTE'):
            sys.exit('ветка отправлена, но GitLab её не показывает (HTTP %s) — проверь права токена на запись' % st)
    w = c.wizard(); w['settings'].update({'poc_branch': a.name, 'default_branch': default}); c.save_state(c.WIZARD, w)
    c.confirm('создание ветки POC ' + a.name, a.confirmed_by)
    print('Ветка POC «%s» %s. Все merge request-ы мастера пойдут в неё. Рабочая ветка «%s» не тронута.' % (a.name, how, default))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('inspect')
    p = sub.add_parser('create'); p.add_argument('--name', required=True); p.add_argument('--confirmed-by', default='')
    a = ap.parse_args()
    {'inspect': cmd_inspect, 'create': cmd_create}[a.cmd](a)
