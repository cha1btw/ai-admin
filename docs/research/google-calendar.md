# Google Calendar: календарь мастера через сервисный аккаунт

Проверено по официальной документации Google 21.10.2026.

## Авторизация сервисного аккаунта

Без OAuth-экрана и проверки приложения Google: мастер открывает доступ к своему календарю для email сервисного аккаунта («Вносить изменения в мероприятия»), а сервер получает токен сам. [service-account]

- JWT: заголовок `{"alg": "RS256", "typ": "JWT"}`; поля `iss` (email сервисного аккаунта), `scope` (через пробел), `aud` = `https://oauth2.googleapis.com/token`, `iat`, `exp` (не больше 1 часа после `iat`). Подпись только RS256. [service-account]
- Обмен: `POST https://oauth2.googleapis.com/token`, `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`, `assertion=<JWT>`. Токен живёт `expires_in` секунд (в примере 3600). [service-account]
- Своя реализация в `app/scheduling/google_auth.py` (библиотека `cryptography` уже в зависимостях), без google-auth и requests.

## Свободное время

- `POST https://www.googleapis.com/calendar/v3/freeBusy` с `timeMin`, `timeMax` (RFC3339), `items[].id`. Ответ: `calendars.{id}.busy[]` с `start` (включительно) и `end` (исключительно), ошибки в `calendars.{id}.errors[]`; список причин может расширяться. Скоупы: подходит `calendar`. [freebusy]
- Событие с `transparency: transparent` показывается как «свободен» и в занятость не попадает; по умолчанию события `opaque`. [events-insert]
- **Целодневные события** в интерфейсе Google по умолчанию создаются как «свободен» — это по сторонним источникам, в документации Google не подтверждено. Поэтому мы дополнительно читаем список событий и считаем целодневные события закрытым днём (`google.all_day_events_block`, по умолчанию включено). Проверить на тестовом календаре.

## Запись

- `POST https://www.googleapis.com/calendar/v3/calendars/{calendarId}/events`, обязательны `start` и `end` (`dateTime` с часовым поясом или `date`). Используем `summary`, `description`, `extendedProperties.private` (id нашей записи). Параметр `sendUpdates` по умолчанию не рассылает приглашения. [events-insert]
- Сервисный аккаунт может добавлять участников только с domain-wide delegation, поэтому клиенток в участники не добавляем. [events-insert]

## Гонки

У Google нет «забронировать, если свободно». Защита от двойной записи у нас: блокировка в Postgres на мастера, перепроверка `freeBusy` внутри транзакции, ограничение `EXCLUDE` на наши записи. Окно гонки с ручным событием мастера — доли секунды; принимаем этот риск.

## Не проверено

- Параметры `events.list` (`singleEvents`, `timeMin`, `timeMax`) — используем стандартные, проверить на тестовом календаре.
- Квоты API.

## Источники

- [service-account] https://developers.google.com/identity/protocols/oauth2/service-account
- [freebusy] https://developers.google.com/calendar/api/v3/reference/freebusy/query
- [events-insert] https://developers.google.com/calendar/api/v3/reference/events/insert
