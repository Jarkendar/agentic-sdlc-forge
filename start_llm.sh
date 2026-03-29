#!/bin/bash

# --- Defaults ---
GUI=false
MODEL_CHOICE=""

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
  case $1 in
    --gui|-g)
      GUI=true
      shift
      ;;
    -*)
      echo "Error: Unknown option '$1'."
      echo "Usage: ./start_llm.sh [--gui|-g] [qwen | gemma | llama]"
      exit 1
      ;;
    *)
      MODEL_CHOICE=$1
      shift
      ;;
  esac
done

if [ -z "$MODEL_CHOICE" ]; then
  echo "Error: No model specified."
  echo "Usage: ./start_llm.sh [--gui|-g] [qwen | gemma | llama]"
  echo ""
  echo "Options:"
  echo "  --gui, -g    Launch Open WebUI in browser (http://localhost:3000)"
  exit 1
fi

case $MODEL_CHOICE in
  qwen)
    PROFILE="qwen"
    CONTAINER="llm_qwen"
    OLLAMA_MODEL="qwen2.5-coder:7b"
    ;;
  gemma)
    PROFILE="gemma"
    CONTAINER="llm_gemma"
    OLLAMA_MODEL="gemma3:27b"
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
docker compose down --remove-orphans

docker stop llm_qwen llm_gemma llm_llama llm_webui 2>/dev/null || true
docker rm llm_qwen llm_gemma llm_llama llm_webui 2>/dev/null || true

sleep 2

# --- Build profile list ---
PROFILES="--profile $PROFILE"
if [ "$GUI" = true ]; then
  PROFILES="$PROFILES --profile gui"
fi

echo "Starting environment for: $MODEL_CHOICE..."
docker compose $PROFILES up -d

echo "Waiting for Ollama server to be ready..."
until curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; do
  sleep 1
done
echo "Ollama server is ready."

# --- Pull the model (ensure it's available) ---
echo "Pulling model $OLLAMA_MODEL (if not already downloaded)..."
docker exec $CONTAINER ollama pull $OLLAMA_MODEL

if [ "$GUI" = true ]; then
  WEBUI_URL="http://localhost:3000"

  echo "Waiting for Open WebUI to be ready..."
  until curl -sf "$WEBUI_URL" > /dev/null 2>&1; do
    sleep 2
  done
  echo "Open WebUI is ready at $WEBUI_URL"

  # --- Open browser ---
  if command -v xdg-open &> /dev/null; then
    xdg-open "$WEBUI_URL" &
  elif command -v open &> /dev/null; then
    open "$WEBUI_URL" &
  elif command -v wslview &> /dev/null; then
    wslview "$WEBUI_URL" &
  else
    echo "Could not detect a browser opener. Please open $WEBUI_URL manually."
  fi

  echo ""
  echo "==================================================="
  echo "  Open WebUI is running at: $WEBUI_URL"
  echo "  Model: $OLLAMA_MODEL"
  echo "  To stop: docker compose down --remove-orphans"
  echo "==================================================="
else
  echo "Connecting to model $OLLAMA_MODEL..."
  echo "Type '/bye' to exit the chat."
  echo "---------------------------------------------------"

  docker exec -it $CONTAINER ollama run $OLLAMA_MODEL
fi