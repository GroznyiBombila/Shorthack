# app — серверный код

| Файл | Чей | Что |
|---|---|---|
| `auth.py`, `mailer.py`, `profile.py` | nyonless | подтверждение почты, письма, профили |
| `schedule_parser.py`, `schedule.py` | nyonless | разбор расписаний, без модели |
| `llm_client.py`, `prompts.py`, `analyze.py`, `compose.py` | alexa | вызовы Groq |
| `faq.py`, `tools.py`, `rules.py`, `router.py` | alexa | поиск, инструменты, решения |
| `storage.py`, `api.py` | alexa | SQLite и HTTP |

В чужой файл — только по договорённости. Контракты между модулями фиксируются до реализации.
