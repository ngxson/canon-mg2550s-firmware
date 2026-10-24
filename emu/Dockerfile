FROM python:3.12-slim
COPY requirements.txt /tmp/emu-requirements.txt
RUN pip install --no-cache-dir -r /tmp/emu-requirements.txt
WORKDIR /project
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "-u"]
