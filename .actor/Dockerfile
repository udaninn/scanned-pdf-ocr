FROM apify/actor-python:3.12

# Tesseract does the reading, poppler turns PDF pages into images.
# Both are apt packages, not Python ones, so they go in before pip.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr \
      tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-spa \
      tesseract-ocr-ita tesseract-ocr-por tesseract-ocr-nld \
      tesseract-ocr-kor tesseract-ocr-jpn tesseract-ocr-chi-sim \
      poppler-utils \
 && rm -rf /var/lib/apt/lists/* \
 && tesseract --version

COPY requirements.txt ./

RUN echo "Python version:" \
 && python --version \
 && echo "Installing dependencies:" \
 && pip install --no-cache-dir -r requirements.txt \
 && echo "All installed Python packages:" \
 && pip freeze

COPY . ./

RUN python -c "import src.main" \
 && echo "Compilation check passed."

CMD ["python3", "-m", "src"]
