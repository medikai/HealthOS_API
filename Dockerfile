FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /code

# Install dependencies separately so Docker can reuse this layer when the
# application source changes but the dependency list does not.
COPY src/requirements.txt /code/requirements.txt
RUN pip install --upgrade -r /code/requirements.txt

COPY src/app /code/app
# Keep the existing Alembic migration architecture available in the image.
# Migrations are applied separately and are not run during container startup.
COPY src/alembic.ini /code/alembic.ini
COPY src/migrations /code/migrations

# Run as an unprivileged user in the application container.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /code
USER appuser

EXPOSE 8000

CMD ["/bin/sh", "-c", "exec gunicorn app.main:app --workers 4 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:${PORT:-8080}"]
