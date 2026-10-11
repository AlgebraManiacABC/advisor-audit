FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY advisor_audit ./advisor_audit
COPY data ./data
RUN useradd --create-home app
USER app
# One worker on purpose: sessions live in this process's memory.
CMD ["uvicorn", "advisor_audit.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
