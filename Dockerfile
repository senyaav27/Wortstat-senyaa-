FROM python:3.12-slim
WORKDIR /app
COPY bot_fixed.py ./bot_fixed.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot_fixed.py"]
