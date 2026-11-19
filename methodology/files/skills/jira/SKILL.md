# Jira через прокси Gina

Какой проект Jira относится к работе и какие у него статусы — в навыке `project`, раздел «Задачи». Здесь — механика.

## Доступ

API: `http://gina-evd:8080/jira/rest/api/2/...` — обычный API Jira, учётные данные подставляет Gina (технический пользователь проекта). Токен не клади и не спрашивай.

## Прочитать задачу — целиком

```bash
J=http://gina-evd:8080/jira/rest/api/2
curl -s "$J/issue/<КЛЮЧ>?fields=summary,description,status,issuetype,assignee,reporter,labels,components,issuelinks,subtasks,attachment,comment" \
 | jq '{key, s: .fields.summary, status: .fields.status.name, type: .fields.issuetype.name, reporter: .fields.reporter.name, links: [.fields.issuelinks[]? | (.outwardIssue.key // .inwardIssue.key)], comments: [.fields.comment.comments[] | {a: .author.name, t: .created, b: .body}]}'
```

Описание, все комментарии, связанные задачи и вложения — часть постановки. Поздний комментарий заказчика отменяет раннее описание.

## Статусы

Доступные переходы у задачи бери запросом, не по памяти:

```bash
curl -s "$J/issue/<КЛЮЧ>/transitions" | jq '.transitions[] | {id, name, to: .to.name}'
```

Агенту разрешено только то, что названо в навыке `project`, раздел «Задачи»: обычно это перевод в работу, когда начата реализация, и перевод на ревью, когда merge request передан человеку. Раздел не называет переходов — статус не меняй, оставь комментарий.

Агенту запрещено всегда: брать в работу задачу из бэклога без прямого указания человека; переводить задачу в завершённые и закрытые статусы; менять исполнителя, приоритет и описание. Нужного перехода нет в списке — не обходи: напиши об этом в итоге захода.

## Комментарий

Комментарий оставляй на ключевых точках: план готов, merge request открыт (ссылка), нужен ответ, работа остановлена. Коротко, на языке команды, без внутренних идентификаторов платформы:

```bash
curl -s -X POST "$J/issue/<КЛЮЧ>/comment" -H 'Content-Type: application/json' \
  -d "$(jq -n --arg b 'План разбора: …' '{body: $b}')" | jq '{id, created}'
```

После записи перечитай задачу и убедись, что комментарий или статус на месте.
