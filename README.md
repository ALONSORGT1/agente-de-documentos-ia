# Agente de documentos IA

[Abrir la aplicación en línea](https://agente-de-documentos-ia.vercel.app). El acceso a los documentos y las consultas requiere la clave compartida de la instalación.

Encuentra respuestas en tus documentos de Google Drive sin revisar cada archivo por separado. Agente de documentos IA reúne una biblioteca autorizada, busca los fragmentos relevantes y presenta una respuesta con fuentes que puedes abrir y comprobar.

La aplicación combina una interfaz web en español, un servidor Python con FastAPI y OpenAI File Search. Admite PDF con texto, archivos TXT y Google Docs. El contenido compatible se transfiere a OpenAI para crear el índice de búsqueda; los originales permanecen en Drive.

## Qué puedes hacer

- Consultar manuales, procedimientos, políticas y documentación de un equipo.
- Comparar lo que dicen varios documentos sobre un mismo tema.
- Localizar instrucciones o requisitos y revisar los fragmentos que respaldan la respuesta.
- Detectar cuándo la biblioteca no contiene evidencia suficiente para responder.
- Compartir un punto de consulta web con las personas que tengan la clave de acceso.

El modo documental busca evidencia antes de responder. Si la evidencia o las citas no son suficientes, lo indica. También existe un modo experimental sin búsqueda documental, identificado en la interfaz y sin fuentes verificadas.

## Cómo funciona

1. **Preparas la biblioteca en tu equipo.** Autorizas una carpeta de Drive y sincronizas sus archivos compatibles.
2. **Se crea el índice en OpenAI.** La aplicación identifica archivos nuevos, modificados, renombrados o retirados y conserva el estado de la sincronización.
3. **Publicas una instantánea del índice.** Vercel recibe la configuración necesaria para consultar los documentos ya indexados.
4. **Consultas desde la web.** Introduces la clave de acceso y el servidor devuelve respuestas con enlaces a los originales.

La sincronización de Drive se realiza localmente. El despliegue en Vercel consulta la última instantánea publicada; no abre ventanas de autorización de Google ni sincroniza Drive desde la nube.

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

Abre [http://127.0.0.1:5000](http://127.0.0.1:5000) y pulsa **Sincronizar desde Drive**. La primera vez se abrirá la autorización de Google en tu equipo. El permiso solicitado es de lectura: la aplicación no modifica ni elimina los archivos originales de Drive.

Mantén un solo proceso del servidor local. El puerto predeterminado es `5000` y puede cambiarse con `PORT`.

## Configuración

Las claves se guardan en `.env` para uso local y en las variables de entorno de Vercel para el despliegue. La clave de OpenAI permanece en el servidor y no se entrega al navegador.

| Variable | Para qué sirve |
|---|---|
| `OPENAI_API_KEY` | Autoriza las consultas y la indexación en OpenAI. Es secreta. |
| `OPENAI_MODEL` | Selecciona el modelo compatible con Responses y File Search. |
| `GOOGLE_DRIVE_FOLDER_ID` | Identifica la carpeta autorizada y el alcance del índice. |
| `DEPLOYMENT_MODE` | `local` para sincronizar en tu equipo; `cloud` para consultar desde Vercel. |
| `APP_ACCESS_KEY` | Clave compartida para proteger las consultas en la nube. Es secreta. |
| `DOCUMENT_MANIFEST_JSON` | Instantánea del índice que consulta Vercel. Trátala como información privada. |
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
6. Crea un despliegue, abre la dirección asignada por Vercel e introduce la clave de `access-key.txt`. Comprueba el listado de documentos y una pregunta con evidencia.

`data/`, `.deployment/`, `.env`, `credentials.json` y `token.json` son privados y están excluidos de Git. El modo nube no necesita el cliente ni el token OAuth de Google. No los cargues en Vercel ni en GitHub.

Cuando cambie la biblioteca, sincroniza de nuevo en tu equipo, ejecuta el preparador, actualiza la instantánea de Vercel y crea un nuevo despliegue. Las variables actualizadas se aplican a los nuevos despliegues. Retirar un archivo o cambiar sus permisos en Drive no actualiza por sí solo la instantánea publicada.

Vercel es la opción recomendada porque ejecuta el servidor FastAPI y mantiene la clave de OpenAI en el servidor. GitHub Pages sirve páginas estáticas y necesitaría otro servicio para ejecutar esta API.

## Integrarlo con otra interfaz

La interfaz y la API incluidas se sirven desde el mismo dominio. Estas son las rutas principales:

| Método | Ruta | Uso |
|---|---|---|
| `GET` | `/` | Abre la interfaz. |
| `GET` | `/api/health` | Muestra comprobaciones de configuración; no prueba las APIs externas. |
| `GET` | `/api/documents` | Lista documentos activos y fecha de última sincronización. |
| `POST` | `/api/ask` | Devuelve una respuesta con sus fuentes. |
| `POST` | `/api/sync` | Sincroniza Drive en modo local; no está disponible en la nube. |

En modo nube, el listado y las consultas requieren la cabecera `X-App-Key` con la clave compartida. La interfaz permite introducirla y la mantiene únicamente en memoria. `/api/health` es público y no devuelve los secretos.

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

- Los PDF necesitan texto extraíble. No hay reconocimiento automático de imágenes u OCR.
- Las respuestas dependen de los fragmentos recuperados y no garantizan una revisión completa de la biblioteca. Comprueba las fuentes antes de tomar decisiones.
- Los documentos se consultan según la última sincronización publicada. Los cambios y permisos de Drive requieren actualizar esa versión.
- El modo nube comparte una biblioteca y una clave de acceso. No ofrece cuentas individuales, roles ni permisos por documento.
- Los límites de frecuencia y bloqueos funcionan por proceso; no representan una cuota global de toda la cuenta. Para más usuarios se requiere control compartido de acceso y consumo.
- Las consultas, búsquedas y archivos almacenados en OpenAI pueden generar cargos. Configura presupuestos y revisa el consumo de tu cuenta.
- Conserva un respaldo privado de `data/` y no borres el manifiesto para resolver un fallo: contiene el seguimiento de archivos e indexaciones remotas.

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
- `scripts/prepare_cloud.py`: preparación privada del despliegue.
- `assets/` e `index.html`: interfaz web.
- `tests/`: pruebas del servidor y de la interfaz.
- `data/`: estado local privado, excluido del repositorio.

Referencias: [FastAPI en Vercel](https://vercel.com/docs/frameworks/backend/fastapi), [File Search de OpenAI](https://developers.openai.com/api/docs/guides/tools-file-search) y [Google Drive API](https://developers.google.com/workspace/drive/api/guides/about-sdk).
