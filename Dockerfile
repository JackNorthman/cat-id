FROM python:3.12-slim
RUN pip install --no-cache-dir numpy==2.1.3 paho-mqtt==2.1.0
WORKDIR /app
COPY app/main.py /app/main.py
ENTRYPOINT ["python3","-u","/app/main.py"]
