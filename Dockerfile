# The Alert Bot API (with its worker inside) for hosts like Render. Unlike Render's plain Python runtime, this image
# has Microsoft's SQL Server ODBC Driver 18, so databases on SQL Server / Azure SQL can be connected.
FROM python:3.12-slim-bookworm

# Microsoft ODBC Driver 18 for SQL Server (Debian 12), as Microsoft documents it
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg ca-certificates unixodbc \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=python3.12

WORKDIR /app
# dependencies first, so a code change doesn't reinstall them
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --all-extras --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --all-extras --no-dev

# the hosted settings: no pre-set projects, worker inside the server (see deploy/render.env)
ENV ALERTBOT_ENV_FILE=deploy/render.env PATH="/app/.venv/bin:$PATH"
EXPOSE 10000
CMD ["sh", "-c", "uvicorn alertbot.api.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
