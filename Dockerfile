FROM python:3.11-slim

# Install Requirements
WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y gcc python3-dev libffi-dev libssl-dev  && rm -rf /var/lib/apt/lists/*
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Default Port 
EXPOSE 6969

# Run Uvicorn server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "6969"]
