FROM python:3.12-slim
WORKDIR /app
COPY dashboard_bot.py ./dashboard_bot.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot_fixed.py"]
