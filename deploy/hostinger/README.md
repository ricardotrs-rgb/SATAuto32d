# Despliegue en Hostinger VPS

Este proyecto se despliega en **Hostinger VPS con Linux**, Docker y Nginx. No es compatible con hosting compartido porque la aplicacion requiere Python, Gunicorn, Playwright y procesos en segundo plano.

## Requisitos

- Hostinger VPS con Ubuntu 22.04 o similar
- Dominio propio apuntando al VPS
- Docker y Docker Compose Plugin instalados
- Puertos 80 y 443 abiertos

## Paso 1: Apuntar el dominio

En el panel DNS de Hostinger crea estos registros:

- `A` para `@` hacia la IP publica del VPS
- `A` o `CNAME` para `www` hacia `@`

## Paso 2: Subir el proyecto

Clona el repositorio en el VPS o sube los archivos por `git`/SFTP.

## Paso 3: Configurar variables

Dentro del proyecto:

```bash
cp .env.production.example .env.production
```

Edita `.env.production` y ajusta:

- `SAT32D_WEB_SECRET`
- `SAT32D_WEB_ALLOWED_PASSWORD`
- `SAT32D_WEB_ALLOWED_HOSTS` con tu dominio real

## Paso 4: Levantar contenedores

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8000/healthz
```

## Paso 5: Configurar Nginx

1. Copia [deploy/nginx/sat32d.conf](../nginx/sat32d.conf) a `/etc/nginx/sites-available/sat32d.conf`.
2. Reemplaza `YOUR_DOMAIN` por tu dominio real.
3. Habilita el sitio:

```bash
sudo ln -s /etc/nginx/sites-available/sat32d.conf /etc/nginx/sites-enabled/sat32d.conf
sudo nginx -t
sudo systemctl reload nginx
```

## Paso 6: Emitir HTTPS

```bash
sudo apt-get update
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d YOUR_DOMAIN -d www.YOUR_DOMAIN
```

## Paso 7: Firewall

En el firewall del VPS permite:

- SSH
- HTTP 80
- HTTPS 443

## Notas

- La app queda expuesta en `127.0.0.1:8000` dentro del VPS.
- Nginx publica el sitio al dominio.
- Los datos persistentes quedan en `sat32d_data` y `web_storage`.