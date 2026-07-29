# pathtrace is stdlib-only, so the image is just Python + one script, run as non-root.
FROM python:3.13-slim
RUN useradd -u 10001 -m app
COPY pathtrace.py /app/pathtrace.py
USER 10001
EXPOSE 8080
ENTRYPOINT ["python", "/app/pathtrace.py"]
