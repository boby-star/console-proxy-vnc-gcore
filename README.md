# Розгортання та налаштування Console Proxy

Цей документ описує production-розгортання Console Proxy на одному Linux-сервері
за допомогою Docker Compose. Стек складається з трьох контейнерів:

```text
Інтернет => Bunny CDN (опціаонально) => Nginx => console-proxy => Redis
```

- **Nginx** — єдина зовнішньо доступна точка: завершує TLS та проксіює HTTP/WebSocket.
- **console-proxy** — асинхронний застосунок Python/aiohttp.
- **Redis** — короткочасне спільне сховище сесій, browser claim та cookies провайдера.

## 1. Встановлення Docker і Git

Якщо на сервері немає служб Docker, Compose або Gir - підключіться до сервера через SSH і виконайте:

```bash
sudo apt update
sudo apt install -y git curl ca-certificates openssl certbot

curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
rm get-docker.sh

sudo usermod -aG docker "$USER"
```

Після додавання до групи `docker` вийдіть із SSH і підключіться повторно:

```bash
exit
```

Перевірте встановлення:

```bash
docker --version
docker compose version
docker ps
```

## 2. Створення сертифікатів для домену

До першого `docker compose up` certificate files **повинні вже існувати**. Якщо
вони відсутні, сучасна Compose-конфігурація завершиться помилкою замість створення
помилкових директорій із суфіксом `.pem`.

sudo certbot certonly \
  --standalone \
  --email <ваш-робочий-email> \
  --agree-tos \
  --no-eff-email \
  -d remote-control.re
```

Після успішного випуску перевірте файли:

```bash
sudo ls -la /etc/letsencrypt/live/remote-control.re/
sudo openssl x509 \
  -in /etc/letsencrypt/live/remote-control.re/fullchain.pem -noout -subject -issuer -dates
sudo openssl pkey \
  -in /etc/letsencrypt/live/remote-control.re/privkey.pem -noout -check
```

`fullchain.pem` повинен бути файлом/символічним посиланням, а його перший рядок
має бути `-----BEGIN CERTIFICATE-----`.

## 3. Клонування потрібної гілки

Створіть каталог застосунку:

```bash
sudo mkdir -p /opt/console-proxy
sudo chown "$USER":"$USER" /opt/console-proxy
```

Для публічного репозиторію (зараз він публічний):

```bash
git clone --branch transfer-to-docker-compose --single-branch \
  https://github.com/boby-star/console-proxy.git \
  /opt/console-proxy
```

## 4. Production-конфігурація

Створіть `.env` лише на сервері:

```bash
cd /opt/console-proxy
mv .env.example .env
chmod 600 .env
vim .env
```

Приклад для домену `remote-control.re`:

```dotenv
PUBLIC_BASE_URL=https://remote-control.re

# Сюди треба вставити секрет, який передаємо в модуль (з ним бекенд billmanager звертається до сервіса)
REGISTER_API_TOKEN=REPLACE_WITH_A_LONG_RANDOM_SECRET

# Це white-list. Сюди можна вносити домени які сервіс сприймає. Цю змінну можна масштабувати якщо будуть інші провайдери.
ALLOWED_HOST_SUFFIXES=cloud.gcore.com,ipmi.ovh.net
SESSION_TTL_SECONDS=3000
PREFETCH_PROVIDER_COOKIES=true

TLS_CERT_PATH=/etc/letsencrypt/live/remote-control.re/fullchain.pem
TLS_KEY_PATH=/etc/letsencrypt/live/remote-control.re/privkey.pem
```

## 5. Запуск

Перед запуском перевірте розгорнуту Compose-конфігурацію:

```bash
cd /opt/console-proxy
docker compose config
```

Зберіть image із локального Git checkout та запустіть stack:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=200
```

Очікувані сервіси:

```text
nginx
console-proxy
redis
```

Перевірка готовності:

```bash
curl -fsS https://remote-control.re/health
curl -fsS https://remote-control.re/ready
```

Очікувані відповіді:

```json
{"status":"ok"}
```

та:

```json
{"status":"ready"}
```