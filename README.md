# 📷 EXIF Manager

Aplicación de escritorio Windows para gestionar y corregir fechas EXIF de colecciones de fotos y videos familiares.

---

## ¿Qué problema resuelve?

Muchas fotos y videos de cámaras digitales tienen fechas incorrectas — ya sea porque la batería de la cámara se agotó y la fecha se reseteó, porque la cámara nunca fue configurada, o porque los archivos fueron renombrados o copiados en algún momento. Esto hace que gestores de fotos como **Immich**, Google Photos o cualquier visor cronológico las ubiquen en el lugar equivocado.

EXIF Manager permite corregir esas fechas de forma masiva, inteligente y con total trazabilidad — sin perder ningún dato original.

---

## ✨ Features

### 🗂 Navegación de carpetas
- Árbol de carpetas con conteo de imágenes y videos por carpeta
- Soporte de rutas de red UNC (`\\servidor\share\...`)
- Indicador visual (verde) en carpetas ya procesadas
- Crear/mover/copiar subcarpetas desde la interfaz
- Drag & Drop de fotos entre carpetas directamente en el árbol
- Excluye automáticamente carpetas del sistema (`_thumbcache`, `_eliminados`, etc.)

### 🖼 Grid de fotos
- Carga progresiva en dos fases — sin bloqueos
- Caché de miniaturas local (`_thumbcache`) para carga rápida
- Ordenamiento por **fecha EXIF** o **nombre de archivo**, ascendente/descendente
- Selección múltiple con `Ctrl+Click` y `Shift+Click`
- Borde rojo en fotos con fecha inválida o ausente
- Filtro rápido "Solo sin fecha"
- Barra de progreso para carpetas grandes
- Doble click → abre la foto con el visor de Windows

### 🎬 Grid de videos
- Grid de videos con miniaturas del primer frame (via ffmpeg)
- Metadatos completos: resolución, duración, codec, bitrate, cámara
- Editor de fechas igual al de fotos
- Backup automático `.video_backup.json`
- Soporte: MP4, MOV, M4V, MKV, AVI, WMV, 3GP

### 🔍 Panel de metadatos
- Visualización completa de todos los campos EXIF disponibles
- Información del archivo: tamaño, dimensiones, fechas del filesystem, hash MD5
- Preview de la imagen con corrección de orientación automática

### 📅 Editor de fechas (fotos y videos)
El núcleo de la aplicación. Funciona en tres modos: **carpeta entera**, **foto individual** o **selección múltiple**.

- **Conservar fecha EXIF original** — no toca el EXIF, útil para renombrar solamente
- **Cambiar fecha EXIF** — modifica los campos de fecha
- Checkboxes independientes por componente: cambiar solo el **año**, solo el **mes**, o solo el **día**
- Opciones de hora: conservar la original de cada foto o ingresar una hora fija
- **Leer fecha del nombre** — detecta automáticamente la fecha en el nombre del archivo (6 patrones soportados)
- Vista previa de cambios antes de aplicar
- Barra de progreso durante el proceso — la app nunca se congela
- Al aplicar: escribe **todos los campos EXIF** y los timestamps del filesystem

### ✏️ Renombrado de archivos
Al editar fechas, se puede activar el renombrado automático con tres formatos:
- `2011-12-24-15h40m46s.jpg` — solo fecha
- `2011-12-24-15h40m46s_nombre_original.jpg` — fecha + nombre original
- Sin renombrar — solo cambia el EXIF

Manejo automático de colisiones (`_1`, `_2`, etc.)

### 🔁 Duplicados (fotos + videos)
Pestaña dedicada para detectar y resolver archivos duplicados.

- **Tres modos de búsqueda**: 📷 Fotos / 🎬 Videos / 🔀 Duplicados (auto-detecta tipo dominante)
- Auto-detección del tipo de media según la carpeta abierta
- Detección por hash MD5 exacto — 100% preciso
- Vista comparativa lado a lado con todos los metadatos
- Scoring automático de calidad: resolución + bitrate + peso del archivo
- Badge **★ Conservar** en el archivo de mayor calidad (siempre a la izquierda)
- Conservar uno a uno: mueve los demás a `_duplicados_eliminados` inmediatamente
- **Deduplicar todo**: procesa todos los grupos de una vez con confirmación detallada
- Los archivos eliminados **nunca se borran permanentemente** — siempre van a `_duplicados_eliminados`
- Escaneo robusto: cada archivo se procesa en try/except individual, con checkpoints cada 100 archivos

### 🗑 Drag & Drop
- Seleccioná fotos en el grid y arrastralas a cualquier carpeta del árbol
- Manejo automático de colisiones de nombres

### 🔒 Seguridad y trazabilidad
- **Backup automático** `.exif_backup.json` / `.video_backup.json` antes de cualquier modificación
- **Restaurar EXIF original** — revierte todos los cambios de una carpeta
- **`_historial_original.txt`** — log legible por humanos con cada operación realizada
- Ningún archivo se borra permanentemente — siempre se mueve a una carpeta de respaldo

### 🧹 Limpieza
- Panel dedicado para eliminar carpetas temporales generadas por la app
- Calcula el espacio a liberar antes de borrar
- Checkboxes por tipo: thumbnails, eliminados, duplicados, historial, backup

### 📋 Log de operaciones
- Registro completo de todas las operaciones: edición de fecha, renombrado, movido, eliminado
- Filtros por tipo de operación
- Exportable a CSV

---

## 🛠 Stack tecnológico

| Componente | Tecnología |
|---|---|
| Interfaz | PyQt6 |
| Procesamiento de imágenes | Pillow |
| Lectura/escritura EXIF fotos | piexif |
| Lectura/escritura metadatos video | ffmpeg + hachoir |
| Timestamps Windows | pywin32 |
| Paths y filesystem | pathlib |
| Distribución | PyInstaller |

---

## 🚀 Instalación

### Requisitos previos
- Python 3.11+
- [ffmpeg](https://ffmpeg.org/download.html) en el PATH (o `ffmpeg.exe` junto al programa)

### Instalar dependencias

```bash
pip install PyQt6 Pillow piexif pywin32 hachoir ffmpeg-python
```

### Ejecutar

```bash
python main.py
```

O doble click en `run_exif_manager.bat`

---

## 📦 Compilar a .exe

```bash
pyinstaller build.spec
```

Genera `dist/ExifManager/ExifManager.exe` — standalone, no requiere Python instalado.

---

## 📁 Carpetas generadas por la app

| Carpeta / Archivo | Descripción | ¿Se puede borrar? |
|---|---|---|
| `_thumbcache/` | Miniaturas cacheadas | ✅ Sí, se regeneran |
| `_eliminados/` | Fotos/videos eliminados manualmente | ⚠️ Revisar antes |
| `_duplicados_eliminados/` | Duplicados descartados | ⚠️ Revisar antes |
| `_historial_original.txt` | Log de cambios legible | 🔒 Recomendado conservar |
| `.exif_backup.json` | Backup EXIF fotos (restaurar) | 🔒 Conservar hasta estar seguro |
| `.video_backup.json` | Backup metadatos videos | 🔒 Conservar hasta estar seguro |

---

## 📌 Changelog

### v1.0 — Abril 2026
- ✅ Editor de fechas EXIF por carpeta, individual y selección múltiple
- ✅ Grid de fotos con carga progresiva en 2 fases + caché
- ✅ Grid de videos con miniaturas via ffmpeg
- ✅ Editor de fechas para videos (ffmpeg codec=copy, sin recompresión)
- ✅ Detección y resolución de duplicados (fotos + videos) por MD5
- ✅ Auto-detección de tipo de media en la pestaña Duplicados
- ✅ Drag & Drop entre carpetas en el árbol
- ✅ Sistema de backup y restauración EXIF
- ✅ Historial de cambios por carpeta
- ✅ Caché de thumbnails con LRU
- ✅ Filtros y ordenamiento en grid
- ✅ Panel de limpieza de archivos temporales
- ✅ Log de operaciones con exportación CSV
- ✅ Soporte rutas UNC (red local / NAS)
- ✅ Tema oscuro profesional con bordes redondeados

---

*Desarrollado con Claude Code (Anthropic) — Buenos Aires, Argentina*
