FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Copy only server modules; clients, credentials and local databases stay outside.
COPY server.py server_network.py server_ai.py database.py ./
COPY common/__init__.py common/protocol.py ./common/

# An empty named volume inherits this writable directory for the non-root server.
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001

EXPOSE 5000/tcp
# Match the existing KeyboardInterrupt path so docker stop closes threads/SQLite.
STOPSIGNAL SIGINT
ENTRYPOINT ["python", "server.py"]
CMD ["--host", "0.0.0.0", "--port", "5000", "--database", "/data/chat.sqlite3"]
