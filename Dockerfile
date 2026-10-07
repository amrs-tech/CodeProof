FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY codeproof ./codeproof
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home codeproof \
    && mkdir /app/data && chown codeproof:codeproof /app/data
USER codeproof
EXPOSE 8000
CMD ["uvicorn", "codeproof.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
