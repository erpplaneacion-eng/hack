FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY web_app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY web_app/ .

RUN mkdir -p jobs

EXPOSE 8000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
