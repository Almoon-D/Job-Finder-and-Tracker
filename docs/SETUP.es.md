# Guía de instalación (español)

En 20–30 minutos tendrás la herramienta funcionando en GitHub Actions, con tus preferencias
en un repositorio privado. No necesitas instalar nada en tu ordenador.

## 0. Cómo encaja todo

| Pieza | Visibilidad | Contenido |
|---|---|---|
| Este repo (o tu fork) | **Público** | Código y workflows. Los minutos de Actions son gratis e ilimitados en repos públicos. |
| `job-finder-data` | **Privado** | `config.yaml` (tus preferencias), estado, feeds e informes. |
| Secrets del repo público | Cifrados | Tokens de acceso, Telegram, email, IA… |

Los logs de Actions de un repo público son visibles para cualquiera. Por eso la herramienta
nunca escribe en ellos nombres de empresas, puestos ni búsquedas: solo cuenta ofertas y
numera fuentes (`source #3`). El detalle de cada ejecución queda en
`runs/last_run.json` dentro de tu repo privado.

## 1. Repositorio público

1. Haz **fork** de este repositorio o usa directamente el tuyo.
2. Pestaña **Actions** → activa los workflows si GitHub lo pide. En un fork vienen
   desactivados por defecto.

## 2. Repositorio privado de datos

1. Crea un repositorio nuevo, **privado**, por ejemplo `job-finder-data`.
2. Añade un archivo `config.yaml`. Parte de [`config.example.yaml`](../config.example.yaml) o
   de la configuración personal que te hayan preparado.
3. Para editar tus preferencias más adelante, modifica `config.yaml` desde la web o la app
   de GitHub. Cada cambio queda en el historial.

## 3. Token de acceso al repo privado (PAT #1)

GitHub → foto de perfil → **Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token**:

- **Repository access**: *Only select repositories* → `job-finder-data`.
- **Permissions → Repository permissions → Contents**: *Read and write*.
- **Expiration**: la máxima que te permita GitHub. Apúntate la fecha de caducidad para
  renovarlo; si caduca, la ejecución fallará en el paso *Checkout private data repository*.

## 4. Variables y secrets del repo público

Repo público → **Settings → Secrets and variables → Actions**:

**Pestaña Variables**
- `DATA_REPO` = `tu-usuario/job-finder-data`

**Pestaña Secrets** (solo los que uses)

| Secret | Para qué |
|---|---|
| `DATA_REPO_TOKEN` | El PAT #1 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` | Email |
| `DISCORD_WEBHOOK_URL` | Discord |
| `NTFY_TOPIC` (y `NTFY_TOKEN` si tu servidor lo pide) | ntfy |
| `APPRISE_URLS` | Otros servicios vía Apprise (Slack, Gotify, Pushover…), separados por espacios |
| `GEMINI_API_KEY`, `NVIDIA_API_KEY`, `GROQ_API_KEY` (también `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY`, `MISTRAL_API_KEY`) | IA opcional |

Los workflows pasan al programa **solo** los secrets de esta tabla, uno a uno (bloque `env:` del
paso *Run* en `.github/workflows/jobfinder.yml` y `favorites.yml`). No se pasan todos de golpe a
propósito: GitHub marca como «posiblemente malicioso» un workflow que vuelca todos los secrets y no
lo ejecuta. Si en `config.yaml` usas otro nombre de variable (por ejemplo `api_key_env: MI_CLAVE`),
crea el secret **y** añade una línea `MI_CLAVE: ${{ secrets.MI_CLAVE }}` en ese bloque de los dos
workflows.

## 5. Canales de notificación

Activa en `config.yaml` (`notify:`) los canales que vayas a usar.

### Telegram (recomendado)
1. En Telegram, habla con **@BotFather** → `/newbot` → copia el token → secret `TELEGRAM_BOT_TOKEN`.
2. Escribe cualquier mensaje a tu bot.
3. Abre `https://api.telegram.org/bot<TOKEN>/getUpdates` en el navegador y copia el número
   de `"chat":{"id": ...}` → secret `TELEGRAM_CHAT_ID`.

### Email con Gmail
1. Activa la **verificación en dos pasos** de tu cuenta de Google.
2. Crea una **contraseña de aplicación** en <https://myaccount.google.com/apppasswords>.
3. Crea estos secrets:
   - `SMTP_HOST=smtp.gmail.com`
   - `SMTP_PORT=587`
   - `SMTP_USER=tu@gmail.com`
   - `SMTP_PASSWORD=<contraseña de aplicación>`
   - `EMAIL_TO=destino@loquesea.com` (varios separados por comas)

### Discord
Canal → **Editar canal → Integraciones → Webhooks → Nuevo webhook** → *Copiar URL* →
secret `DISCORD_WEBHOOK_URL`.

### ntfy
Instala la app ntfy y suscríbete a un topic con un nombre **largo y aleatorio**, por ejemplo
`jf-7d3k29x-q8v1`. En ntfy.sh los topics son públicos para quien adivine su nombre. Después
crea el secret `NTFY_TOPIC`. Si usas tu propio servidor, cambia `notify.ntfy.server` en la
config.

## 6. IA gratuita (opcional pero recomendada)

La IA solo evalúa las ofertas que pasan los filtros baratos (ubicación, fecha, exclusiones)
y añade a cada oferta una puntuación de encaje, los años de experiencia pedidos y una razón
breve.

- **Gemini**: crea una clave en <https://aistudio.google.com/apikey> → secret `GEMINI_API_KEY`.
  El modelo por defecto es `gemini-3.5-flash-lite`: su plan gratuito admite cientos de
  peticiones al día y cada una evalúa unas 10 ofertas.
- **Groq** (respaldo): crea una clave en <https://console.groq.com/keys> → secret `GROQ_API_KEY`.
- Se puede usar cualquier API compatible con OpenAI (NVIDIA NIM, OpenRouter, Cerebras,
  Mistral…); basta con añadirla en `llm.providers`.
- Privacidad: los planes gratuitos pueden usar los datos para entrenar. Solo se envían
  ofertas públicas y tu texto `profile`, así que mantenlo **anónimo**.
- Sin ninguna clave, la herramienta funciona igual usando solo keywords.

## 7. Primeras ejecuciones

Pestaña **Actions → jobfinder → Run workflow**:

1. `mode: validate` comprueba la config. Si hay errores, el log solo dice en qué campo
   están y el detalle completo queda en `runs/config_error.txt` del repo privado.
2. `mode: test-notify` envía un mensaje de prueba a cada canal activo.
3. `mode: run` con `force: true` hace una primera ejecución real.
   - En esa primera pasada, las ofertas **sin fecha** quedan como «línea base» y no se
     notifican, para no recibir una avalancha de golpe.
   - Las ofertas **con fecha** de los últimos días sí se notifican.
   - Si prefieres no recibir nada al empezar, lanza antes `mode: bootstrap`.

## 8. Puntualidad: disparador externo (cron-job.org)

El cron de GitHub se retrasa a menudo entre 15 minutos y más de 2 horas, y a veces se salta
ejecuciones. Para que los avisos lleguen a su hora y las favoritas se revisen cada 10 minutos, un
servicio externo gratuito «llama» a GitHub a la hora exacta.

1. Crea un segundo token fine-grained (**PAT #2**): *Only select repositories* → tu repo
   **público**; **Permissions → Actions: Read and write**. No le des ningún otro permiso.
   Copia el token al crearlo (GitHub no vuelve a mostrarlo).
   > **Dónde va el PAT #2:** en **cron-job.org**, no en GitHub. No lo guardes como secret: se pega
   > en la cabecera `Authorization` de cada cronjob (paso 2), después de la palabra `Bearer` y un
   > espacio. Es lo que autoriza a cron-job.org a lanzar tus workflows.
2. Crea una cuenta gratuita en <https://cron-job.org> → **Create cronjob**:
   - **URL**: `https://api.github.com/repos/TU-USUARIO/TU-REPO-PUBLICO/actions/workflows/jobfinder.yml/dispatches`
   - **Schedule**: personalizado, zona horaria *Europe/Madrid*: 09:00, 11:00 y 20:30. Si la
     interfaz no admite horas distintas en un mismo trabajo, crea dos.
   - **Advanced → Request method**: `POST`
   - **Advanced → Headers** (tres cabeceras):

     | Key | Value |
     |---|---|
     | `Accept` | `application/vnd.github+json` |
     | `Authorization` | `Bearer github_pat_…` ← aquí pegas el PAT #2 |
     | `X-GitHub-Api-Version` | `2022-11-28` |
   - **Advanced → Request body**: `{"ref":"main","inputs":{"mode":"run"}}`
   - Pulsa **Test run**: la respuesta correcta es **204**. Un 401 significa token mal pegado o
     caducado; un 404, URL o permiso *Actions* incorrectos.
3. Crea otro cronjob igual (mismas tres cabeceras, mismo PAT #2) para las **favoritas**, cada 10
   minutos:
   - URL: `.../actions/workflows/favorites.yml/dispatches`
   - Body: `{"ref":"main"}`
4. No hace falta quitar los cron de GitHub: quedan de respaldo. La ejecución es
   **idempotente**, así que nunca recibirás un aviso duplicado aunque se disparen ambos.
5. Cuando caduque el PAT #2, crea otro y sustitúyelo en la cabecera `Authorization` de los dos
   cronjobs.

Los horarios reales de cada grupo se definen en `groups:` de tu `config.yaml`. Los cron de
los workflows y de cron-job.org solo «despiertan» el sistema. Si cambias los horarios en la
config, ajusta también cron-job.org (o las líneas `cron:` de `.github/workflows/jobfinder.yml`).

## 9. Empresas favoritas (tipo 4)

- Marca `favorite: true` en las fuentes que quieras vigilar cada 10 minutos. Recomendado:
  como máximo unas 10, y solo fuentes de ATS/API, **nunca LinkedIn**.
- La vacante te llega al momento con prioridad alta y después aparece también en el resumen
  normal de las 09:00/20:30, marcada «⚡ ya avisada».
- `quiet_hours` (por defecto 23:00–07:00) evita revisiones nocturnas. Lo que salga de noche
  llega en el primer aviso de la mañana.
- El estado solo se guarda cuando algo cambia, así que tu repo privado no se llena de commits.

## 10. Dashboards (Glance, Homepage…)

Cada ejecución escribe en el repo privado:

- `feeds/jobs.json` (JSON Feed 1.1)
- `feeds/jobs.xml` (RSS 2.0)
- `feeds/summary.json` (conteos y últimas ofertas)

Para leerlos desde un dashboard autoalojado:

1. Crea un **PAT #3** de solo lectura: *Contents: Read-only* sobre `job-finder-data`.
2. Usa la API de contenidos de GitHub:
   - URL: `https://api.github.com/repos/TU-USUARIO/job-finder-data/contents/feeds/summary.json`
   - Cabeceras: `Authorization: Bearer <PAT #3>` y `Accept: application/vnd.github.raw+json`

Ejemplos listos para Glance y Homepage llegarán en la sesión de dashboards.

## 11. Mantenimiento y problemas

- **Una fuente falla**: la notificación incluye «⚠️ Fuentes con problemas» tras 2 fallos
  seguidos, o si devuelve 0 resultados de forma anómala. Revisa `runs/last_run.json` en el
  repo privado.
- **No llega nada**: comprueba en Actions que la ejecución terminó en verde. Si *todos* los
  canales fallan, la ejecución se marca en rojo y GitHub te avisa por email. Las ofertas no
  se marcan como enviadas, así que se reintentan en la siguiente ejecución.
- **Regla de 60 días**: GitHub desactiva los cron de repos públicos sin actividad. El job
  `keepalive` (domingos) los reactiva automáticamente.
- **Probar una fuente en tu ordenador** (opcional, requiere Python y uv):
  ```bash
  uv sync
  uv run jobfinder test-source "Nombre de la fuente" --config ruta/a/config.yaml --details
  uv run jobfinder dry-run --config ruta/a/config.yaml --data-dir /tmp/jf -v
  ```
