# Instagram API with Instagram Login: что важно для бота

Проверено по официальной документации Meta 21.10.2026. Пометка «не проверено» значит, что страница недоступна или молчит; такие пункты подтверждаем на живом тесте (Фаза 6).

## Доступ

- Нужен **профессиональный аккаунт** Instagram (бизнес или автор). [overview]
- **Standard Access** — уровень по умолчанию: приложение работает с аккаунтами, у владельцев которых есть роль в приложении, или с вашими собственными. [overview]
- **Advanced Access** нужен, чтобы обслуживать аккаунты, которыми вы не владеете и не управляете. Требует **App Review и Business Verification**. [overview]
- **Вебхуки** приходят пользователям приложения, только когда приложение опубликовано (**Live**). Для продакшн-доступа нужна Business Verification. [webhooks-setup]

**Вывод:** пилот на 1–3 студии — через роль тестера и Live-приложение; для продаж — ФОП, Business Verification и App Review на `instagram_business_manage_messages`. Это самый долгий путь в проекте, начинать сразу (`docs/founders.md`).

## Вход студии (Business Login)

1. Ссылка `https://www.instagram.com/oauth/authorize` с параметрами `client_id`, `redirect_uri`, `response_type=code`, `scope` (через запятую: `instagram_business_basic,instagram_business_manage_messages`), `state` (защита от CSRF). Код в ответе живёт 1 час и одноразовый. [business-login]
2. Короткий токен: `POST https://api.instagram.com/oauth/access_token`, форма `client_id`, `client_secret`, `grant_type=authorization_code`, `redirect_uri`, `code`. Ответ: `access_token`, `user_id`, `permissions` (в массиве `data`). [business-login]
3. Долгий токен: `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret=...&access_token=...`. Ответ: `access_token`, `token_type`, `expires_in`. Только на сервере. [business-login]
4. Обновление: `GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=...`. Токен должен быть старше 24 часов и ещё действовать; обновлённый живёт ещё 60 дней; не обновлённый за 60 дней токен истекает навсегда. [business-login]

**Вывод:** задача обновления токенов обязательна (у нас: за 10 дней до истечения, с алертом при ошибке).

Профиль: `GET https://graph.instagram.com/v26.0/me?fields=user_id,username`. `user_id` — id профессионального аккаунта (`<IG_ID>`), и именно он приходит как `id` в вебхуках; `id` в ответе — app-scoped id, его не используем. [get-started]

## Отправка сообщений

- `POST https://graph.instagram.com/v26.0/<IG_ID>/messages` (или `/me/messages`), заголовок `Authorization: Bearer <token>`, тело `{"recipient": {"id": "<IGSID>"}, "message": {"text": "..."}}`. Ответ: `recipient_id`, `message_id`. [messaging-api]
- **Текст не длиннее 1000 байт UTF-8**, это около 500 кириллических символов. Режем по байтам. [messaging-api]
- **Окно ответа:** обычно 24 часа после действия пользователя; после первого сообщения из рекламы Click-to-Direct — до 7 дней. [messaging-api]
- **Human Agent:** если отвечает человек, ответ можно пометить и получить отдельное окно 7 дней. Кто имеет право и нужно ли одобрение — **не проверено**. Пока бот считает окно 24 часа и для ответов мастера. [messaging-api]
- Сообщения, пришедшие через вебхук, в приложении Instagram не отмечаются прочитанными, пока не отправлен ответ. Папки входящих не поддерживаются. [messaging-api]
- Индикатор набора текста на этой странице не описан. [messaging-api]

## Вебхуки

- Проверка адреса: GET с `hub.mode=subscribe`, `hub.verify_token`, `hub.challenge`; если токен совпал, ответить значением `hub.challenge`. [webhooks-setup]
- Подпись: заголовок `X-Hub-Signature-256` вида `sha256=<подпись>`, подпись SHA256 от тела запроса с App Secret. Документация не пишет явно «HMAC» и «сырое тело»; реализуем HMAC-SHA256 от сырого тела (стандарт Meta) и подтверждаем на первой реальной доставке. [webhooks-setup]
- Подписка аккаунта: `POST https://graph.instagram.com/v26.0/<IG_ID>/subscribed_apps?subscribed_fields=<поля>&access_token=<токен>`, ответ `{"success": true}`. [webhooks-setup]
- Поля вебхуков для Instagram Login: `messages`, `message_echoes`, `message_reactions`, `messaging_seen`, `messaging_postbacks`, `messaging_referral`, `messaging_optins`, `messaging_handover`, `standby`. **`message_echoes` требует `instagram_business_manage_comments`**, поэтому мы просим это право и подписываемся на `messages,message_echoes`. Поля `message_edit` в матрице нет; правки обрабатываем, если придут. [webhooks-fields]
- Повторы: Meta повторяет сразу, потом реже в течение 36 часов, затем отбрасывает; одно событие может прийти несколько раз. Эндпоинт должен быстро отвечать 200 и сам убирать дубли. [webhooks-setup, webhooks]
- Поля событий: `is_echo` (сообщение отправил сам аккаунт), `is_self` (self messaging), `message_edit` (`mid`, `text`, `num_edit`), `is_deleted`, реакции (`action` = `react`/`unreact`, `reaction`, `emoji`), прочтения (`read.mid`), ответ на сторис (`reply_to.story.url`, `reply_to.story.id`), вложения, включая `story_mention`. [webhooks-examples]
- Эхо приходит и когда аккаунт пишет из самого приложения (задокументировано для self messaging). Поле, отличающее отправку через API от ручной, **не описано**. [self-messaging]

**Как определяем сообщение мастера (R8):** сохраняем `message_id` каждого своего ответа; эхо с незнакомым `mid` значит, что написал человек → `HUMAN_ACTIVE`. Подтвердить на живом тесте (`docs/PRODUCT.md`, раздел 8).

## Тестирование

Self messaging: профессиональный аккаунт пишет сам себе, окно 24 часа не действует, эхо приходит с `is_self=true`. Удобно для проверок без второго аккаунта. [self-messaging]

## Открытые вопросы

- Conversation routing (handover между приложениями и инбоксом) и конфликт с ManyChat и автоответами Meta: страница документации не открылась. В онбординге просим студию выключить другие автоматизации.
- Условия окна Human Agent.
- Формат запроса Meta на удаление данных (data deletion callback) для приложений с Instagram Login; пока даём страницу с инструкцией удаления (`/privacy/data-deletion`).
- Требования к privacy policy для перевода приложения в Live и документы для Business Verification.
- Лимиты частоты запросов Send API.

## Источники

- [overview] https://developers.facebook.com/docs/instagram-platform/overview
- [business-login] https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login
- [messaging-api] https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api
- [webhooks] https://developers.facebook.com/docs/instagram-platform/webhooks
- [webhooks-setup] https://developers.facebook.com/documentation/instagram-platform/webhooks/setup
- [webhooks-examples] https://developers.facebook.com/documentation/instagram-platform/webhooks/examples
- [self-messaging] https://developers.facebook.com/documentation/instagram-platform/self-messaging
- [get-started] https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/get-started
- [webhooks-fields] https://developers.facebook.com/documentation/instagram-platform/webhooks/fields
