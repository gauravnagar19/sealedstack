FROM python:3.12-alpine

WORKDIR /app
COPY app/ /app/

# Run as an unprivileged user; the app needs no write access.
RUN adduser -D -u 10001 app
USER app

ENV PORT=3000
EXPOSE 3000
CMD ["python", "-u", "server.py"]
