FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /bin/uv

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN DJANGO_SECRET_KEY=build uv run --no-sync python manage.py collectstatic --noinput

RUN useradd --no-create-home app
USER app

EXPOSE 8000
CMD ["/app/.venv/bin/gunicorn", "cloud_optimizer_project.wsgi", "--bind", "0.0.0.0:8000", "--workers", "2"]
