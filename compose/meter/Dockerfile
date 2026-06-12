FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# config/ and .auth/ are bind-mounted at runtime so credential or resident
# changes don't require a rebuild
COPY src/ src/

COPY ./compose/meter/start /start
RUN chmod +x /start

CMD ["/start"]
