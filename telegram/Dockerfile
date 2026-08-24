# job4meNow telegram bot container
# Base: official Node LTS-track Alpine image. No Playwright/browser dependency here
# (unlike career-ops) — this process only talks to the Telegram Bot API, Aurora's
# Data API (HTTPS), and S3, so a slim image is enough.

FROM node:22-alpine

ENV NODE_ENV=production

# tini is PID 1 so SIGTERM/SIGINT reach the Node process and get reaped correctly —
# without it, `docker stop` on a long-poll loop can hang until the kill timeout.
RUN apk add --no-cache tini

WORKDIR /app

# Prime npm deps in their own layer so rebuilds after a source-only change stay fast.
COPY package.json package-lock.json* ./
RUN npm install --omit=dev --no-audit --no-fund

COPY src ./src

# Artifact cache lives here — docker-compose mounts a named volume over it so the
# LRU cache survives container recreation (rebuilding the image should not cost
# every cached CV/JD re-download from S3).
RUN mkdir -p /app/.cache

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "src/bot.mjs"]
