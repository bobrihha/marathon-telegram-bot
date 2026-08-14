# Marathon Bot — AI Handoff

Updated: 2026-07-15

## 🟢 СДАН (2026-07-06, доп. фича 2026-07-15) — прод `132.243.114.203:8081`, домен `bot.bosforovna-klub.ru`

Бот в проде, здоров (`docker compose ps` → Up, `restart: always`), работает без вмешательства. Текущая архитектура:

- **Оплата:** Prodamus webhook → `Payment` (продукт из `customer_extra`).
- **Telegram:** полностью автоматический — юзер вводит телефон/email → бот проверяет оплату, перепривязывает платёж на актуальный марафон, выдаёт ссылку; заявку в ТГ-группу одобряет сам бот (админ чата). Повторный ввод телефона → умный повтор ссылки (не тупик).
- **VK:** двухсообщественная схема, **как раньше**. Отдельный **консьерж «Bosforovna-Club»** (`212441146`, открытое, в `.env` `VK_GROUP_ID`) проверяет оплату и общается с юзером; сами группы марафонов (biohakerclub, clubjelchniy и т.п.) **без бота в сообщениях** — там клиенты пишут человеку-админу. Кнопка в ТГ-боте «Открыть ВК-бота» ведёт именно в консьерж.
- **VK-одобрение заявок — РУЧНОЕ** (осознанное решение владельца от 2026-07-06): user-токен полностью отключён (`VK_USER_PROXY` убран из `.env`, fail-safe в `_vk_user_api` пропускает вызовы) — аккаунт-держатель токена (Мелиса, 1113006772) под нагрузкой всё равно блокировался VK даже через РФ-прокси. Взамен — **кнопка «🔔 Заявки ВК»** в админ-меню: живой самоочищающийся список оплативших, кто ждёт одобрения (заполняется через community-callback `group_join`, без user-токена). Клиентка одобряет вручную в самом ВК.
- **Контроль доступа:** TG-приём заявок закрыт от «last-resort: любая группа» (возвратные клиенты со старым/чужим продуктом больше не проскакивают в новый марафон).
- **Фиксация выхода из группы (2026-07-15):** для доказательной базы при спорах о возврате. TG — `router.chat_member()` в `join_requests.py`, ловит официальное событие Telegram о смене статуса (LEFT/KICKED), пишет `AccessLog` с меткой времени ОТ TELEGRAM (`event.date`) и кто инициировал (сам/админ). VK — `_record_vk_leave()` в `vk_bot.py` на событии `group_leave`, метка = время получения callback (VK не даёт точное время выхода), различает «вышел сам» vs «удалён». «Найти оплату» в админке теперь показывает полную хронологию (30 записей, по возрастанию времени) вместо последних 5. ⚠️ Работает только ВПЕРЁД с даты деплоя — задним числом ни TG, ни VK не хранят историю выходов, у старых случаев доказать дату нечем.

**Известные, не блокирующие хвосты** (см. `TODO.md` для деталей): 33 старые ВК-заявки в biohakerclub не подтверждали оплату боту (провисят, пока не напишут консьержу); 4 старые заявки с оплатой `pechen` (не тот продукт, решение за клиенткой); бесхозный callback-сервер «Сервер 1» в сообществе биохакеров (безвреден); старые TG-чаты прошлых марафонов не редиректят на актуальный.

**Если продолжаешь работу:** читай этот баннер + `TODO.md`. Полная история решений (VK-геоблок, дыра доступа, консьерж-разделение, фиксация выхода) — в секциях ниже, по датам.

## Recent Changes (2026-08-14, вторая волна — надиктовка клиентки подтвердилась, механизм собран полностью)

Клиентка надиктовала точный паттерн: «возвратные вводят номер — бот повторяет ссылку ПРОШЛОГО марафона; помогает нажать "Проверить оплату" и ввести снова / удалить бота и перезайти». Проверено — она права, механизм из трёх частей:
1. **В письме после оплаты стоит инвайт-ссылка ГРУППЫ** (`t.me/+3SjSJX4u-W4wYzMy` = invite_link группы ЖКТ; клиентка называет её «ссылка на бота» и не меняет между марафонами, т.к. ведёт ОДИН чат ЖКТ с переименованием). Люди из письма подают заявку МИНУЯ проверку бота → у возвратных привязка на старом продукте → заявка висит (путь Веры: оплата 06:06 → заявка 06:29 без ввода номера).
2. **Гонка вебхука:** человек вводит номер сразу после оплаты, свежая оплата ещё не доехала до базы → бот находит его СТАРУЮ paid → resend ссылки ПРОШЛОГО марафона. «Магия кнопки» = ко второму вводу вебхук уже в базе; сама кнопка код не меняет.
3. **Висящая заявка не одобрялась задним числом:** бот одобрял только в момент события заявки; после успешного ввода номера уже поданная заявка продолжала висеть — потому и «помогало удалить бота и перезайти» (заявка переподавалась).

**Фиксы (задеплоены):** (а) recovery «ничьих» used-оплат (см. выше); (б) **`_approve_pending_tg_request`** в `main.py` — после успешной проверки оплаты (grant-путь И resend) бот вызывает `approve_chat_join_request` для целевой группы: если заявка уже висела — одобряется сразу, пишется AccessLog `granted` с комментарием «Approved pending join request after payment check»; исключение (заявки нет) глотается — обычный случай; (в) в resend-текст добавлена строка «⏳ Только что оплатили НОВЫЙ марафон... подождите пару минут и отправьте номер ещё раз» — закрывает гонку вебхука. Теперь порядок действий покупателя не важен: заявка → номер или номер → заявка — оба пути ведут к одобрению.
**Рекомендация клиентке (передана):** в письме после оплаты лучше ссылка на самого бота (@vhonoyBot) с просьбой ввести номер, а не инвайт группы — но и с инвайтом теперь работает.

## Recent Changes (2026-08-14)

**«Ничьи» used-оплаты рвали цепь доступа (марафон jkt/«ЖКТ 5R») — recovery-фикс задеплоен.** Симптом: возвратные покупательницы (Vera Arbuzova tg 166015353, юзер vk 494339902) оплатили jkt, но их заявки в ТГ-группу висели неодобренными, при этом бот на ввод телефона отвечал «У тебя уже есть доступ ✅» и слал ссылки. Корень: их свежие jkt-оплаты оказались `used=1`, но **ни один user к ним не привязан** («ничья» оплата; источник появления НЕ установлен — за день 2 случая, см. открытый вопрос в TODO), а привязка юзеров осталась на старых продуктах («Книга», pechen) → авто-одобрение заявки видело старый продукт → NOT approving; resend-ветка ссылки слала, но привязку НЕ чинила → замкнутый круг. **Фикс:** в обеих resend-ветках (main.py ТГ + vk_bot.py ВК) — если `existing_user is None` (оплату никто не «держит»), привязать её к тому, кто назвал её телефон/email, затем resend; заявка после этого одобряется. Обе живые пострадавшие привязаны руками (бэкап `backups/db.sqlite3.before-vera-rebind.*`). Прочие выводы дня: массово всё работает (10+ авто-одобрений за утро); чат «КЛУБ ЖКТ 5R АВГУСТ» = переименованный чат ЖКТ августа (тег jkt тот же, это ок); ⚠️ **ТГ-бот НЕ логирует входящие сообщения** — вывод «юзер не вводил телефон» из тишины в логах ДЕЛАТЬ НЕЛЬЗЯ (ошибся так в этой сессии); ссылка в письме Тильды ведёт на бота и не меняется между марафонами (со слов клиентки) — «выкидывает на желчный» у юзеров скорее от старых кнопок «Открыть ВК-бота» в истории переписки (до 2026-07-02 они вели в сообщество конкретного марафона).

## Recent Changes (2026-07-15)

**Фиксация даты выхода из группы — доказательная база для споров о возврате.** Клиентка столкнулась со спором о возврате, где важна была дата выхода участника, а бот её нигде не хранил. Разобрано: подписка на `chat_member` в TG уже стояла в `allowed_updates` (main.py), но обработчика не было — событие приходило и терялось. Добавлено:
- `app/handlers/join_requests.py`: `router.chat_member()` → детектит переход "был в чате" → LEFT/KICKED, пишет `AccessLog(action="left_tg"|"kicked_tg", timestamp=event.date)` — время ОТ САМОГО TELEGRAM, не «когда заметили». Комментарий фиксирует инициатора (сам вышел / кто из админов удалил).
- `app/vk_bot.py`: новая `_record_vk_leave()`, вызывается на `group_leave` callback рядом с существующей `_clear_vk_join_request()`. `action="left_vk"|"kicked_vk"` по полю `self` из VK-объекта. Таймстамп = время получения вебхука (VK не передаёт точное время выхода в событии).
- `app/handlers/admin.py::send_payment_info` («Найти оплату»): лимит логов 5→30, сортировка по времени по возрастанию (хронологическая история вместо «последние 5»), фильтр расширен на `telegram_id` пользователя.
- Проверено вживую на проде: aiogram 3.29.0, `router.chat_member()` и `ChatMemberStatus` подтверждены внутри контейнера перед деплоем. Задеплоено, `docker compose ps` → Up, polling чистый.
- **Ограничение (сказано владельцу прямо):** работает только для будущих выходов. Для уже случившихся (включая текущий спор) ни Telegram, ни VK не хранят историю выходов задним числом — доказать дату нечем, только контекстными признаками (последняя активность, дата оплаты и т.п.).

## Recent Changes (2026-07-06)

**Чеккер ВК-заявок для клиентки (ручной приём) — новая фича, задеплоено.** Раз ВК-одобрение теперь ручное, клиентке нужен инструмент видеть, КОГО принимать. Реализовано БЕЗ user-токена (только community-callback): бот ловит `group_join(request)` → пишет заявку в новую таблицу `vk_join_requests` (vk_id, vk_group_id, full_name через users.get community-токеном, status pending); при `group_join(approved/join)` и `group_leave` → `_clear_vk_join_request` ставит status=done (список **самоочищается**, клиентке отмечать не надо). Кнопка **«🔔 Заявки ВК»** в админ-меню (`admin.py`, обработчик `admin_vk_requests` → `get_pending_paid_vk_requests`) показывает pending-заявки, у кого оплата подтверждена И продукт совпадает с группой (`_vk_group_matches_payment`) — т.е. кого принимать. Вариант А (только оплатившие; неподтверждённых не показываем). Клиентка принимает в ВК вручную → уходят из списка. Файлы: `models.py` (класс `VkJoinRequest`, таблица через create_all), `vk_bot.py` (`_record_vk_join_request`/`_clear_vk_join_request`/`get_pending_paid_vk_requests` + group_leave роутинг), `admin.py` (кнопка). **Доступ:** кнопку видят только `ADMIN_IDS` (сейчас `5261935873,624193469` в .env) — клиенткин TG id должен быть там. **Ограничение:** чеккер видит только НОВЫЕ заявки (после деплоя, через callback); существующие pending в ВК бот не знает (getRequests требует user-токен, отключён). Бэкапы `.bak.*`.



**ВК user-токен ОТКЛЮЧЁН — авто-приём ВК-заявок выключен, клиентка одобряет ВРУЧНУЮ (решение владельца).** Даже через РФ-прокси (Kaluga) аккаунт-держатель user-токена (Мелиса) блокировался под нагрузкой (много вызовов). Наташа решила: отключить user-токен совсем, ВК-заявки клиентка принимает руками. **Сделано:** из прод `/root/bot/.env` убрана строка `VK_USER_PROXY` (добавлена пометка-коммент) → встроенный fail-safe `_vk_user_api` теперь ПРОПУСКАЕТ все user-токен вызовы (`approveRequest`/`getRequests`/`getMembers`/`removeUser`) → аккаунт Мелисы больше не дёргается. Контейнер пересоздан (env перечитан). Токен Мелисы в `vk_admin_auth` id6 оставлен (не используется). Также поправлены тексты бота: ВК-обещания «автоматически приму заявку» → «администратор одобрит вашу заявку» (`main.py` 2 места + `vk_bot.py` handle_payment_check + DM при заявке); ТГ-часть «примется автоматически» ОСТАВЛЕНА (TG-бот сам админ ТГ-группы, авто-приём TG работает). **Последствие (важно):** бот больше НЕ фильтрует ВК-вход по оплате — консьерж проверяет оплату и подтверждает юзеру + даёт ссылку, но само одобрение заявки в ВК — вручную клиенткой (она сама решает, кого пускать). **Обратимо:** вернуть авто-ВК = снова добавить `VK_USER_PROXY=...` в .env + recreate (но аккаунт опять под риском блокировки). Бэкапы `.env.bak.*`, `app/main.py.bak.*`, `app/vk_bot.py.bak.*`.

## Recent Changes (2026-07-02)

**Новый марафон «Клуб желчный» (продукт `jelch`) — настройка проверена, поведение объяснено.** Клиентка подключала новую пару групп. Разобрано три вопроса:
1. **Установка ВК-группы «Клуб желчный»:** на шаге «ключ доступа сообщества» клиентка вставила ПОЛЬЗОВАТЕЛЬСКИЙ токен Мелисы (oauth-ссылку), а нужен COMMUNITY-токен группы → бот «не смог определить ID сообщества». Позже исправлено: community-токен валиден, `vk_group` id2 = vk_group_id **239982244** «КЛУБ ЖЕЛЧНЫЙ» (clubjelchniy, is_closed=1), callback «MarathonBot» status=ok, TG-группа `current_group` id2 chat `-1003822612617`. Мелиса (1113006772) — руководитель и этой группы. Всё готово. В сообществе висит orphan «Сервер 1» (unconfigured) — безвреден.
2. **«Странность» ВК-кнопки «Открыть ВК-бота»:** это ПРАВИЛЬНОЕ поведение (та же схема, что biohakerclub). Кнопка ведёт `https://vk.com/im?sel=-<group_id>` = в сообщения сообщества (бота), НЕ на страницу группы. Для закрытого сообщества нужен шаг «напиши email в чат», чтобы бот (а) сопоставил vk_id↔оплату, (б) мог ответить (иначе VK error 901). После проверки email бот шлёт инвайт-ссылку «подайте заявку — примется автоматически» → тупика нет (подтверждено кодом `vk_bot.py:687-710` + adversarial workflow). Когда токен был кривой, кнопка была прямым «Вступить в ВК-группу» — отсюда путаница у клиентки.
5. **ВК-консьерж вынесен из сообщества марафона (переделка «по старому», задеплоено).** Требование Наташи: в самом сообществе марафона бота в сообщениях быть НЕ должно (там клиенты пишут живому админу); проверка оплаты и выдача ссылок — только в ОТДЕЛЬНОМ консьерж-сообществе. Консьерж уже существовал: **«Консьерж Bosforovna-Club» (bosforovnamarafon, group_id 212441146, открытое, в `.env` `VK_GROUP_ID=212441146`, `VK_COMMUNITY_TOKEN`=его токен)** — бот его сообщения обрабатывает (source_group=None → `handle_payment_check` → `select_vk_group_for_payment` по продукту → инвайт+approve в нужную группу марафона; отвечает токеном консьержа). **Было не так:** (а) ТГ-кнопка «Открыть ВК-бота» строила `im?sel=-{vk_group.vk_group_id}` = вела в САМУ группу марафона; (б) на группах марафона callback имел `message_new=1` (бот отвечал в них). **Сделано:** (1) `main.py` обе ВК-кнопки (`_resend_access_links` + grant) → `im?sel=-{VK_GROUP_ID}` (консьерж); (2) `vk_bot.py configure_vk_callback_server` → `message_new=0` (будущие группы марафона без бота в сообщениях); (3) на проде через API переключены callback групп марафона biohak(239398316)+jelch(239982244): `message_new 1→0`, `group_join=1`/`group_leave=1` сохранены (консьерж 212441146 НЕ тронут, message_new=1). Итог-флоу: оплатил → ТГ даёт ссылку на консьерж → пишешь консьержу email → консьерж проверяет оплату → инвайт в группу марафона + авто-одобрение заявки; в самой группе бот молчит. Бэкапы `app/main.py.bak.*`, `app/vk_bot.py.bak.*`. ВК-сообщества: консьерж 212441146; группы марафона biohakerclub 239398316, clubjelchniy 239982244, печень 238291860.

4. **Дыра доступа для возвратных (закрыта, задеплоено).** `join_requests.py` (TG-авто-одобрение) имел «last-resort: любая последняя группа» без проверки продукта. Механизм: возвратный клиент со СТАРЫМ привязанным `user.payment` (продукт пусто/`pechen`/старый) при заявке в НОВЕЙШИЙ TG-чат → тег не совпал → NULL нет → last-resort брал новейшую группу (jelch) → chat совпадал → одобрял БЕЗ оплаты нового марафона. VK был защищён (`select_vk_group_for_payment` имеет guard `and not product`), TG — нет. **Эмпирика (детектор по access_logs vs product):** jelch — 0 протечек, biohakerclub — ВСЕГО 2 за всё время (orders 42607499, 41377919, оба пустой продукт, 12+14 июн). Почему почти не протекало: в штатном флоу `main.py handle_email_or_order` **перепривязывает `user.payment` на новый платёж ПЕРЕД выдачей ссылки**, поэтому к заявке продукт уже правильный; last-resort срабатывал только при заявке МИНУЯ бота (прямая ссылка). **Фикс:** убран last-resort — если продукт оплаты не совпал ни с одной группой (точный тег → NULL-fallback → стоп), заявка НЕ одобряется (оставляем pending), как в VK. Легитимных не ломает. Бэкап `app/handlers/join_requests.py.bak.*`. **Рекомендация Наташе:** раздавать доступ ТОЛЬКО через бота (ввод телефона), не прямыми ссылками на группу.

3. **«Два человека по одной оплате».** Разобрано на её тесте (order 46345773, тел 79131111111): доступ получили 1 TG (461713084) + 1 ВК (76767383) через **мост TG↔VK** (фича «две площадки — вступай в обе»). Within-platform дубль БЛОКИРУЕТСЯ (проверено по всей базе: нет оплат с 2 TG или 2 ВК; max 1 TG + 1 ВК). Слабое место: TG и ВК нельзя кросс-верифицировать, общий ключ — телефон/email. **Решение Наташи: ОСТАВИТЬ мост как есть** (соответствует обещанию «две площадки»). Код НЕ менялся. Прим.: тест-телефон 79141189700 имеет 9 оплат — на нём тесты выглядят как reuse, хотя это разные оплаты.

## Recent Changes (2026-06-24)

**VK user-token geo-block — личный аккаунт админа (Наталья Каракоч) блокируется VK + авто-приём ВК отвалился.** VK прислал Наталье Каракоч «страница взломана, вход из Amsterdam, IP 132.243.114.203» и ограничил аккаунт → авто-одобрение заявок в ВК сломалось. Корень: сервер бота — в Амстердаме (NL, CLODO/Clodo Cloud), а `groups.approveRequest`/`groups.removeUser`/`groups.getMembers(managers)` требуют её ЛИЧНЫЙ user-токен (`VkAdminAuth`). VK видит личный токен с зарубежного IP → считает взломом → блок. **Проверено вживую: токен СООБЩЕСТВА approveRequest НЕ умеет** (`error 27: method is unavailable with group auth`) — обойтись им нельзя, нужен именно user-токен. Старый сервер `176.12.74.39` ЖИВ, но он в **Казахстане** (Алматы, HostLab) — не РФ, как релей ненадёжен. Решение (согласовано с Натальей): пускать user-токен вызовы через **российский residential/mobile прокси**; Наталья покупает прокси.

Сделано в коде (`app/config.py` + `app/vk_bot.py`, задеплоено rebuild):
- `vk_api(..., proxy=..., timeout=...)` — поддержка прокси и таймаута.
- `_vk_user_api(method, **params)` — единая обёртка для вызовов с личным токеном: берёт список прокси из `VK_USER_PROXY` (одна строка ИЛИ несколько URL через запятую), **шафлит и делает фейловер** по узлам (резидентские узлы периодически отдают `503 Node has rejected`/падают). **Если прокси не задан и `VK_USER_DIRECT_OK` ≠ 1 → вызов ПРОПУСКАЕТСЯ (пауза)**, чтобы не бить по аккаунту. `_approve_vk_request`/`_remove_vk_user`/`_is_group_manager` переведены на неё.
- Бэкапы на сервере: `app/vk_bot.py.bak.*`, `app/config.py.bak.*`, `.env.bak.*` (2026-06-24).

**Прокси настроен и активен (proxys.io, account user401774):** в прод `/root/bot/.env` дописана строка `VK_USER_PROXY=` с **5 узлами** `http://user401774o30780r521725:mpsj98@pool.proxys.io:{10001,10003,10005,10010,10050}`. Выход — **Калуга, Ростелеком, RU residential** (проверено: `groups.getRequests`/`utils.getServerTime` через прокси отвечают). Лист на proxys.io: «Наташин юзер бот», Russia/Kaluga Oblast, ротация (нельзя отключить) — выставлять макс (60 мин), порт 10000 был мёртв (потому фейловер). Контейнер пересоздан (`up -d --build`), `VK_USER_PROXY` виден в контейнере (5 URL).

**Токен админа:** старый аккаунт **Наталья Каракоч (710298860) ЗАБЛОКИРОВАН VK** («User authorization failed: user is blocked», ban_info). Через прокси авторизовали **другой аккаунт — «Мелиса Василиса» (melisa_vasilisa, vk_user_id 1113006772)**, токен бессрочный (`expires_in=0`), записан в `vk_admin_auth` **id 5** (его и берёт `_get_vk_admin_token` = ORDER BY id DESC). Старые строки 1-4 (710298860) остались, но игнорируются. Бэкап БД: `backups/db.sqlite3.before-vk-token-melisa.*`.

**ЗАКРЫТО 2026-06-25:** Наташа назначила Мелису (1113006772) руководителем biohakerclub. Свежий бессрочный токен записан в `vk_admin_auth` **id 6** (id 5 — старый дубль). Право одобрять подтверждено end-to-end через прокси: `groups.getRequests` → список (37 заявок было), `approveRequest` на не-ожидающего → `error 100 request not found` (не «access denied»). Цепочка токен→прокси(Калуга)→VK работает; новые оплатившие biohack авто-одобряются. Прим.: прокси привязан к Калуге; если логин из другого города — VK может изредка показывать «новый вход» (не жёсткий бан как с NL).

**Инцидент 2026-06-25:** сервер `132.243.114.203` лежал ~пару часов из-за провайдерского сбоя ДЦ (CLODO/Amsterdam) — не наша ошибка. Бот сам поднялся после восстановления (`restart: always`). Мысль на будущее: уже второй сервер с проблемами (176.12.74.39 умирал в мае) — при повторе обсудить миграцию.

**Бэклог 37 зависших ВК-заявок (2026-06-25):** сверка с базой оплат → авто-одобрять нельзя: 33 без user-строки (не подтверждали оплату боту — нажали «Вступить», должны прислать email в сообщество; VK 901 не даёт боту писать первым), 4 с оплатой `pechen` (закрытый марафон, не тот продукт — решение вручную). Детали в `TODO.md`.

Также висит VK-уведомление: отключён callback «Сервер 1» в сообществе «БОСФОРОВНА КЛУБ» (7 дней >90% ошибок) — вероятно старый orphan, рабочий марафон-callback это «MarathonBot» (server_id 2) в biohakerclub; проверить/удалить orphan. См. memory `[[feedback_tilda_vk_user_token_geo_block]]`, `[[reference_vk_token_app_kate_mobile]]`, `[[reference_proxy_account_split]]`.

## Recent Changes (2026-06-14)

**TG smart resend — fix «попадаю на старый марафон» / «ссылка выдаётся один раз».** Клиентка (Наташа) жаловалась: вернувшиеся участники (делали прошлый марафон) покупают Биохакинг, вводят боту телефон — и «попадают на старый марафон». Полная диагностика на проде показала: **бот НЕ выдаёт старую ссылку** (в `current_group`/`vk_group` теперь одна группа — Биохакинг, tag `biohack-pay`, TG chat `-1003956698279` «КЛУБ БИОХАКЕРОВ», VK biohakerclub; печёночной группы в боте нет вообще, печень была отдельным чатом `-1003831239539`, закрыта). Реальный механизм: при повторном вводе телефона уже отоварившимся юзером TG-бот отвечал тупиком «Эта оплата уже была использована ✅ / Ссылка выдаётся один раз» **без ссылки**, и человек от безысходности листал переписку вверх и тыкал в **старую кнопку «Вступить 🔐»** из прошлого марафона → попадал в старый чат. Подтверждено трассировкой Natali Karman (tg_id 1080563301): бот корректно выдал ей Биохакинг 12.06 (access_log), но она до сих пор числится в чатах фев/март. В TG не было «умного повтора», который уже есть в ВК (`vk_bot.py:_resend_granted_links`).

Фикс: добавлен `_resend_access_links(message, db, payment)` в `app/main.py` (порт ВК-логики) + заменена тупиковая ветка «уже использована» (внутри `handle_email_or_order`, ветка `if not payment → used_payment → else`) на вызов повтора. Теперь повторный ввод телефона = бот шлёт **свежую рабочую ссылку на актуальную группу** (по product_tag платежа, с фолбэком на NULL-tag → последнюю группу). Логика выбора группы скопирована 1:1 с грант-пути. Задеплоено (rebuild, образ пересобран, контейнер recreated, polling OK). Бэкап БД: `backups/db.sqlite3.before-product-tag-fix.20260614_091612`. Бэкап старого main.py: `app/main.py.bak.20260614_105403`.

Открытый хвост (операционный, по желанию клиента, бот не трогаем): старые чаты (фев `-1003877724267`, март `-1003898113957`, печень `-1003831239539`) живы, люди в них числятся, старые кнопки «Вступить» в переписках работают. Чтобы добить путаницу — закрепить в старых чатах редирект «Марафон завершён, актуальный — Биохакинг: <ссылка>» и/или revoke старых инвайт-ссылок (revoke НЕ удаляет участников). Клиент пока не подтвердил.

Прод-доступ: `root@132.243.114.203` (пароль в панели FirstVDS; в этой сессии заходили по нему). DB: bind-mount `./data/db.sqlite3` (внутри контейнера `/app/data/db.sqlite3`), на хосте `sqlite3` есть.

## Recent Changes (2026-06-07)

**Bugfix — VK Callback API "invalid secret_key" (intermittent setup failures).** `configure_vk_callback_server` generated the callback secret with `secrets.token_urlsafe(24)`, which can contain `-`/`_`. VK's `groups.addCallbackServer` accepts only Latin letters + digits in `secret_key`, so setup failed ~64% of the time with "invalid secret_key" and succeeded only when the random key happened to be alphanumeric (≈36%). This looked like "fixed once, broke again". Changed to `secrets.token_hex(16)` (always alphanumeric, 32 chars). `app/vk_bot.py:364`. Deployed (rebuild). DB backup: `backups/db.sqlite3.before-vk-secret-fix.*`. Callback title is `"MarathonBot"` (11 ≤ 14 chars) — the older "title should be not more 14 letters" error won't recur.

**VK auto-approval diagnosis + fix (2026-06-07).** TG auto-approval works (verified in prod logs: real buyer `vasilisa.krivosheeva` paid `biohack-pay`, join request to chat `-1003956698279` auto-approved). VK approvals were NOT working: (1) the VK admin USER token in `vk_admin_auth` (vk_user_id 710298860, from 2 May, no `offline` scope) had **expired** — `users.get`/`groups.approveRequest` return `error 5 invalid access_token`; `_approve_vk_request`/`_remove_vk_user` need a USER token, not the community token. (2) The «🔑 Авторизация ВК» button (`app/handlers/admin.py`) linked to the **blocked** VK app `client_id=6121396` ("application is blocked"), so re-auth was impossible. Fixed the button to Kate Mobile `client_id=2685278`, `scope=groups,offline` (offline ⇒ non-expiring), `revoke=1`. Deployed. **RESOLVED 2026-06-07:** Наталья Каракоч (vk_user_id 710298860, admin of biohakerclub — confirmed via `groups.get filter=admin`) re-authorized; fresh non-expiring token saved to `vk_admin_auth` (id 3). Verified through the bot's own `_get_vk_admin_token()` + `groups.getRequests` — VK approvals now functional. Also note: a user who just clicks "join" on the closed VK community WITHOUT first messaging the community their email stays pending — and the bot can't even DM them a prompt (VK error 901 "Can't send messages for users without permission"). Intended flow: user messages the community with email → bot verifies → approves. Test payment row `TESTJUNE-001` (`test-biohack@example.com`, product `biohack-pay`) left in DB for testing — delete when done.

**VK group for «Марафон июнь» configured directly (2026-06-07).** Client couldn't complete the bot wizard, so set up via VK API + DB insert from prod: community «Клуб БИОХАКЕРОВ» (`biohakerclub`, group_id 239398316), tag `biohack-pay`, link `https://vk.com/biohakerclub`. Callback server `MarathonBot` (server_id 2) status **ok**. Verified `VK_CALLBACK_URL=https://bot.bosforovna-klub.ru/webhooks/vk` resolves to 132.243.114.203 and answers confirmation pings. Method: insert `vk_group` row (with `callback_confirmation` so the running bot answers VK's ping from DB) BEFORE `groups.addCallbackServer`, then `setCallbackSettings(message_new, group_join, group_leave)`. An orphan unconfigured «Сервер 1» remains on the VK side (harmless, client created it manually). Still TODO for client: delete stray TG slot `tg2` («Марафон июнь» pointing to a vk.com link); keep `tg1` (real t.me link).

Note: client also hit confusion between «Установить группу» (TG flow, never asks for token) and «Установить ВК-группу» (asks for token + auto-configures Callback). Pasting a vk.com link into the TG flow silently creates a stray TG CurrentGroup — clean it via «🗂 Группы». VK product tag must equal the Prodamus product name (case-insensitive).

## Recent Changes (2026-06-02)

Deployed to prod (`132.243.114.203:/root/bot/`, rebuild). DB backup: `backups/db.sqlite3.before-groups-mgmt.20260602_135136`.

1. **Bugfix — admin payment search was case-sensitive on email.** Buyer entered `Samaria.nadegda@...`, DB stored lowercase → user-facing flow (`func.lower`) granted access correctly, but admin «Найти оплату» said «Оплата не найдена». Fixed all 4 admin lookups in `app/handlers/admin.py` to `func.lower(Payment.email) == query.lower()` (lines ~114/255/831/934). Access itself was always correct — only the admin check lied.
2. **New admin feature «🗂 Группы»** (`app/handlers/admin.py`): lists all TG + VK groups keyed `tg<id>`/`vk<id>` with their `product_tag`, and deletes by key (state `delete_group`). Needed because `create_current_group`/VK-group only ever INSERT — groups accumulated forever with no UI to remove finished marathons.
3. **Stricter group matching** (`select_*_for_payment` in `app/vk_bot.py` + inline TG/VK lookup in `app/main.py`): the last-resort "any latest group" fallback now applies ONLY when the payment has no `product_name`. A payment that names a product but matches neither a tagged group nor a general (NULL-tag) group now gets "группа не настроена" instead of being dumped into an unrelated marathon's group. Behaviorally neutral on current prod (NULL groups still exist) — protects the state after old groups are deleted.

**Prod group/tag state (2026-06-02):** TG groups 1-9 untagged (old marathons), 10-12 tag `pechen`. VK groups 1-3,5 untagged, 4,6-9 tag `pechen`. Payments: 2592 NULL product, 1196 `pechen`, 106 `Hormones`, 8 «Книга». ⚠️ `Hormones` buyers have NO matching tagged group → currently fall into latest NULL group (old March marathon). If Hormones is sold, create groups with tag `hormones`.

**Product advice given to client:** delete finished/recorded-only marathon groups via «🗂 Группы»; always set `product_tag` per live group; recorded marathons sold via Tilda should have NO active bot group so their payments don't mix. Bot returns only ONE payment per lookup (newest unused), so it never grants two products at once — but a second lookup can grant a second unused payment sequentially.

Updated: 2026-05-15

## Current State

- **Project:** Tilda Marathon Bot (марафон Тильда)
- **Path:** `/Users/nataliabobrovskaya/dev/06_TG_Bots/tilda_marafon_bot/`
- **Production:** `132.243.114.203`, path `/root/bot/`, domain `bot.bosforovna-klub.ru`
- **Old server:** `176.12.74.39` — bot stopped and removed (datacenter outage 2026-05-13)
- **Stack:** Python, aiogram 3, aiohttp webhooks, SQLAlchemy, SQLite, Docker Compose
- **Status:** Production, working

## Architecture

- **Prodamus webhooks** (`/webhooks/prodamus/{token}`) — saves payment with product name
- **VK Callback API** (`/webhooks/vk`) — per-community tokens, confirmation, message routing
- **Telegram polling** (aiogram 3) — payment check + FSM menu + admin panel
- **Multi-product:** groups linked by `product_tag` → buyer of "Печень" doesn't enter "Гормоны"
- **Cross-platform:** one payment = access to both TG and VK (User stores both IDs)
- **VK multi-community:** admin can add new VK community token; bot auto-creates Callback server
- **VK guard:** paid `vk_id` gets `groups.approveRequest`; unpaid requests get `groups.removeUser` + admin notification

## Key Modules

| File | Purpose |
|---|---|
| `app/webhooks.py` | Prodamus/Tilda webhook handler + aiohttp server |
| `app/vk_bot.py` | VK Callback API + payment check + message FSM |
| `app/handlers/admin.py` | TG admin panel (910+ lines) |
| `app/handlers/join_requests.py` | Auto-approve by payment status |
| `app/db/models.py` | Payment, User, CurrentGroup, VkGroup (with product_tag) |
| `app/db/dal.py` | DB init + auto-migrations (ALTER TABLE) |

## Recent Changes (2026-05-15) — UX: повторный тык по своему платежу

- **Bug:** после выдачи ссылки юзеры рефлекторно тыкали «Проверить оплату», вводили свой email/телефон ещё раз и получали «Эта оплата уже была использована ✅» — звучало как отказ, продолжали тыкать часами.
- **Fix А:** в `handle_payment_check` если `used_payment` принадлежит **тому же** `vk_id` — вызов `_resend_granted_links()`, который шлёт «У тебя уже есть доступ ✅, высылаю ссылки ещё раз: VK + TG». Без новых привязок, без новых одобрений, без создания новой TG one-use ссылки (используем `tg_group.invite_link`).
- **Fix Б:** новый `granted_keyboard()` с одной кнопкой «Поддержка» (без «Проверить оплату»). Применён в success-выдаче и в resend. Welcome / «введи email» / «не нашла оплату» по-прежнему используют `main_keyboard()` с обеими кнопками.
- **Защита не ослаблена:** ветка «`existing_user.vk_id != vk_id_str`» (другой VK-аккаунт) → по-прежнему «оплата уже использована другим пользователем».

## Recent Changes (2026-05-15) — VK spam fix

- **Bug:** ВК-бот спамил юзерам «Я не нашла оплаченный заказ» пачками каждую секунду.
- **Root causes (vk_bot.py):**
  1. `_PENDING_SUPPORT: set[int] = {}` — на самом деле dict; `.add()` падал AttributeError на нажатие «Поддержка», VK не получал `ok` → ретраил коллбэк → дубли.
  2. `handle_vk_callback` отвечал `ok` только после `await vk_send_message`. При лагах VK API >10 c → ретрай → каждый ретрай слал ещё один «не нашла оплату».
  3. Любой свободный текст (приветствия, «спасибо», вопросы) шёл в `handle_payment_check` и получал «не нашла оплату» — даже от уже верифицированных юзеров.
- **Fixes (vk_bot.py):**
  1. `_PENDING_SUPPORT = set()`.
  2. `_spawn_vk_task()` + `_vk_event_already_seen()` (TTL 300 с): callback отвечает `ok` сразу, обработка в фоне; дедуп старых ретраев на всякий случай.
  3. `_looks_like_payment_data()` + `_is_verified_vk_user()`: payment_check вызывается только если текст похож на email/телефон/order_id; верифицированным юзерам шлётся «доступ уже есть»; остальным — подсказка про email/телефон.
- **Deploy:** scp `app/vk_bot.py` → `docker compose up -d --build` (Dockerfile запекает app/ в образ, рестарта без ребилда мало).
- **DB backup before deploy:** `/root/bot/backups/db.sqlite3.before-vk-spam-fix.20260515_124641`
- **Verified in prod logs:** POST /webhooks/vk → 200 за 14 мс, «Поддержка» обрабатывается без краша, ретраев одного event_id нет.

## Recent Changes (2026-05-14)

- **Emergency server migration:** old server `176.12.74.39` (FirstVDS/Ahost datacenter) went down due to hardware failure
- New server: `132.243.114.203` (FirstVDS, different datacenter)
- Full DB migrated: 3571 payments, 2057 users
- Docker port changed to 8081 (Apache/ISPmanager occupies 8080 on new server)
- SSL via certbot, auto-renewal configured
- DNS `bot.bosforovna-klub.ru` updated in reg.ru → `132.243.114.203`
- Old bot container removed (`docker compose down`) to prevent Telegram polling conflict
- Domain `bot.bosforovna-marafon.ru` no longer in use (client confirmed)
- Old server nginx proxy redirect to new server (for Prodamus/VK DNS lag)
- **VK fix:** `.env` was missing VK variables — added from old server, recreated container
- **VK fix:** enabled "Bot capabilities" in VK community 238291860 (error 912)
- **Security fix:** used payments no longer re-issue group links (vk_bot.py L398-411). TG→VK bridge still works.
- Daily DB backup to Telegram at 04:00 (cron + `/root/bot/backup_db.sh`)

## DB State

- `payments=3571`, `users=2057`, `current_group=11`, `vk_group=7`
- Local `db.sqlite3` is outdated relative to code (needs `init_db()` migration before local tests)
- Production DB backup: `/tmp/old_marafon_db.sqlite3` (local, from migration)

## Deploy Notes

- Deploy by `tar/scp` excluding `.env`, `data/`, local DB, `.git`, `.venv`
- New server password: in FirstVDS panel (root@132.243.114.203)
- Old server backup: `176.12.74.39:/root/backups/` (server is alive but bot stopped)
- Git status: untracked `portfolio_marafon_bot.md` + локально изменены `app/main.py`, `app/vk_bot.py` (WIP, **уже задеплоен на прод через rsync**, в git не закоммичен); прод не имеет git-репы — сравнивать через scp+md5/diff
- На проде нет `.git`; для проверки текущего кода — `scp` файлов и сравнение с локалью

## Checks Passed

- `python3 -m py_compile` on all key modules
- `git diff --check` passed
- SQLite smoke tests for migrations, VK confirmation, per-group token, join request approve/remove, TG-first/VK attach
