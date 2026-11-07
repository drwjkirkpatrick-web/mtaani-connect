# mtaani-connect — production image.
# Pin the build's Python; the dev box runs the project on 3.14.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The GDAL/GEOS system libs feed Django's GeoDjango-dependent bits; the
# planner itself (Prompt 32) is pure Python — these are for safety with
# any postgis client tooling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils libproj-dev gdal-bin curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[prod]" || pip install --no-cache-dir .

COPY . .

EXPOSE 8000
HEALTHCHECK --interval=15s --retries=5 \
  CMD curl -fsS http://localhost:8000/health/ || exit 1

CMD ["gunicorn", "mtaani.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "4"]
