FROM node:20-alpine AS dashboard
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY configs ./configs
COPY deploy/render/entrypoint.sh /app/deploy/render/entrypoint.sh
COPY --from=dashboard /ui/dist ./frontend/dist

RUN pip install --no-cache-dir . \
    && chmod +x /app/deploy/render/entrypoint.sh \
    && mkdir -p /app/var \
    && useradd --create-home --uid 1000 --shell /bin/sh ats \
    && chown -R ats:ats /app

USER ats

EXPOSE 8000

# Paper only. Private bind is Render's job; LiveAdapter stays disabled.
ENTRYPOINT ["/app/deploy/render/entrypoint.sh"]
CMD ["paper"]
