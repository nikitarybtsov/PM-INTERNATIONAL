FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Зависимости отдельным слоем — быстрее пересборка
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Каталоги для БД и экспортов
RUN mkdir -p /app/data /app/exports && \
    useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"

# Никаких секретов в образе: ключи передаются только через переменные окружения.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
