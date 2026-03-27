#!/bin/bash

if [ -z "$1" ]; then
  echo "Error: No model specified."
  echo "Usage: ./start_llm.sh [qwen | gemma | llama]"
  exit 1
fi

MODEL_CHOICE=$1

case $MODEL_CHOICE in
  qwen)
    PROFILE="qwen"
    CONTAINER="llm_qwen"
    OLLAMA_MODEL="qwen2.5-coder:7b"
    ;;
  gemma)
    PROFILE="gemma"
    CONTAINER="llm_gemma"
    OLLAMA_MODEL="gemma2:27b"
    ;;
  llama)
    PROFILE="llama"
    CONTAINER="llm_llama"
    OLLAMA_MODEL="llama3.1:8b"
    ;;
  *)
    echo "Error: Unknown model '$MODEL_CHOICE'."
    echo "Available options: qwen, gemma, llama"
    exit 1
    ;;
esac

echo "Stopping any running models..."
docker compose down

docker stop llm_qwen llm_gemma llm_llama 2>/dev/null || true
docker rm llm_qwen llm_gemma llm_llama 2>/dev/null || true

sleep 2

echo "Starting environment for: $MODEL_CHOICE..."
docker compose --profile $PROFILE up -d

echo "Waiting for Ollama server to be ready..."
until curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; do
  sleep 1
done
echo "Ollama server is ready."

echo "Connecting to model $OLLAMA_MODEL..."
echo "Type '/bye' to exit the chat."
echo "---------------------------------------------------"

docker exec -it $CONTAINER ollama run $OLLAMA_MODEL
