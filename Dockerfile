FROM python:3.8-slim

# 1) Update apt-get metadata
# 2) Install ffmpeg
# 3) Clean up apt-cache to keep image size smaller
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Copy your files into the container
COPY . .

# Install Python dependencies
RUN python -m pip install --no-cache-dir -r requirements.txt

CMD ["python", "youtubebot.py"]