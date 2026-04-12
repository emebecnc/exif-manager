# 📷 EXIF Manager

Aplicación de escritorio Windows para gestionar y corregir fechas EXIF de colecciones de fotos familiares.

---

## ¿Qué problema resuelve?

Muchas fotos de cámaras digitales tienen fechas incorrectas — ya sea porque la batería de la cámara se agotó y la fecha se reseteó, porque la cámara nunca fue configurada, o porque los archivos fueron renombrados o copiados en algún momento. Esto hace que gestores de fotos como **Immich**, Google Photos o cualquier visor cronológico las ubiquen en el lugar equivocado.

EXIF Manager permite corregir esas fechas de forma masiva, inteligente y con total trazabilidad — sin perder ningún dato original.

---

## ✨ Features

### 🗂 Navegación de carpetas
- Árbol de carpetas con conteo de imágenes por carpeta
- Soporte de rutas de red UNC (`\\servidor\share\...`)
- Indicador visual en carpetas ya procesadas
- Crear nuevas subcarpetas desde la interfaz
- Excluye automáticamente carpetas del sistema (`_thumbcache`, `_eliminados`, etc.)

### 🖼 Grid de miniaturas
- Carga progresiva en dos fases — sin bloqueos
- Caché de miniaturas local (`_thumbcache`) para carga rápida
- Ordenamiento por **fecha EXIF** o **nombre de archivo**, ascendente/descendente
- Por defecto: más vieja primero
- Selección múltiple con `Ctrl+Click` y `Shift+Click`
- Borde rojo en fotos con fecha inválida o ausente
- Filtro rápido "Solo sin fecha"
- Barra de progreso para carpetas grandes
- Doble click → abre la foto con el visor de Windows

### 🔍 Panel de metadatos
- Visualización completa de todos los campos EXIF disponibles
- Información del archivo: tamaño, dimensiones, fechas del filesystem, hash MD5
- Preview de la imagen con corrección de orientación automática

### 📅 Editor de fechas
El núcleo de la aplicación. Funciona en tres modos: **carpeta entera**, **foto individual** o **selección múltiple**.

- **Conservar fecha EXIF original** — no toca el EXIF, útil para renombrar solamente
- **Cambiar fecha EXIF** — modifica los campos de fecha
- Checkboxes independientes por componente: cambiar solo el **año**, solo el **mes**, o solo el **día** — sin tocar los demás
- Opciones de hora: conservar la original de cada foto o ingresar una hora fija
- **Leer fecha del nombre** — detecta automáticamente la fecha en el nombre del archivo (6 patrones soportados)
- Vista previa de cambios antes de aplicar (muestra fecha actual → fecha nueva)
- Barra de progreso durante el proceso — la app nunca se congela
- Al aplicar: escribe **todos los campos EXIF** (DateTimeOriginal, DateTimeDigitized, DateTime) **y** los timestamps del filesystem (fecha de modificación y creación)

### ✏️ Renombrado de archivos
Al editar fechas, se puede activar el renombrado automático con tres formatos:
- `2011-12-24-15h40m46s.jpg` — solo fecha
- `2011-12-24-15h40m46s_nombre_original.jpg` — fecha + nombre original
- Sin renombrar — solo cambia el EXIF

Manejo automático de colisiones (`_1`, `_2`, etc.)

### 🔁 Duplicados
Pestaña dedicada para detectar y resolver fotos duplicadas.

- Detección por hash MD5 exacto
- Vista comparativa lado a lado con todos los metadatos
- Scoring automático de calidad: resolución × peso del archivo
- Badge **★ MEJOR** en la foto de mayor calidad
- Eliminar duplicados uno a uno o todos de una vez con **Deduplicar todo**
- Los archivos eliminados se mueven a `_duplicados_eliminados` — nunca se borran permanentemente

### 🗑 Drag & Drop
- Seleccioná fotos en el grid y arrastralas a cualquier carpeta del árbol
- Manejo automático de colisiones de nombres

### 🔒 Seguridad y trazabilidad
- **Backup automático** `.exif_backup.json` antes de cualquier modificación
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
| Lectura/escritura EXIF | piexif |
| Timestamps Windows | pywin32 |
| Caché y paths | platformdirs, pathlib |
| Distribución | PyInstaller |

---

## 🚀 Instalación

```bash
pip install PyQt6 Pillow piexif pywin32 platformdirs
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

| Carpeta | Descripción | ¿Se puede borrar? |
|---|---|---|
| `_thumbcache/` | Miniaturas cacheadas | ✅ Sí, se regeneran |
| `_eliminados/` | Fotos eliminadas manualmente | ⚠️ Revisar antes |
| `_duplicados_eliminados/` | Duplicados descartados | ⚠️ Revisar antes |
| `_historial_original.txt` | Log de cambios | 🔒 Recomendado conservar |
| `.exif_backup.json` | Backup EXIF para restaurar | 🔒 Conservar hasta estar seguro |

---

## 📌 Changelog

### v0.1 — Abril 2026
- Primera versión funcional
- Editor de fechas por carpeta, individual y selección múltiple
- Detección y resolución de duplicados
- Drag & drop entre carpetas
- Sistema de backup y restauración EXIF
- Historial de cambios
- Caché de thumbnails
- Filtros y ordenamiento en grid
- Panel de limpieza de archivos temporales

---

## 🗺 Roadmap

- [ ] Soporte de video (MP4, MOV)
- [ ] Mejoras de rendimiento para carpetas con 1000+ fotos
- [ ] Installer con acceso directo en escritorio
- [ ] Ícono de aplicación definitivo

---

*Desarrollado con Claude Code (Anthropic) — Buenos Aires, Argentina*
