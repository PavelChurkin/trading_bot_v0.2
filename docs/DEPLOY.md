# Перенос на сервер

Порядок из п. 20 issue: сначала всё проверяется локально (`python start.py`,
вход по ключу, пуши на `localhost`), и только потом — сервер.

Схема:

```
браузер ──HTTPS──► nginx (443, сертификат) ──HTTP──► start.py (127.0.0.1:8000)
```

HTTPS обязателен. Без защищённого контекста браузер не регистрирует
service worker и не выдаёт push-подписку. Исключение — только
`localhost`/`127.0.0.1`.

## 1. Код и окружение

```bash
sudo useradd -r -m -d /opt/orbita orbita
sudo -u orbita git clone https://github.com/PavelChurkin/trading_bot_v0.2 /opt/orbita/app
cd /opt/orbita/app
sudo -u orbita python3 -m venv .venv
sudo -u orbita .venv/bin/pip install -r requirements.txt
```

Ключ админа и VAPID-пару можно перенести с локальной машины — скопировать
`config/admin_key.txt` и `config/vapid_private.pem`. Тогда старый ключ и уже
оформленные push-подписки продолжат работать. Если не переносить, всё
сгенерируется при первом запуске, а устройства придётся подписать заново.

## 2. systemd

`/etc/systemd/system/orbita.service`:

```ini
[Unit]
Description=Orbita: MOEX scanner + dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=orbita
WorkingDirectory=/opt/orbita/app
Environment=TB_HOST=127.0.0.1
Environment=TB_PORT=8000
Environment=TB_VAPID_SUBJECT=mailto:you@example.com
ExecStart=/opt/orbita/app/.venv/bin/python start.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now orbita
journalctl -u orbita -f            # ключ админа печатается при старте
```

### Остановка

Без консоли (под systemd) stdin закрыт, и сервер просто работает дальше.

* Штатная остановка — `sudo systemctl stop orbita` или кнопка «Остановить
  сервер» в админке. Под systemd с `Restart=on-failure` штатный выход
  перезапуска не вызывает.
* Если нужна именно остановка вводом «0» (п. 2), запускайте в `tmux`/`screen`:

  ```bash
  tmux new -s orbita
  .venv/bin/python start.py
  ```

  Отключиться от сессии — `Ctrl+B D`, вернуться и ввести `0` —
  `tmux attach -t orbita`.

## 3. nginx + HTTPS (Let's Encrypt)

`/etc/nginx/sites-available/orbita`:

```nginx
server {
    listen 80;
    server_name orbita.example.com;
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name orbita.example.com;

    ssl_certificate     /etc/letsencrypt/live/orbita.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/orbita.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/orbita /etc/nginx/sites-enabled/
sudo certbot --nginx -d orbita.example.com
sudo nginx -t && sudo systemctl reload nginx
```

После этого откройте `https://orbita.example.com` на телефоне в Chrome,
войдите ключом и в «Настройках» нажмите «Подписать это устройство». Затем
«Установить приложение» добавит PWA на домашний экран. Проверить доставку
можно в админке кнопкой «Тестовый пуш» (таймер по умолчанию 1 с).

## 4. Обслуживание

* **База** — `data/trading_bot.sqlite3`. Бэкап:

  ```bash
  sqlite3 data/trading_bot.sqlite3 ".backup /backup/orbita.sqlite3"
  ```

  Свечи, метрики и журнал старше срока хранения (по умолчанию 7 дней)
  удаляются автоматически.
* **Нагрузка.** Сканер делает ≈1 запрос/с к ISS (`TB_RPS`); цикл по всем
  бумагам TQBR укладывается в 15-минутный интервал. Состояние очереди видно
  во вкладке «Метрики».
* **Postgres.** В issue он предложен для сервера. Сейчас используется SQLite:
  один процесс-писатель, WAL-режим, нагрузка — сотни строк за цикл. Этого
  хватает и на сервере.
