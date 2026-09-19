FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Sin root. Solo lee correo y calendario: no necesita privilegios y no los tendra.
RUN useradd --system --uid 10003 --no-create-home mcp \
    && chown -R mcp:mcp /app
USER mcp

EXPOSE 8000

CMD ["python", "-m", "app.server"]
