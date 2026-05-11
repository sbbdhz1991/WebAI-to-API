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
- Change to `ENVIRONMENT=production` to enable **multi-worker production** mode with detached execution (`make up`).
- `GEMINI_API_KEY`: When non-empty, every request must carry the matching key — see [Authentication](README.md#authentication) for accepted header/query forms. Empty (default) means auth is disabled.

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
- In **production**, the server runs in **detached mode** (`-d`) with multiple workers.

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
├── docker-compose.yml      # Shared config (network, ports, env)
├── .env                    # Defines ENVIRONMENT (development/production)
├── Makefile                # Simplifies Docker CLI usage
```

---

### ✅ Best Practices

- Don't use `ENVIRONMENT=development` in **production**.
- Avoid bind mounts (`volumes`) in production to ensure image consistency.
