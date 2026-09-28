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
| `GEMINI_API_KEY`, `NVIDIA_API_KEY`, `GROQ_API_KEY` (también `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY`, `MISTRAL_API_KEY`) | IA opcional (§6) |
| `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` | Alertas por email (§13) |
| `INFOJOBS_CLIENT_ID`, `INFOJOBS_CLIENT_SECRET` | InfoJobs, opcional (§12) |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Adzuna, opcional (§12) |

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
breve. Los proveedores de `llm.providers` se prueban **en orden**: si uno agota su cuota
(HTTP 429) o su clave falla, se pasa al siguiente al momento, sin reintentos, y no se le vuelve
a llamar en esa ejecución. Si fallan todos, se usa el filtro por keywords.

Orden recomendado (plan gratuito):

1. **Gemini 3.8 Flash**: la mejor calidad, pero solo unas **20 peticiones al día**. Con
   `batch_size: 15` son unas 300 ofertas/día.
2. **NVIDIA NIM** (p. ej. `deepseek-ai/deepseek-v4.1-flash`): unas 40 peticiones por minuto y
   sin tope diario conocido. Es el respaldo principal.
3. **Gemini 3.5 Flash-Lite**: misma clave que el 1, cuota propia de unas 500 peticiones/día.
4. **Groq** (`openai/gpt-oss-120b`): último respaldo.

Los límites cambian: consulta los tuyos en <https://aistudio.google.com/rate-limit>.

Claves (cada una es un secret del repo público):

- **Gemini**: <https://aistudio.google.com/apikey> → `GEMINI_API_KEY` (vale para los modelos
  Flash y Flash-Lite).
- **NVIDIA**: <https://build.nvidia.com> → inicia sesión (cuenta gratuita de NVIDIA Developer,
  pide verificar el teléfono) → *Get API Key* → `NVIDIA_API_KEY` (empieza por `nvapi-`).
- **Groq**: <https://console.groq.com/keys> → `GROQ_API_KEY`.
- Cualquier otra API compatible con OpenAI (OpenRouter, Cerebras, Mistral…) se añade en
  `llm.providers`. Opciones extra del modelo van en `extra_body`, por ejemplo
  `extra_body: {reasoning_effort: low}`.

Comprueba que todo responde con **Actions → jobfinder → Run workflow → `mode: test-ai`**. El
log solo muestra `provider #1 (gemini-flash): OK in 1.4s` o el código de error. Un `HTTP 404`
suele indicar un nombre de modelo mal escrito, y un `HTTP 401`, una clave incorrecta.

- Privacidad: los planes gratuitos pueden usar los datos para entrenar. Solo se envían
  ofertas públicas, tu texto `profile` (mantenlo **anónimo**) y, si activas `unknown: ai`,
  el texto de los emails de alerta de remitentes desconocidos (§13).
- Sin ninguna clave, la herramienta funciona igual usando solo keywords.

## 7. Primeras ejecuciones

Pestaña **Actions → jobfinder → Run workflow**:

1. `mode: validate` comprueba la config. Si hay errores, el log solo dice en qué campo
   están y el detalle completo queda en `runs/config_error.txt` del repo privado.
2. `mode: test-notify` envía un mensaje de prueba a cada canal activo, y `mode: test-ai`
   comprueba los proveedores de IA.
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

## 12. Portales de empleo (grupo `boards`)

El grupo `boards` envía cada 3 días (11:00) un resumen **agrupado por familia de puesto y
ubicación** (`format: grouped`). Solo entran las mejores `max_items`, por encaje y fecha; el
resto queda en el feed. Una oferta que ya te llegó por otro grupo (por ejemplo desde la web de
la empresa) no se repite: el pie lo indica con «🔁 N ya avisadas en otros grupos» y el feed la
marca «También en …». Para decidir que dos ofertas son la misma se comparan la empresa y el
título normalizados (sin «(m/f/d)», sin ciudades…) y la URL.

| Portal | Qué hace falta |
|---|---|
| LinkedIn, eFinancialCareers, jobup.ch, jobs.ch, OCC, Computrabajo | Nada |
| InfoJobs | Credenciales gratuitas de la API (abajo) |
| Adzuna | Credenciales gratuitas de la API (abajo) |
| Indeed | Sus alertas por email (§13): no tiene API pública |

**InfoJobs** (opcional): entra en <https://developer.infojobs.net> con tu cuenta de InfoJobs →
*Registrar aplicación* → copia el *Client ID* y el *Client Secret* → secrets
`INFOJOBS_CLIENT_ID` e `INFOJOBS_CLIENT_SECRET`.

**Adzuna** (opcional): regístrate en <https://developer.adzuna.com> → *Dashboard* → copia el
*Application ID* y la *Application Key* → secrets `ADZUNA_APP_ID` y `ADZUNA_APP_KEY`.

Mientras no existan esas claves, la fuente aparece como `skipped` en el log y no genera
alertas de salud. Para probar una fuente concreta en Actions usa **`mode: test-source`** con el
**número** de la fuente (el que aparece en los logs como `source #7`), no su nombre, que
quedaría visible en el log público.

## 13. Alertas por email (IMAP)

Muchos portales (Indeed sobre todo) solo ofrecen alertas por email, y suelen ser más finas que
cualquier búsqueda. La herramienta lee un buzón **dedicado** en modo solo lectura: nunca marca,
mueve ni borra correos.

1. Crea una cuenta de Gmail nueva, solo para esto (por ejemplo `tunombre.alertas@gmail.com`).
2. Activa la **verificación en dos pasos** y crea una **contraseña de aplicación** en
   <https://myaccount.google.com/apppasswords>.
3. IMAP ya viene activado en Gmail; si no, en Gmail → Configuración → *Reenvío y correo
   POP/IMAP* → *Habilitar IMAP*.
4. Crea los secrets `IMAP_USER` (la dirección) e `IMAP_PASSWORD` (la contraseña de aplicación,
   sin espacios). `IMAP_HOST` solo si no usas Gmail (por defecto `imap.gmail.com`).
5. Crea las alertas **con esa dirección de email**:
   - **LinkedIn**: busca empleos → *Crear alerta* (frecuencia diaria).
   - **Indeed** (es/ch/mx): busca → *Recibir nuevos empleos por email*.
   - **InfoJobs**: busca → *Crear alerta*.
   - **eFinancialCareers**: busca → *Create job alert*.
   - **jobup.ch / jobs.ch**: busca → *Créer une alerte*.
   - **Michael Page**: busca → *Crear alerta*. Llega al grupo `recruiters`.
   - Otros portales (Welcome to the Jungle…) también sirven con `unknown: ai`: sus emails
     los lee la IA y solo se aceptan enlaces que aparecen en el propio email.
6. En la config privada, las fuentes `type: email_alerts` deciden qué remitente va a cada
   grupo (`parsers`, `ai_senders`, `exclude_senders`). Consulta `docs/SOURCES.md`.

Cada email se procesa una sola vez (se guarda el último UID en `state/runs.json`). Solo se miran
los emails de los últimos 4–5 días. Los logs públicos solo muestran conteos
(`email: 12 new messages … 30 jobs`).
