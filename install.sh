pkg update && pkg upgrade -y
pkg install -y python git ffmpeg termux-api poppler less ollama whisper
pip install --upgrade pip
pip install -r requirements.txt
ollama serve &
ollama pull gpt-oss:120b-cloud
python asistente2.py