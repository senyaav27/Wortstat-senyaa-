FROM python:3.12-slim
WORKDIR /app
COPY alerts_bot.py ./alerts_bot.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot_fixed.py"]
