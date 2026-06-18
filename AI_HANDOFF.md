# Marathon Bot — AI Handoff

Updated: 2026-06-14

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
