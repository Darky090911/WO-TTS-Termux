# WO-TTS v2.2

Asistente de voz + chat con **Ollama** para **Termux** (Android) y Linux.

Combina grabación de voz, transcripción con **Whisper**, conversación con modelos
locales o en la nube vía **Ollama**, y síntesis de voz con `termux-tts-speak`.
Todo desde la terminal, con atajos de teclado de una sola tecla, cola de TTS con
streaming y una barra de estado en vivo.

Si tiene algún problema o error al usarlo, coméntelo en el proyecto y será solucionado cuanto antes.

---

## ✨ Características

- 🎙️ **Grabación + transcripción** con Whisper.
- 💬 **Chat con Ollama** en streaming, con historial y contexto.
- 🌐 **Modelos cloud** (p. ej. `gpt-oss:120b-cloud`) y locales.
- 🔊 **TTS** con cola de frases y detección automática de idioma (ES/EN).
- 🧠 **Modo pensamiento** (thinking) para modelos que lo soportan.
- 🖐️ **Modo manos libres** (grabar → transcribir → responder → grabar).
- 🎨 **UI de terminal** con paleta, banner ASCII, gradientes y animaciones.
- 🗂️ **Historial** navegable, guardado/carga de conversaciones en JSON.
- 📎 **Carga de archivos** al contexto (`.txt`, `.md`, `.py`, `.json`, `.csv`, `.log`, `.rst`, `.html`, `.pdf`).
- ⏰ **Recordatorios** (`/recordar 10m sacar la pizza`).
- 📝 **Exportación a Obsidian** (`/obsidian`).
- 📋 **Copia automática** de la respuesta al portapapeles.
- 🔔 **Notificaciones** y **vibración** al terminar.
- 🧾 **Log de sesión** en `~/.ollama_chats/log.jsonl`.

---

## 📦 Instalación

### 1. Termux (Android)

```bash
# Actualizar repos
pkg update && pkg upgrade -y

# Dependencias base
pkg install -y python git ffmpeg termux-api poppler less
pkg install -y whisper            # Whisper desde los repos de Termux

# Ollama (elige UNA opción)
pkg install -y ollama
#   ...o si el paquete no existe en tu repo:
#   curl -fsSL https://ollama.com/install.sh | sh

# Cliente Python
pip install --upgrade pip
pip install -r requirements.txt

# Descargar/usar un modelo
ollama pull gpt-oss:120b-cloud    # modelo por defecto (cloud)
# ollama pull qwen2.5:0.5b        # alternativa local ligera

# Arrancar el servidor de Ollama (déjalo en otra sesión o en background)
ollama serve &
```

Instala también la app **Termux:API** desde F-Droid y concédele permisos de
micrófono y almacenamiento.

### 2. Linux / macOS

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gpt-oss:120b-cloud
```

> En Linux/macOS las funciones TTS y grabación dependientes de `termux-*`
> quedarán deshabilitadas. La parte de chat, historial, comandos slash, etc.
> funciona con normalidad.

---

## ▶️ Uso

```bash
python Asistente.py
# o con opciones:
python Asistente.py --model gpt-oss:120b-cloud --no-anim
```

### Opciones de línea de comandos

| Flag             | Descripción                                          |
| ---------------- | ---------------------------------------------------- |
| `-m, --model`    | Modelo de Ollama a usar (por defecto `gpt-oss:120b-cloud`). |
| `-s, --system`   | Prompt de sistema.                                   |
| `-t, --temp`     | Temperatura (0.0–2.0).                               |
| `-c, --ctx`      | `num_ctx` (tamaño de contexto).                      |
| `-l, --load`     | Cargar una conversación guardada (JSON).             |
| `--compact`      | Arrancar en modo compacto.                           |
| `--no-tts`       | Desactivar TTS al arrancar.                          |
| `--no-anim`      | Desactivar animaciones.                              |
| `--no-config`    | Ignorar `~/.ollama_chats/config.json`.               |

---

## ⌨️ Atajos de teclado

| Tecla | Acción                                |
| :---: | ------------------------------------- |
| `g`   | Grabar / detener grabación            |
| `e`   | Escribir texto manualmente            |
| `f`   | Modo manos libres on/off              |
| `t`   | TTS on/off                            |
| `d`   | Modo pensamiento on/off               |
| `m`   | Cambiar de modelo                     |
| `o`   | Opciones                              |
| `v`   | Ver historial                         |
| `c`   | Modo compacto on/off                  |
| `a`   | Animaciones on/off                    |
| `z`   | Modo verboso on/off                   |
| `l`   | Limpiar interfaz                      |
| `i`   | Estado de la sesión                   |
| `s`   | Guardar conversación                  |
| `r`   | Reiniciar contexto                    |
| `h`   | Mostrar ayuda                         |
| `q`   | Salir                                 |

---

## 🧩 Comandos slash (en modo `e`)

| Comando                    | Descripción                                  |
| -------------------------- | -------------------------------------------- |
| `/model <nombre>`          | Cambiar modelo.                              |
| `/temp <n>`                | Temperatura (0.0–2.0).                       |
| `/ctx <n>`                 | `num_ctx`.                                   |
| `/file <ruta>`             | Cargar archivo al contexto.                  |
| `/clear`                   | Limpiar contexto.                            |
| `/save`                    | Guardar conversación.                        |
| `/tts`                     | Toggle TTS.                                  |
| `/recordar <10m> <texto>`  | Programar recordatorio (s/m/h/d).            |
| `/obsidian`                | Exportar a Obsidian.                         |
| `/help`                    | Mostrar ayuda.                               |

---

## 📁 Rutas y archivos

| Ruta                                         | Contenido                            |
| -------------------------------------------- | ------------------------------------ |
| `~/.ollama_chats/`                           | Directorio de trabajo de la app.     |
| `~/.ollama_chats/config.json`                | Config persistente.                  |
| `~/.ollama_chats/log.jsonl`                  | Log de turnos.                       |
| `~/.ollama_chats/audios/`                    | Copias de respaldo de grabaciones.   |
| `~/storage/music/Grabaciones/`               | Audio + transcripción en curso.      |
| `~/storage/shared/Obsidian/WO-TTS/`          | Exportaciones a Obsidian.            |

---

## 🛠️ Solución de problemas

**“Se ve lo que escribo pero la terminal se comporta raro”**  
Comenta el `import readline` en la cabecera del script.

**“Los menús interactivos se comen las teclas”**  
Ya resuelto en v2.2 (`_with_paused`). Verifica que `sshkeyboard` esté actualizado.

**“No se genera transcripción”**  
Comprueba `which whisper ffmpeg` y que la app Termux:API tenga permiso de
micrófono. Si usas el `whisper` de Termux (`pkg install whisper`) verifica que
acepte las mismas opciones (`--model`, `--no-subs`); si no, ajusta la invocación
en `Grabador.detener_y_transcribir`.

**“Ollama no responde”**  
Verifica el servidor:
```bash
ollama serve &
ollama list
```

**“TTS no habla”**  
Comprueba `which termux-tts-speak` y que la app **Termux:API** esté instalada
en Android.

**“El modelo `gpt-oss:120b-cloud` no aparece / no responde”**  
Es un modelo **cloud**: requiere sesión iniciada en Ollama (`ollama signin`) y
conexión a internet. Verifícalo con `ollama list`. Puedes cambiarlo temporalmente
con `--model qwen2.5:0.5b` o desde el menú (`m`).

---

## 📝 Licencia

Sin licencia definida. Uso personal / educativo.

---

## 🧾 Versión

**v2.2** — ver bloque docstring al principio de `Asistente.py` para el
changelog.
