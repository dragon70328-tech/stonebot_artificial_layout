FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONUTF8=1

WORKDIR /app

# 用实测版本快照构建，保证同一份代码在不同时间构建出同一个镜像
# （requirements.txt 仍是允许区间的人工维护来源，升级依赖要显式更新 lock）。
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY . .

EXPOSE 8000

CMD ["python", "-X", "utf8", "-m", "uvicorn", "src.http_api:app", "--host", "0.0.0.0", "--port", "8000"]
