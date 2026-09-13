FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONUTF8=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "-X", "utf8", "-m", "uvicorn", "src.http_api:app", "--host", "0.0.0.0", "--port", "8000"]
