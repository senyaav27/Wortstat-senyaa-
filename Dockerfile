FROM python:3.12-slim
WORKDIR /app
COPY tvoe_wordstat_bot.py ./tvoe_wordstat_bot.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "tvoe_wordstat_bot.py"]
