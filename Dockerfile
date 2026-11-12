# PS-1 AP Invoice Processor — production image for Render (or any Docker host)
#
# The app is pure Python stdlib + pdfplumber + tesseract/poppler for OCR.
# Render injects PORT; locally it falls back to 8321.

FROM python:3.11-slim

# tesseract-ocr + poppler-utils = the OCR fallback for scanned/image-only PDFs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# runs.db + uploads live under data/ — ephemeral by default on Render's free
# tier; attach a Disk in render.yaml to persist them (optional).
VOLUME ["/app/data"]

EXPOSE 8321
CMD ["python", "-m", "ap.server"]
