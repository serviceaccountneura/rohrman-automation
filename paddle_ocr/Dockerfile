# PaddleOCR recognition service -- TESTING BRANCH, see server.py.
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Versions as tested on this host: paddlepaddle 3.3.1 + paddleocr 3.7.0 on
# Python 3.13, CPU only.
RUN pip install --no-cache-dir paddlepaddle==3.3.1 paddleocr==3.7.0

RUN useradd --create-home app
USER app
WORKDIR /home/app
ENV PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True

# Download the model at build time, so the container starts without the
# internet and the first invoice is not the one that waits for it.
RUN python -c "from paddleocr import TextRecognition; TextRecognition(model_name='PP-OCRv5_server_rec', enable_mkldnn=False)"

COPY server.py .
EXPOSE 8100
CMD ["python", "server.py"]
