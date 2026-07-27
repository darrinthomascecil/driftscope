FROM python:3.8-slim AS builder
RUN pip install --no-cache-dir build

FROM python:3.8-slim
COPY --from=builder /app /app
CMD ["python", "-m", "app"]
