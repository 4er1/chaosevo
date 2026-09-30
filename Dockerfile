FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY chaosevo ./chaosevo
RUN pip install --no-cache-dir . \
 && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin chaosevo
USER chaosevo

EXPOSE 9200
ENTRYPOINT ["chaosevo"]
CMD ["--help"]
