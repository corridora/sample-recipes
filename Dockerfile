FROM node:26-bookworm-slim AS frontend
WORKDIR /build
COPY package*.json ./
RUN npm ci
COPY frontend ./frontend
RUN npm run build

FROM python:3.14-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
COPY --from=frontend /build/frontend/dist ./frontend/dist
RUN useradd --system --uid 10001 sample && mkdir /app/.sample && chown sample /app/.sample
USER 10001
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
