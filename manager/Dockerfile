FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi uvicorn docker cryptography httpx
WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
