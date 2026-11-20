FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    KERMA_DATA_DIR=/data

# Everything is installed at build time: the grading environment has no network access.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN useradd --create-home kerma && mkdir /data && chown kerma:kerma /data

WORKDIR /app
COPY *.py ./

USER kerma
EXPOSE 18018
CMD ["python", "main.py"]
