## Disclaimer

> **This project is intended for research and educational purposes only.**  
> Please refrain from any commercial use and act responsibly when deploying or modifying this tool.

---

# WebAI-to-API

<p align="center">
  <img src="./assets/Server-Run-WebAI.png" alt="WebAI-to-API Server" height="160" />
  <img src="./assets/Server-Run-G4F.png" alt="gpt4free Server" height="160" />
</p>

**WebAI-to-API** is a modular web server built with FastAPI that allows you to expose your preferred browser-based LLM (such as Gemini) as a local API endpoint.

---

This project supports **two operational modes**:

1. **Primary Web Server**

   > WebAI-to-API

   Connects to the Gemini web interface using your browser cookies and exposes it as an API endpoint. This method is lightweight, fast, and efficient for personal use.

2. **Fallback Web Server (gpt4free)**

   > [gpt4free](https://github.com/xtekky/gpt4free)

   A secondary server powered by the `gpt4free` library, offering broader access to multiple LLMs beyond Gemini, including:

   - ChatGPT
   - Claude
   - DeepSeek
   - Copilot
   - HuggingFace Inference
   - Grok
   - ...and many more.

This design provides both **speed and redundancy**, ensuring flexibility depending on your use case and available resources.

---

## Features

- 🌐 **Available Endpoints**:

  - **WebAI Server**:

    - `/v1/chat/completions`
    - `/gemini`
    - `/gemini-chat`
    - `/translate`
    - `/v1beta/models/{model}` (Google Generative AI v1beta API)

  - **gpt4free Server**:
    - `/v1`
    - `/v1/chat/completions`

- 🔄 **Server Switching**: Easily switch between servers in terminal.

- 📎 **File Uploads**: Send images, PDFs, video, and audio to any WebAI endpoint via `multipart/form-data`, JSON-embedded base64, or OpenAI-style multimodal `image_url` parts. See [File Uploads](#file-uploads).

- 🛠️ **Modular Architecture**: Organized into clearly defined modules for API routes, services, configurations, and utilities, making development and maintenance straightforward.

<p align="center">
  <img src="./assets/Endpoints-Docs.png" alt="Endpoints" height="280" />
</p>

---

## Installation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/Amm1rr/WebAI-to-API.git
   cd WebAI-to-API
   ```

2. **Install dependencies using Poetry:**

   ```bash
   poetry install
   ```

3. **Create and update the configuration file:**

   ```bash
   cp config.conf.example config.conf
   ```

   Then, edit `config.conf` to adjust service settings and other options.

4. **Run the server:**

   ```bash
   poetry run python src/run.py
   ```

---

## Authentication

The WebAI-to-API endpoints support optional API key authentication, enabled by setting the `GEMINI_API_KEY` environment variable.

- **Disabled (default):** If `GEMINI_API_KEY` is unset or empty, all endpoints are open — same as before. Convenient for local use.
- **Enabled:** If `GEMINI_API_KEY` is set, every request to `/gemini`, `/gemini-chat`, `/translate`, `/v1/*`, and `/v1beta/models/*` must present a matching key, otherwise the server returns `401 Unauthorized`.

### Setting the key

```bash
# Linux / macOS
export GEMINI_API_KEY="your-secret-key"
poetry run python src/run.py
```

```powershell
# Windows PowerShell
$env:GEMINI_API_KEY = "your-secret-key"
poetry run python src/run.py
```

```bash
# Docker
docker run -e GEMINI_API_KEY="your-secret-key" -p 6969:6969 webai-to-api
```

### Passing the key from clients

Any one of the following is accepted — pick whichever matches your client:

| Header / Param                 | Style                  | Typical client                       |
| ------------------------------ | ---------------------- | ------------------------------------ |
| `Authorization: Bearer <key>`  | OpenAI-compatible      | OpenAI SDKs, `/v1/chat/completions`  |
| `x-goog-api-key: <key>`        | Google Generative AI   | `@google/generative-ai`, `/v1beta/*` |
| `x-api-key: <key>`             | Generic                | curl, custom clients                 |
| `?key=<key>` query parameter   | Google query-param     | Browser / quick tests                |

### Examples

```bash
# OpenAI-compatible
curl http://localhost:6969/v1/chat/completions \
  -H "Authorization: Bearer your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.0-flash","messages":[{"role":"user","content":"Hello"}]}'

# Google Generative AI compatible
curl "http://localhost:6969/v1beta/models/gemini-3.0-flash:generateContent" \
  -H "x-goog-api-key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"parts":[{"text":"Hello"}]}]}'

# Query-param style
curl "http://localhost:6969/v1/models?key=your-secret-key"
```

> Note: This guards only the WebAI-to-API server. The fallback gpt4free server is launched as a separate process and is not covered by `GEMINI_API_KEY`.

---

## File Uploads

`/gemini`, `/gemini-chat`, `/translate`, and `/v1/chat/completions` all accept file attachments — images, PDFs, video, and audio — in three interchangeable forms. Pick whichever fits your client.

### Prerequisites: full browser cookies

File attachments require **more cookies than the two `__Secure-1PSID*` cookies** used for plain text chat. Google's media upload endpoint demands the SAPISID family (`SAPISID`, `__Secure-1PAPISID`, `__Secure-3PAPISID`, `SID`, `HSID`, `SSID`, `APISID`, and a handful of others). Without these, uploads silently fail with `APIError 1099` or hang until watchdog timeout.

To make uploads work:

1. Sign in at https://gemini.google.com in a regular browser using the same Google account whose cookies you've put in `gemini_cookie_1psid` / `gemini_cookie_1psidts`.
2. Open DevTools → Network → make one chat message (no attachment needed).
3. Pick any request to `gemini.google.com/_/BardChatUi/...` → right-click → Copy → Copy as cURL (bash).
4. Find the `-H 'Cookie: <long string>'` chunk. Copy the **entire string after `Cookie: `** (everything between the single quotes).
5. Paste it as a one-line value into `config.conf`:

```ini
[Cookies]
gemini_cookie_1psid = <kept as before>
gemini_cookie_1psidts = <kept as before>
gemini_cookie_extra = SAPISID=...; __Secure-1PAPISID=...; SID=...; HSID=...; SSID=...; APISID=...; __Secure-3PAPISID=...; <…>
```

6. Restart the server (`docker compose restart` if `config.conf` is volume-mounted, otherwise rebuild). Startup log should show:

```
Injected N extra cookies into Gemini session.
```

with `N ≥ 20`.

> ⚠️ **The pasted string is account-password equivalent.** Treat `config.conf` like a secret: `chmod 600`, add to `.gitignore`, never commit/share. Cookies typically last days to weeks before requiring refresh.

### 1. `multipart/form-data` (recommended for browsers and curl)

Plain form fields plus one or more `files` parts. Filename is preserved, so MIME is inferred correctly.

```bash
# Image
curl -F message="What's in this image?" -F model=gemini-3-flash \
     -F files=@photo.jpg \
     http://localhost:6969/gemini

# Multiple files (image + PDF)
curl -F message="Compare these two documents" -F model=gemini-3-pro \
     -F files=@report.pdf -F files=@chart.png \
     http://localhost:6969/gemini-chat

# Video
curl -F message="Summarize this clip" -F model=gemini-3-pro \
     -F files=@demo.mp4 \
     http://localhost:6969/gemini
```

### 2. JSON with base64-embedded files

Each entry in `files` may be a server-side path string (legacy behavior) **or** an object carrying base64-encoded content. Always supply `filename` for byte payloads so the MIME type is detected.

```json
POST /gemini
Content-Type: application/json

{
  "message": "Describe this image",
  "model": "gemini-3-flash",
  "files": [
    {
      "filename": "photo.jpg",
      "content_base64": "/9j/4AAQSkZJRgABAQEASABIAAD..."
    },
    "/absolute/path/already/on/server.pdf"
  ]
}
```

`content_base64` also accepts a full data URL (e.g. `"data:image/png;base64,iVBORw0KGgo..."`).

### 3. OpenAI-compatible multimodal (`/v1/chat/completions` only)

The standard OpenAI vision format works as-is — `image_url` accepts both `data:` URLs and `http(s)://` URLs (server fetches them).

```json
POST /v1/chat/completions
Content-Type: application/json

{
  "model": "gemini-3-pro",
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "What's in this picture?" },
        { "type": "image_url", "image_url": { "url": "data:image/png;base64,iVBORw0..." } }
      ]
    }
  ]
}
```

`/v1/chat/completions` additionally supports a `multipart/form-data` variant where the OpenAI payload is sent as a JSON string in a `payload` field alongside one or more `files` uploads:

```bash
curl http://localhost:6969/v1/chat/completions \
  -F 'payload={"model":"gemini-3-flash","messages":[{"role":"user","content":"Describe"}]};type=application/json' \
  -F files=@photo.jpg
```

### Size limit

Total upload size per request is capped by `[Server] max_upload_size_mb` (default **100 MB**, set to `0` to disable). Requests exceeding the limit are rejected with `413 Payload Too Large`. If you front the server with nginx or another proxy, raise its body-size limit accordingly (`client_max_body_size` for nginx).

### Known limitations & troubleshooting

File upload support is built on top of patches that reverse-engineer Google's current Gemini web protocol (see `src/app/services/gemini_patch.py`). The protocol is unofficial and Google changes it without notice, so this layer is inherently fragile.

| Symptom | Likely cause | Fix |
|---|---|---|
| Hangs ~2 minutes, log shows `Watchdog … Stream suspended` | Cookies are short / SAPISID family missing | Re-paste full browser `Cookie:` header into `gemini_cookie_extra` |
| `APIError 1099` | Upload protocol mismatch | Verify patches are loaded (look for `[patch] gemini_webapi.upload_file -> resumable …` at startup); rebuild image if missing |
| `INVALID_ARGUMENT 400` from `er` frame | Body or header layout mismatch (likely Google changed protocol) | Recapture a working browser request via Fiddler / DevTools HAR and re-diff against `gemini_patch.py` |
| Model replies "I can't see the video/image" | Cookies are valid but account / region lacks media support | Verify upload works in the browser first; if browser also can't see attachments, the account tier is the limit |
| `503 Gemini cookies not found` | `config.conf` is missing or empty | Refill cookies and ensure the file is reachable inside the container (mount it as a volume if you don't want to rebuild every time) |

**Diagnostic mode**: set environment variable `WEBAI_DEBUG_DUMP_REQUEST=1` to log the full outgoing StreamGenerate request (URL + headers + body, **including live cookies**) on every attachment call. Use only for one-off debugging — leaving it on writes account-password-equivalent data into the log.

**Cookie rotation**: when Google rotates `__Secure-1PSIDTS`, the library auto-saves the new value back into `config.conf`. But the other cookies in `gemini_cookie_extra` are not refreshed automatically. If uploads start failing weeks after setup, refresh `gemini_cookie_extra` from the browser.

**Long video / long response timeout**: gemini-webapi's stream watchdog defaults to 120 seconds. For large videos (tens of MB) where Google's response can take longer to stream out, set `WEBAI_WATCHDOG_TIMEOUT` to override:

```yaml
# docker-compose.yml
services:
  webai:
    environment:
      - WEBAI_WATCHDOG_TIMEOUT=600   # 10 minutes; recommended when handling video
```

Or in `.env`:

```
WEBAI_WATCHDOG_TIMEOUT=600
```

Symptoms that mean you need this: ``[Watchdog] Connection idle for 120s … Stream suspended`` with a partial ``parse_response_by_frame: Incomplete frame …`` log line right before. Recommended values: ``120`` (default, text-only), ``240`` (occasional images), ``600`` (video work), ``900`` (very large videos with detailed analyses).

---

## Usage

Send a POST request to `/v1/chat/completions` (or any other available endpoint) with the required payload.

### Supported Models

| Model                       | Description                        |
| --------------------------- | ---------------------------------- |
| `gemini-3.0-pro`            | Most powerful model                |
| `gemini-3.0-flash`          | Fast and efficient model (default) |
| `gemini-3.0-flash-thinking` | Enhanced reasoning model           |

### Example Request (Basic)

```json
{
  "model": "gemini-3.0-pro",
  "messages": [{ "role": "user", "content": "Hello!" }]
}
```

### Example Request (With System Prompt & Conversation History)

```json
{
  "model": "gemini-3.0-flash-thinking",
  "messages": [
    { "role": "system", "content": "You are a helpful assistant." },
    { "role": "user", "content": "What is Python?" },
    { "role": "assistant", "content": "Python is a programming language." },
    { "role": "user", "content": "Is it easy to learn?" }
  ]
}
```

### Example Response

```json
{
  "id": "chatcmpl-12345",
  "object": "chat.completion",
  "created": 1693417200,
  "model": "gemini-3.0-pro",
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "Hi there!"
      },
      "finish_reason": "stop",
      "index": 0
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

---

## Documentation

### WebAI-to-API Endpoints

> `POST /gemini`

Initiates a new conversation with the LLM. Each request creates a **fresh session**, making it suitable for stateless interactions.

> `POST /gemini-chat`

Continues a persistent conversation with the LLM without starting a new session. Ideal for use cases that require context retention between messages.

> `POST /translate`

Designed for quick integration with the [Translate It!](https://github.com/iSegaro/Translate-It) browser extension.
Functionally identical to `/gemini-chat`, meaning it **maintains session context** across requests.

> `POST /v1/chat/completions`

**OpenAI-compatible endpoint** with full support for:
- **System prompts**: Set behavior and context for the assistant
- **Conversation history**: Maintain context across multiple turns (user/assistant messages)
- **Streaming**: Optional streaming response support

Built for seamless integration with clients that expect the OpenAI API format.

> `POST /v1beta/models/{model}`

**Google Generative AI v1beta API** compatible endpoint.
Provides access to the latest Google Generative AI models with standard Google API format including safety ratings and structured responses.

---

### gpt4free Endpoints

These endpoints follow the **OpenAI-compatible structure** and are powered by the `gpt4free` library.  
For detailed usage and advanced customization, refer to the official documentation:

- 📄 [Provider Documentation](https://github.com/gpt4free/g4f.dev/blob/main/docs/selecting_a_provider.md)
- 📄 [Model Documentation](https://github.com/gpt4free/g4f.dev/blob/main/docs/providers-and-models.md)

#### Available Endpoints (gpt4free API Layer)

```
GET  /                              # Health check
GET  /v1                            # Version info
GET  /v1/models                     # List all available models
GET  /api/{provider}/models         # List models from a specific provider
GET  /v1/models/{model_name}        # Get details of a specific model

POST /v1/chat/completions           # Chat with default configuration
POST /api/{provider}/chat/completions
POST /api/{provider}/{conversation_id}/chat/completions

POST /v1/responses                  # General response endpoint
POST /api/{provider}/responses

POST /api/{provider}/images/generations
POST /v1/images/generations
POST /v1/images/generate            # Generate images using selected provider

POST /v1/media/generate             # Media generation (audio/video/etc.)

GET  /v1/providers                  # List all providers
GET  /v1/providers/{provider}       # Get specific provider info

POST /api/{path_provider}/audio/transcriptions
POST /v1/audio/transcriptions       # Audio-to-text

POST /api/markitdown                # Markdown rendering

POST /api/{path_provider}/audio/speech
POST /v1/audio/speech               # Text-to-speech

POST /v1/upload_cookies             # Upload session cookies (browser-based auth)

GET  /v1/files/{bucket_id}          # Get uploaded file from bucket
POST /v1/files/{bucket_id}          # Upload file to bucket

GET  /v1/synthesize/{provider}      # Audio synthesis

POST /json/{filename}               # Submit structured JSON data

GET  /media/{filename}              # Retrieve media
GET  /images/{filename}             # Retrieve images
```

---

## Roadmap

- ✅ Maintenance

---

<details>
  <summary>
    <h2>Configuration ⚙️</h2>
  </summary>

### Key Configuration Options

| Section     | Option                | Description                                                | Example Value           |
| ----------- | --------------------- | ---------------------------------------------------------- | ----------------------- |
| [AI]        | default_ai            | Default service for `/v1/chat/completions`                 | `gemini`                |
| [Browser]   | name                  | Browser for cookie-based authentication                    | `firefox`               |
| [EnabledAI] | gemini                | Enable/disable Gemini service                              | `true`                  |
| [Proxy]     | http_proxy            | Proxy for Gemini connections (optional)                    | `http://127.0.0.1:2334` |
| [Server]    | max_upload_size_mb    | Max total upload size per request in MB (`0` to disable)   | `100`                   |

The complete configuration template is available in [`WebAI-to-API/config.conf.example`](WebAI-to-API/config.conf.example).  
If the cookies are left empty, the application will automatically retrieve them using the default browser specified.

---

### Sample `config.conf`

```ini
[AI]
# Default AI service.
default_ai = gemini

# Default model for Gemini (options: gemini-3.0-pro, gemini-3.0-flash, gemini-3.0-flash-thinking)
default_model_gemini = gemini-3.0-flash

# Gemini cookies (leave empty to use browser_cookies3 for automatic authentication).
gemini_cookie_1psid =
gemini_cookie_1psidts =

[EnabledAI]
# Enable or disable AI services.
gemini = true

[Browser]
# Default browser options: firefox, brave, chrome, edge, safari.
name = firefox

# --- Proxy Configuration ---
# Optional proxy for connecting to Gemini servers.
# Useful for fixing 403 errors or restricted connections.
[Proxy]
http_proxy =

# --- Server Settings ---
# Max total size (MB) of file uploads per request.
# Applies to multipart, base64-embedded files, and fetched image_url payloads.
# Set to 0 to disable the check.
[Server]
max_upload_size_mb = 100
```

</details>

---

## Project Structure

The project now follows a modular layout that separates configuration, business logic, API endpoints, and utilities:

```plaintext
src/
├── app/
│   ├── __init__.py
│   ├── main.py                # FastAPI app creation, configuration, and lifespan management.
│   ├── config.py              # Global configuration loader/updater.
│   ├── logger.py              # Centralized logging configuration.
│   ├── endpoints/             # API endpoint routers.
│   │   ├── __init__.py
│   │   ├── gemini.py          # Endpoints for Gemini (e.g., /gemini, /gemini-chat).
│   │   ├── chat.py            # Endpoints for translation and OpenAI-compatible requests.
│   │   └── google_generative.py  # Google Generative AI v1beta API endpoints.
│   ├── services/              # Business logic and service wrappers.
│   │   ├── __init__.py
│   │   ├── gemini_client.py   # Gemini client initialization, content generation, and cleanup.
│   │   └── session_manager.py # Session management for chat and translation.
│   └── utils/                 # Helper functions.
│       ├── __init__.py
│       └── browser.py         # Browser-based cookie retrieval.
├── models/                    # Models and wrappers (e.g., MyGeminiClient).
│   └── gemini.py
├── schemas/                   # Pydantic schemas for request/response validation.
│   └── request.py
├── config.conf                # Application configuration file.
└── run.py                     # Entry point to run the server.
```

---

## Developer Documentation

### Overview

The project is built on a modular architecture designed for scalability and ease of maintenance. Its primary components are:

- **app/main.py:** Initializes the FastAPI application, configures middleware, and manages application lifespan (startup and shutdown routines).
- **app/config.py:** Handles the loading and updating of configuration settings from `config.conf`.
- **app/logger.py:** Sets up a centralized logging system.
- **app/endpoints/:** Contains separate modules for handling API endpoints. Each module (e.g., `gemini.py` and `chat.py`) manages routes specific to their functionality.
- **app/services/:** Encapsulates business logic, including the Gemini client wrapper (`gemini_client.py`) and session management (`session_manager.py`).
- **app/utils/browser.py:** Provides helper functions, such as retrieving cookies from the browser for authentication.
- **models/:** Holds model definitions like `MyGeminiClient` for interfacing with the Gemini Web API.
- **schemas/:** Defines Pydantic models for validating API requests.

### How It Works

1. **Application Initialization:**  
   On startup, the application loads configurations and initializes the Gemini client and session managers. This is managed via the `lifespan` context in `app/main.py`.

2. **Routing:**  
   The API endpoints are organized into dedicated routers under `app/endpoints/`, which are then included in the main FastAPI application.

3. **Service Layer:**  
   The `app/services/` directory contains the logic for interacting with the Gemini API and managing user sessions, ensuring that the API routes remain clean and focused on request handling.

4. **Utilities and Configurations:**  
   Helper functions and configuration logic are kept separate to maintain clarity and ease of updates.

---

## 🐳 Docker Deployment Guide

For Docker setup and deployment instructions, please refer to the [Docker.md](Docker.md) documentation.

To enable API-key authentication in Docker, set `GEMINI_API_KEY` in `.env` (or pass it via the shell at launch):

```bash
# Persistent — write to .env, then start
echo 'GEMINI_API_KEY=your-secret-key' >> .env
make up

# Or one-shot — override from the shell (takes precedence over .env)
GEMINI_API_KEY=your-secret-key docker-compose up -d
```

Leave `GEMINI_API_KEY` empty (or unset) to keep authentication disabled. See [Authentication](#authentication) for the accepted header/query forms.

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Amm1rr/WebAI-to-API&type=Date)](https://www.star-history.com/#Amm1rr/WebAI-to-API&Date)

## License 📜

This project is open source under the [MIT License](LICENSE).

---

> **Note:** This is a research project. Please use it responsibly, and be aware that additional security measures and error handling are necessary for production deployments.

<br>

[![](https://visitcount.itsvg.in/api?id=amm1rr&label=V&color=0&icon=2&pretty=true)](https://github.com/Amm1rr/)
