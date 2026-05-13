## 🐳 Docker Deployment Guide

### Prerequisites

Ensure you have the following installed on your system:

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose v2.24+](https://docs.docker.com/compose/)
- GNU Make (optional but recommended)

---

### 🛠️ Docker Environment Configuration

This project uses a `.env` file for environment-specific settings like development or production mode on docker.

#### Example `.env`

```env
# Set the environment mode
ENVIRONMENT=development

# Optional API key for protecting the WebAI-to-API endpoints.
# Leave empty to disable authentication (all endpoints open).
GEMINI_API_KEY=
```

- `ENVIRONMENT=development`: Runs the server in **development** mode with auto-reload and debug logs.
- Change to `ENVIRONMENT=production` to run in detached mode (`make up -d`).
- `GEMINI_API_KEY`: When non-empty, every request must carry the matching key — see [Authentication](README.md#authentication) for accepted header/query forms. Empty (default) means auth is disabled.

> The server runs with `--workers 1` on purpose. Gemini sessions are stateful and each worker maintains its own cookie-rotation loop; running multiple workers causes inconsistent `__Secure-1PSIDTS` state and risks Google rate-limiting. Concurrency is handled by FastAPI's async stack within the single worker.

> **Tip:** If `ENVIRONMENT` is not set, the default is automatically assumed to be `development`.

#### Setting the API key for Docker

`docker-compose.yml` already wires `GEMINI_API_KEY` through. You have two ways to set it:

```bash
# Option 1 — edit .env (persistent across reboots)
echo 'GEMINI_API_KEY=your-secret-key' >> .env
make up

# Option 2 — override from the shell at launch (no file change)
GEMINI_API_KEY=your-secret-key make up
# or, without Make
GEMINI_API_KEY=your-secret-key docker-compose up -d
```

Verify it took effect inside the running container:

```bash
docker exec web_ai_server printenv GEMINI_API_KEY
```

> Shell-supplied values **override** the value in `.env`. Leave the key empty in `.env` if you only want to set it via the shell.

---

### 🚀 Build & Run

> Use `make` commands for simplified usage.

#### 🔧 Build the Docker image

```bash
make build         # Regular build
make build-fresh   # Force clean build (no cache)
```

#### ▶️ Run the server

```bash
make up
```

Depending on the environment:

- In **development**, the server runs in the foreground with hot-reloading.
- In **production**, the server runs in **detached mode** (`-d`) with a single worker (see note above).

> **Before first launch**, create the persistent cookie-cache directory so the rotated `__Secure-1PSIDTS` survives container restarts:
>
> ```bash
> mkdir -p ./data/gemini_cache
> ```
>
> `docker-compose.yml` mounts `./data` into the container at `/app/data` and sets `GEMINI_COOKIE_PATH=/app/data/gemini_cache`. Without this directory, every restart falls back to the (often-expired) `__Secure-1PSIDTS` in `config.conf` and produces `AuthError`. See the [Cookie rotation](README.md#cookie-rotation) section for the full picture.
>
> `config.conf` is also bind-mounted from the host, so you can edit cookies without rebuilding the image.

#### ⏹ Stop the server

```bash
make stop
```

---

### 🧠 Development Notes

- **Reloading**: In development, the server uses `uvicorn --reload` for live updates.
- **Logging**: On container start, it prints the current environment with colors (🟡 dev / ⚪ production).
- **Watch Mode (optional)**: Docker Compose v2.24+ supports file watching via the `compose watch` feature. If enabled, press `w` to toggle.

---

### 📦 File Structure for Docker

Key files:

```plaintext
.
├── Dockerfile              # Base image and command logic
├── docker-compose.yml      # Shared config (network, ports, env, volumes)
├── .env                    # Defines ENVIRONMENT (development/production)
├── Makefile                # Simplifies Docker CLI usage
├── config.conf             # Bind-mounted into the container (cookies, proxy, etc.)
└── data/gemini_cache/      # Bind-mounted; holds the rotated 1PSIDTS cache
```

---

### ✅ Best Practices

- Don't use `ENVIRONMENT=development` in **production**.
- Keep the `./data` and `./config.conf` bind mounts — they are required for cookie persistence across restarts. Image consistency is preserved because application code is still baked into the image; only state (cookies/cache) is mounted.
- Stick to `--workers 1`. See the note in [Environment Configuration](#%EF%B8%8F-docker-environment-configuration).
