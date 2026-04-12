# Claude Code - Refactorización: Árbol Compartido + Optimización Performance

## Contexto
EXIF Manager es una app PyQt6 de escritorio Windows. Actualmente hay **duplicación de código**: cada tab (Fotos, Videos, Duplicados) tiene su propio árbol de carpetas. Necesitamos:
1. **Un árbol único** compartido entre todos los tabs
2. **Optimización** para 5000+ fotos/videos sin freeze
3. **Mismas funcionalidades** (menú contextual, drag/drop) pero en un componente único

---

## Cambios de Arquitectura

### Antes (ACTUAL - INEFICIENTE)
```
main_window.py
└─ QTabWidget
   ├─ PhotoTab (tiene FolderTreePanel)
   ├─ VideoTab (tiene otro FolderTreePanel)
   └─ DuplicateTab (tiene otro FolderTreePanel)
```

### Después (OBJETIVO - CORRECTO)
```
main_window.py
└─ QSplitter (horizontal)
   ├─ FolderTreePanel (ÚNICA) ← 220px
   └─ QTabWidget
      ├─ PhotoTab (recibe folder_changed signal)
      ├─ VideoTab (recibe folder_changed signal)
      └─ DuplicateTab (recibe folder_changed signal)
```

---

## Tareas en Orden de Ejecución

### PASO 1: Refactorizar `main_window.py`
**Archivo:** `ui/main_window.py`

1. Mover `FolderTreePanel` a nivel de `QMainWindow`
2. Crear **signal `folder_changed(Path)`** en main_window
3. Crear método **`_setup_layout()`** que arme el splitter correcto:
   ```python
   # Pseudo-código (completo en el editor)
   splitter = QSplitter(Qt.Orientation.Horizontal)
   splitter.addWidget(self._folder_tree)  # Nueva instancia única
   splitter.addWidget(self._tab_widget)
   splitter.setStretchFactor(0, 0)  # Árbol: ancho fijo 220px
   splitter.setStretchFactor(1, 1)  # Tabs: flexible
   ```
4. **Conectar el árbol a main_window**:
   ```python
   self._folder_tree.folder_selected.connect(self._on_folder_changed)
   ```
5. Crear slot `_on_folder_changed(folder: Path)`:
   - Emite `self.folder_changed.emit(folder)` para que escuchen los tabs
   - Actualiza label/status si es necesario

### PASO 2: Refactorizar `PhotoTab` (ui/thumbnail_grid.py + photo_detail.py)
**Archivo:** `ui/thumbnail_grid.py`

1. **Remover `FolderTreePanel`** del constructor
2. **Agregar slot público**:
   ```python
   def on_folder_changed(self, folder: Path):
       """Llamado cuando el árbol selecciona una carpeta diferente"""
       if folder == self._current_folder:
           return  # Mismo folder, no re-cargar
       self._current_folder = folder
       self._load_thumbnails()  # Inicia el worker
   ```
3. **Inicializar sin folder** en `__init__`:
   ```python
   self._current_folder = None
   ```
4. **Safety check en todas las operaciones**:
   ```python
   if not self._current_folder:
       return  # UI no inicializada aún
   ```

**Archivo:** `ui/photo_detail.py`

1. Mismo patrón: `on_folder_changed(folder)`
2. Actualizar path de preview cuando folder cambia

### PASO 3: Refactorizar `VideoTab` (ui/video_grid.py + video_detail.py)
**Archivo:** `ui/video_grid.py`

1. **Remover el árbol duplicado completamente**
2. Crear `on_folder_changed(folder: Path)` idéntico a PhotoTab
3. Cargar videos de `folder` en background con `VideoThumbnailWorker`

**Archivo:** `ui/video_detail.py`

1. Mismo patrón que `photo_detail.py`

### PASO 4: Refactorizar `DuplicateTab` (ui/duplicate_panel.py)
**Archivo:** `ui/duplicate_panel.py`

1. **Remover árbol propio**
2. **Crear `on_folder_changed(folder: Path)`**:
   ```python
   def on_folder_changed(self, folder: Path):
       self._current_folder = folder
       self._duplicate_scan_scope = 'current'  # Siempre empieza en carpeta actual
       self._clear_results()  # Limpiar resultado anterior
       # NO iniciar escaneo automático (esperar a que el usuario haga click en "Buscar")
   ```
3. El botón "Buscar" ahora usa `self._current_folder` en lugar de pedirla al árbol

### PASO 5: Optimización para 5000+ items (CRÍTICO)
**Contexto:** Esto aplica a TODOS los grids (fotos, videos, duplicados)

#### 5.1 Virtual Scrolling (Lazy Loading)
En `thumbnail_grid.py` y `video_grid.py`:
```python
# En _load_thumbnails() / _load_videos():
# NO cargar TODO de una vez
# Estrategia: cargar solo lo visible + 2 filas adelante

BATCH_SIZE = 50  # Cargar 50 thumbnails a la vez
visible_range = self._calculate_visible_range()
start_idx = max(0, visible_range[0] - 100)  # 2 filas adelante
end_idx = min(len(files), visible_range[1] + 100)

# Emitir solo items[start_idx:end_idx] al worker
self._thumbnail_worker.load(items[start_idx:end_idx], start_idx)
```

#### 5.2 Batch Updates (No 1 por 1)
```python
# MAL — emite 5000 signals:
for item in items:
    self.item_loaded.emit(item)

# BIEN — emite cada 20 items:
batch = []
for i, item in enumerate(items):
    batch.append(item)
    if len(batch) == 20 or i == len(items) - 1:
        self.batch_loaded.emit(batch)
        batch = []
```

#### 5.3 setUpdatesEnabled(False) durante carga
```python
self._grid.setUpdatesEnabled(False)
# ... cargar 1000 items ...
self._grid.setUpdatesEnabled(True)
self._grid.update()
```

#### 5.4 LRU Cache para miniaturas en memoria
```python
from collections import OrderedDict

class ThumbnailCache:
    def __init__(self, max_size=200):
        self.cache = OrderedDict()
        self.max_size = max_size
    
    def get(self, key):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None
    
    def put(self, key, value):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        if len(self.cache) > self.max_size:
            self.cache.popitem(last=False)  # Quita el más viejo
```

#### 5.5 os.scandir() para listado rápido (3-5x más rápido que os.walk())
```python
# En core/file_scanner.py:
# MAL:
for root, dirs, files in os.walk(folder):
    ...

# BIEN:
with os.scandir(folder) as entries:
    for entry in entries:
        if entry.is_dir():
            # ...
```

---

## Patrones Críticos (COPIAR EXACTAMENTE)

### Threading Pattern
```python
# En __init__:
self._worker = None
self._thread = None

# Para iniciar:
self._thread = QThread()
self._worker = MyWorker(params)
self._worker.moveToThread(self._thread)
self._thread.started.connect(self._worker.run)
self._worker.finished.connect(self._on_finished)
self._worker.finished.connect(self._thread.quit)  # NO duplicar
self._thread.finished.connect(self._cleanup_thread)
self._thread.start()

# Cleanup:
def _cleanup_thread(self):
    if self._thread:
        self._thread.wait(5000)
        if self._thread.isRunning():
            self._thread.terminate()
            self._thread.wait(1000)
        self._worker.deleteLater()
        self._thread.deleteLater()
    self._worker = self._thread = None
```

### Lambda en loops (CRÍTICO)
```python
# BIEN:
for path in paths:
    btn.clicked.connect(lambda checked, p=path: self._on_delete(p))
```

### Progress Dialog (SIEMPRE ANTES del thread)
```python
self._progress_dlg = QProgressDialog('Procesando...', None, 0, total, self)
self._progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
self._progress_dlg.setMinimumDuration(0)  # SIEMPRE 0
self._progress_dlg.show()
QApplication.processEvents()  # Forzar repintado
self._thread.start()  # DESPUÉS
```

### Carpetas Excluidas (TODOS los escaneos)
```python
EXCLUDED_FOLDERS = {"_thumbcache", "_eliminados", "_duplicados_eliminados", "__pycache__"}

for root, dirs, files in os.walk(folder):
    dirs[:] = [d for d in dirs if d not in EXCLUDED_FOLDERS]
```

---

## Orden de Trabajo Recomendado

```
1. main_window.py — mover árbol, crear signal folder_changed
2. thumbnail_grid.py — implementar on_folder_changed, inicializar sin folder
3. photo_detail.py — idem
4. video_grid.py — remover árbol, implementar on_folder_changed
5. video_detail.py — idem
6. duplicate_panel.py — remover árbol, conectar a folder_changed
7. Optimizaciones — batch updates, virtual scrolling, caché LRU
8. Testing — abrir 20, 100, 500, 2000 fotos/videos sin freeze
9. Git commit + push
```

---

## Señales/Slots Necesarias

### En main_window.py
```python
class MainWindow(QMainWindow):
    folder_changed = pyqtSignal(Path)  # Nueva
    
    def _on_folder_changed(self, folder: Path):
        self.folder_changed.emit(folder)
```

### En cada Tab
```python
def on_folder_changed(self, folder: Path):
    """Slot público llamado por main_window"""
    if folder == self._current_folder:
        return
    self._current_folder = folder
    self._load_items()  # Inicia worker
```

### En main_window.__init__
```python
# Después de crear los tabs:
self.folder_changed.connect(self._photos_tab.on_folder_changed)
self.folder_changed.connect(self._videos_tab.on_folder_changed)
self.folder_changed.connect(self._duplicates_tab.on_folder_changed)
```

---

## Notas Importantes

- **NO eliminar funcionalidades**: menú contextual, drag/drop, etc. deben seguir funcionando
- **Verificar drag/drop**: el origen (grid) arrastra al árbol (destino) — debe seguir funcionando
- **Tests de performance**: cargar 2000+ fotos/videos y verificar que no freeze
- **Safety checks**: si `_current_folder` es None, todas las operaciones deben ignorarse
- **CLAUDE.md**: después de terminar, actualizar con la nueva arquitectura

---

## Archivos a Modificar

```
ui/main_window.py          ← PRINCIPAL (refactorizar layout)
ui/thumbnail_grid.py       ← Remover árbol, agregar on_folder_changed
ui/photo_detail.py         ← Agregar on_folder_changed
ui/video_grid.py           ← Remover árbol duplicado, agregar on_folder_changed
ui/video_detail.py         ← Agregar on_folder_changed
ui/duplicate_panel.py      ← Remover árbol, agregar on_folder_changed
core/file_scanner.py       ← OPCIONAL: optimizar con os.scandir()
```

## Archivos a Dejar Sin Cambios

```
core/exif_handler.py       ✓
core/backup_manager.py     ✓
core/duplicate_finder.py   ✓
core/video_handler.py      ✓
ui/folder_tree.py          ✓ (funciona igual)
ui/date_editor.py          ✓
ui/styles.py               ✓
```

---

## Stack & Versiones

- Python 3.11
- PyQt6 >= 6.4.0
- Pillow >= 10.0.0
- piexif >= 1.1.3
- ffmpeg-python >= 0.2.0
- hachoir >= 3.1.3

---

## Comando para Claude Code

```bash
cd D:\homelab\exif_manager
/allowed-tools Bash,Write,Edit,Read
# Luego copiar/pegar el contenido de este archivo como prompt
```

---

**FIN DEL PROMPT PARA CLAUDE CODE**
