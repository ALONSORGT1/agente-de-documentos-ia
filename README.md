# Agente de documentos IA

[Abrir la aplicación en línea](https://agente-de-documentos-ia.vercel.app). El acceso a los documentos y las consultas requiere la clave compartida de la instalación.

Encuentra respuestas en tus documentos de Google Drive sin revisar cada archivo por separado. Agente de documentos IA reúne una biblioteca autorizada, busca los fragmentos relevantes y presenta una respuesta con fuentes que puedes abrir y comprobar.

La aplicación combina una interfaz web en español, un servidor Python con FastAPI y OpenAI File Search. Admite PDF con texto, Word (.docx), archivos TXT y Google Docs. Word se convierte en texto para buscar en párrafos y tablas, conservando el nombre y el enlace al original. El contenido compatible se transfiere a OpenAI para crear el índice de búsqueda; los originales permanecen en Drive.

## Qué puedes hacer

- Consultar manuales, procedimientos, políticas y documentación de un equipo.
- Comparar lo que dicen varios documentos sobre un mismo tema.
- Localizar instrucciones o requisitos y revisar los fragmentos que respaldan la respuesta.
- Detectar cuándo la biblioteca no contiene evidencia suficiente para responder.
- Compartir un punto de consulta web con las personas que tengan la clave de acceso.

El modo documental busca evidencia antes de responder. Si la evidencia o las citas no son suficientes, lo indica. También existe un modo experimental sin búsqueda documental, identificado en la interfaz y sin fuentes verificadas.

## Cómo funciona

1. **Conectas una carpeta de Drive.** Autorizas su lectura desde tu equipo y configuras esa conexión privada en Vercel.
2. **Se crea el índice en OpenAI.** Los documentos se preparan para encontrar los fragmentos que respaldan cada respuesta.
3. **Actualizas desde la web.** Pulsa **Actualizar base documental** después de añadir o modificar archivos en Drive. Verás los nuevos, actualizados y sin cambios.
4. **Consultas y compruebas las fuentes.** El servidor responde con enlaces a los documentos originales.

El índice y el seguimiento de cambios se conservan entre recargas, reinicios y despliegues en un almacenamiento privado de Vercel Blob. La aplicación revisa los metadatos de Drive y reutiliza los archivos ya indexados. Solo descarga y procesa los nuevos o modificados; si el contenido sigue siendo idéntico, reutiliza también su copia en OpenAI. Renombrar un documento no exige indexarlo otra vez.

La actualización avanza por tandas y guarda cada paso. Si una indexación tarda, puede continuarse sin duplicar cargas. No es una vigilancia automática de Drive: pulsa el botón cuando quieras incorporar cambios. Abrir o recargar la página muestra la última base guardada sin volver a descargar los documentos.

## Requisitos

- Python 3.11 o superior para ejecutar y sincronizar en tu equipo.
- Una cuenta de OpenAI con acceso a Responses, File Search y saldo o facturación habilitada.
- Una cuenta de Google con acceso a la carpeta que quieres consultar.
- Google Drive API habilitada y un cliente OAuth de tipo **Desktop app**.
- Para publicar: una cuenta de GitHub y una cuenta de Vercel.

Usa únicamente documentos que tengas autorización para transferir a OpenAI. Abrir un enlace a un original sigue sujeto a los permisos de Google Drive de cada persona.

## Ejecutar en tu equipo

Clona el repositorio y abre PowerShell en su carpeta:

```powershell
git clone https://github.com/ALONSORGT1/agente-de-documentos-ia.git
cd agente-de-documentos-ia
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
Copy-Item .env.example .env
```

El repositorio es privado: necesitas acceso a la cuenta o una invitación para clonarlo. Si ya existe `.env`, conserva su contenido y actualízalo en lugar de sobrescribirlo. Configura al menos:

```dotenv
OPENAI_API_KEY=tu_clave_de_openai
OPENAI_MODEL=un_modelo_compatible
GOOGLE_DRIVE_FOLDER_ID=id_de_la_carpeta
DEPLOYMENT_MODE=local
```

Elige un modelo disponible en tu cuenta que permita Responses y File Search. El ID de la carpeta es la parte que sigue a `/folders/` en su enlace de Drive; no pegues la URL completa.

En [Google Cloud Console](https://console.cloud.google.com/), habilita Google Drive API, configura el consentimiento OAuth y descarga el cliente Desktop como `credentials.json` junto a `app.py`. Si la aplicación de Google está en pruebas, añade tu cuenta a los usuarios autorizados.

Inicia el servidor:

```powershell
.\.venv\Scripts\python.exe app.py
```

Abre [http://127.0.0.1:5000](http://127.0.0.1:5000) y pulsa **Actualizar base documental**. La primera vez se abrirá la autorización de Google en tu equipo. El permiso solicitado es de lectura: la aplicación no modifica ni elimina los archivos originales de Drive.

Mantén un solo proceso del servidor local. El puerto predeterminado es `5000` y puede cambiarse con `PORT`.

## Configuración

Las claves se guardan en `.env` para uso local y en las variables de entorno de Vercel para el despliegue. La clave de OpenAI permanece en el servidor y no se entrega al navegador.

| Variable | Para qué sirve |
|---|---|
| `OPENAI_API_KEY` | Autoriza las consultas y la indexación en OpenAI. Es secreta. |
| `OPENAI_MODEL` | Selecciona el modelo compatible con Responses y File Search. |
| `GOOGLE_DRIVE_FOLDER_ID` | Identifica la carpeta autorizada y el alcance del índice. |
| `DEPLOYMENT_MODE` | `local` para sincronizar en tu equipo; `cloud` para consultar y actualizar desde Vercel. |
| `APP_ACCESS_KEY` | Clave compartida para proteger las consultas en la nube. Es secreta. |
| `DOCUMENT_MANIFEST_JSON` | Instantánea inicial y alternativa de consulta si no se configura Blob. Es privada. |
| `BLOB_READ_WRITE_TOKEN` | Conexión al almacenamiento **privado** de Vercel Blob. Es secreta. |
| `GOOGLE_TOKEN_JSON` | Contenido del token OAuth autorizado con `drive.readonly`. Permite actualizar desde Vercel y es secreto. |
| `DRIVE_INCLUDE_SUBFOLDERS` | Incluye subcarpetas con `true`; por defecto están excluidas. Debe coincidir con el índice. |
| `GOOGLE_CREDENTIALS_PATH` | Ruta del cliente OAuth local; por defecto `credentials.json`. |
| `GOOGLE_TOKEN_PATH` | Ruta del token OAuth local; por defecto `token.json`. |
| `LOCAL_ADMIN_KEY` | Clave opcional para autorizar la sincronización local. |
| `VECTOR_STORE_ID` | Comprobación opcional del índice esperado. Normalmente se reutiliza el del manifiesto. |

`.env.example` incluye opciones para limitar tamaño de archivos, cantidad de documentos, longitud de respuesta, tiempos de espera y solicitudes por minuto.

## Publicar en Vercel

El repositorio es [ALONSORGT1/agente-de-documentos-ia](https://github.com/ALONSORGT1/agente-de-documentos-ia). El proyecto se administra desde [Vercel · agente-de-documentos-ia](https://vercel.com/acme-2f81/agente-de-documentos-ia), dentro del equipo ACME.

Para implementar tu propia biblioteca:

1. Ejecuta la aplicación local, sincroniza la carpeta autorizada y comprueba una consulta con sus fuentes.
2. Prepara la configuración de la nube:

```powershell
.\.venv\Scripts\python.exe scripts/prepare_cloud.py
```

3. El script valida el manifiesto local y prepara tres archivos privados en `.deployment/`: `manifest.json` con la instantánea, `vercel-env.json` con las variables de entorno y `access-key.txt` con la clave de acceso. Reutiliza la clave existente; `--rotate-access-key` permite cambiarla de forma explícita.
4. Publica el código en GitHub y enlázalo con Vercel. La raíz del repositorio contiene `app.py`, `requirements.txt`, `index.html` y `vercel.json`; deja **Root Directory** vacío. La configuración incluida selecciona FastAPI.
5. En las variables de entorno de **Production**, configura `DEPLOYMENT_MODE=cloud`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `APP_ACCESS_KEY`, `DOCUMENT_MANIFEST_JSON`, `GOOGLE_DRIVE_FOLDER_ID` y `DRIVE_INCLUDE_SUBFOLDERS` con los valores preparados. Marca los secretos como sensibles. La clave de OpenAI se configura por separado en Vercel.
6. Crea un almacenamiento **privado** de Vercel Blob y conéctalo al proyecto. Vercel añade `BLOB_READ_WRITE_TOKEN`. Para la preparación local, descarga las variables de Development a `.env.local` con `vercel env pull`; conserva ese archivo fuera de Git.
7. Inicializa ese almacenamiento **una sola vez**, reutilizando el índice existente:

```powershell
.\.venv\Scripts\python.exe scripts/initialize_cloud.py
```

El script usa `.deployment/manifest.json` y la conexión de `.env.local`. Rechaza sobrescribir una base existente. Antes de publicarla, configura `GOOGLE_TOKEN_JSON` en Production con el JSON de tu `token.json` local ya autorizado; márcalo como sensible. No pegues ese valor en JavaScript ni lo subas a GitHub.
8. Crea un despliegue, abre la dirección de Vercel e introduce la clave de `.deployment/access-key.txt`. El acceso dura seis horas mediante una cookie segura; F5 conserva el acceso y la base documental. **Cerrar sesión** termina el acceso en ese navegador.
9. Pulsa **Actualizar base documental** y comprueba el listado y una pregunta con fuentes.

`data/`, `.deployment/`, `.env*`, `credentials.json` y `token.json` son privados y están excluidos de Git. Vercel recibe el token únicamente como una variable secreta del servidor. No abre ventanas de autorización de Google ni necesita copiar los archivos OAuth al despliegue.

Una vez activada la actualización en la nube, esa base guardada es la referencia para producción. Usa el botón de la web; no sincronices en paralelo una copia local antigua del mismo índice ni vuelvas a reemplazar el estado de Blob con la instantánea inicial. Cada instalación independiente debe utilizar su propio índice.

Si Google revoca o vence la autorización, autoriza de nuevo desde el equipo local y actualiza `GOOGLE_TOKEN_JSON` en Vercel; publica un nuevo despliegue para aplicar el secreto. Esto no exige volver a indexar los documentos. La modalidad de consentimiento OAuth de Google determina la duración de su autorización.

Vercel es la opción recomendada porque ejecuta el servidor FastAPI y mantiene la clave de OpenAI en el servidor. GitHub Pages sirve páginas estáticas y necesitaría otro servicio para ejecutar esta API.

## Integrarlo con otra interfaz

La interfaz y la API incluidas se sirven desde el mismo dominio. Estas son las rutas principales:

| Método | Ruta | Uso |
|---|---|---|
| `GET` | `/` | Abre la interfaz. |
| `GET` | `/api/health` | Muestra comprobaciones de configuración; no prueba las APIs externas. |
| `GET` | `/api/documents` | Lista documentos activos y fecha de última sincronización. |
| `POST` | `/api/ask` | Devuelve una respuesta con sus fuentes. |
| `POST` | `/api/sync` | Actualiza Drive de forma incremental. En la nube devuelve `has_more` para continuar las tandas. |
| `POST` | `/api/session` | Crea la sesión segura de seis horas usando `X-App-Key`. |
| `POST` | `/api/logout` | Cierra la sesión del navegador. |

En modo nube, el listado, las consultas y la actualización requieren una sesión válida o la cabecera `X-App-Key`. La interfaz envía la clave una vez y recibe una cookie `Secure`, `HttpOnly` y `SameSite=Strict` de seis horas. No guarda la clave en localStorage ni sessionStorage. `/api/health` es público y no devuelve secretos.

Para consultar, envía también `Content-Type: application/json` y `X-Requested-With: AgenteDocumentalTIC`, con este cuerpo:

```json
{
  "question": "¿Qué pasos indica el procedimiento de atención?",
  "use_search": true,
  "history": []
}
```

La respuesta incluye `answer`, `sources`, `grounded`, `mode`, `warning` y el consumo disponible en `usage`. Cada fuente puede contener nombre, enlace y fragmento. El historial admite hasta seis mensajes; la interfaz conserva las preguntas previas en memoria y las elimina al recargar o limpiar el contexto.

No está habilitado el acceso directo desde cualquier origen. Para integrar una interfaz en otro dominio, prepara un servidor que gestione autenticación y acceso a esta API. Nunca coloques `OPENAI_API_KEY` ni la clave compartida en un archivo JavaScript público.

## Límites que conviene conocer

- Los PDF y Word necesitan texto extraíble. No hay OCR para documentos formados solo por imágenes. Se admite `.docx`; el formato antiguo `.doc` debe convertirse a `.docx`.
- Las respuestas dependen de los fragmentos recuperados y no garantizan una revisión completa de la biblioteca. Comprueba las fuentes antes de tomar decisiones.
- Los documentos se consultan según la última actualización guardada. Tras cambiar archivos o permisos en Drive, pulsa **Actualizar base documental** para reflejarlo en el índice.
- El modo nube comparte una biblioteca y una clave de acceso. No ofrece cuentas individuales, roles ni permisos por documento.
- Las consultas y actualizaciones de la nube comparten un bloqueo persistente para evitar cambios simultáneos sobre la evidencia. El límite de solicitudes sigue siendo por proceso; no representa una cuota global de consumo.
- Las consultas, búsquedas y archivos almacenados en OpenAI pueden generar cargos. Configura presupuestos y revisa el consumo de tu cuenta.
- Conserva un respaldo privado del estado de Blob (o de `data/` en modo local). No borres el manifiesto para resolver un fallo: contiene el seguimiento de archivos e indexaciones remotas.

## Comprobar el proyecto

Desde la carpeta de la aplicación:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/frontend.test.mjs
.\.venv\Scripts\python.exe -m pip check
```

Node.js solo es necesario para las pruebas de la interfaz. Las pruebas automatizadas utilizan datos temporales y servicios simulados, sin consumir OpenAI ni autorizar Google. Una comprobación real del despliegue requiere revisar el acceso y hacer una consulta con documentos autorizados.

## Organización del código

- `app/main.py`: rutas, acceso y respuestas de la API.
- `app/core/`: configuración, errores y cliente de OpenAI.
- `app/services/`: conexión a Drive, validación, sincronización y consultas con evidencia.
- `scripts/prepare_cloud.py` e `initialize_cloud.py`: preparación privada e inicio del almacenamiento.
- `assets/` e `index.html`: interfaz web.
- `tests/`: pruebas del servidor y de la interfaz.
- `data/`: estado local privado, excluido del repositorio.

Referencias: [FastAPI en Vercel](https://vercel.com/docs/frameworks/backend/fastapi), [File Search de OpenAI](https://developers.openai.com/api/docs/guides/tools-file-search) y [Google Drive API](https://developers.google.com/workspace/drive/api/guides/about-sdk).
