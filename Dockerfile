FROM python:3.12-slim
WORKDIR /app
COPY ui2027_bot.py ./ui2027_bot.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "ui2027_bot.py"]
