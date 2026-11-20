FROM python:3.12-slim
WORKDIR /app
COPY ui2027_bot.py ./ui2027_bot.py
COPY campaigns_wordstat.py ./campaigns_wordstat.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "ui2027_bot.py"]
