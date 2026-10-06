# syntax=docker/dockerfile:1.6
FROM python:3.12-slim AS base
WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir .

# ---------- Development (debug build) ----------
FROM base AS dev
RUN pip install --no-cache-dir debugpy

EXPOSE 3000 5678
CMD ["uvicorn", "retrieval_api.main:app", \
     "--host", "0.0.0.0", "--port", "3000", "--reload"]
