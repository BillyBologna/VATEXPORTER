# VAT Tool: Maya → Web (Three.js) / Unity / Unreal

Exporta **Vertex Animation Textures** desde Maya y las reproduce en la web o en un motor de videojuegos, sin huesos y con instancias baratas.

```
Maya (vat_exporter.py) ──► texturas + malla + JSON ──► Web  (index.html, Three.js)
                                                   ├─► Unity (VAT_Standard.shader / VAT.hlsl)
                                                   └─► Unreal (VAT_Position.hlsl / VAT_Normal.hlsl)
```

## Contenido del paquete

| Carpeta | Archivo | Para qué sirve |
|---|---|---|
| `maya/` | `vat_exporter.py` | Herramienta con ventana para Maya. Exporta todo. |
| `web/` | `index.html` | Visor Three.js: instancias, PBR, texturas, normal maps, Rigid. |
| `unity/` | `VAT_Standard.shader` | Unity **Built-in RP**. Se asigna a un material. |
| `unity/` | `VAT.hlsl` | Unity **URP/HDRP**, para un Custom Function node de Shader Graph. |
| `unreal/` | `VAT_Position.hlsl`, `VAT_Normal.hlsl` | Código para dos nodos *Custom* de un material de Unreal. |

---

## 1. Exportar desde Maya

1. Abre el **Script Editor** (pestaña Python), pega `vat_exporter.py` completo y ejecútalo. Se abre la ventana.
2. Selecciona la malla (o varias piezas si usas Rigid).
3. Elige **Destino**, revisa las opciones y pulsa **EXPORTAR VAT**.

Requisitos: Maya con Python 3 y `numpy` (ya viene incluido). No necesita librerías extra.

### Opciones de la ventana

| Opción | Qué hace |
|---|---|
| **Destino** | Web, Unity, Unreal o Genérico. Rellena valores por defecto (formato, escala, ejes). |
| **Tipo** | *Soft body*: una malla que se deforma. *Rigid body*: varias piezas que se mueven sin deformarse. |
| **Espacio** | *Mundo*: incluye el movimiento del objeto. *Objeto*: solo la deformación. *Offset*: mundo menos el frame inicial. |
| **Precisión** | Float32 (recomendado) o Float16. Float16 solo con Offset. |
| **Archivos** | `.bin` (web), `.exr` (motores), FBX con UV2, y malla dentro del JSON. |
| **Bordes duros** | Normales por cara. Divide vértices, así que sube el número de filas. |
| **Tangentes** | Para normal maps. |
| **Color / alpha** | Color de vértice por frame (fluidos, efectos). |
| **Frames, paso, tamaño máx.** | Rango a exportar, salto entre frames y tamaño máximo de textura (4096). Si se supera, se parte en tiles. |
| **Escala unidades** | Web 1, Unity 0.01 (cm → m), Unreal 1. |

### Qué espacio elegir

- **Objeto único que se mueve por transform** (tu esfera rebotando): *Mundo*.
- **Muchas copias (instancias)** o mover la malla desde el motor: *Objeto* u *Offset*.
- **Motores con EXR half (Unreal siempre)**: *Offset*, porque las posiciones absolutas pierden precisión.

### Archivos que salen

| Archivo | Contenido | Se usa en |
|---|---|---|
| `nombre_pos.bin` / `.exr` | Posición de cada vértice en cada frame | Web / Motores |
| `nombre_nrm.bin` / `.exr` | Normales | Web / Motores |
| `nombre_tan.*`, `nombre_col.*` | Tangentes y color (opcionales) | Web / Motores |
| `nombre_rot.*` | Rotación por pieza (solo Rigid) | Web / Motores |
| `nombre_vat.json` | Metadatos (y la malla si se activa) | Web; en motores solo lo lees |
| `nombre_vat.fbx` | Malla con **UV2** de lookup | Unity / Unreal |

### Convención de la textura

- Ancho = frames (× tiles). Alto = filas (vértices ya desdoblados por UV y, si aplica, por normal).
- Fila `r`: `v = (r + 0.5) / tile_height`. Con tiles, `u = tile / tiles` en el UV2.
- Los `.exr` se guardan **volteados en vertical** para que el UV2 funcione directo en Unity y Unreal.
- El `.bin` va sin voltear, listo para `THREE.DataTexture`.

### Campos importantes del JSON

| Campo | Significado |
|---|---|
| `frames`, `fps` | Duración y velocidad de reproducción. |
| `vertices` | Filas de la textura (o piezas, en Rigid). |
| `texture` | `[ancho, alto]` de la textura. |
| `tiles`, `tile_height` | Partición horizontal cuando hay demasiadas filas. |
| `position_mode` | `absolute` u `offset`. |
| `space`, `mode`, `target` | Cómo se exportó. |
| `unit_scale`, `up_axis` | Escala aplicada y eje vertical del destino. |
| `bbox_min`, `bbox_max` | Caja de todo el recorrido. |
| `dtype` | `float32` o `float16`. |
| `flip_winding` | `true` si se invirtió el orden de triángulos (Unity y Unreal). |

---

## 2. Web (Three.js)

1. Deja en la misma carpeta: `index.html`, `nombre_pos.bin`, `nombre_nrm.bin` y `nombre_vat.json`.
2. Sirve la carpeta con un servidor (`python -m http.server` o GitHub Pages). No funciona abriendo el HTML con doble clic.
3. Si tu nombre base no es `my_mesh`, abre `index.html?base=./tu_nombre` o cambia `BASE` en el archivo.

El visor lee el JSON y se adapta solo: tiles, float16, modo offset, color de vértice, tangentes y Rigid. Panel de control:

- **Reproducción:** velocidad, pausa y frame.
- **Instancias:** de 1 a 2500, con desfase aleatorio de tiempo.
- **Material:** color, metal, rugosidad, textura de color y normal map (este último requiere haber exportado tangentes).

Si ves la versión vieja tras subir archivos, recarga con `Ctrl+Shift+R`. El visor ya evita la caché en sus peticiones.

---

## 3. Unity

Exporta con destino **Unity**: genera `.exr` (float32), el FBX y el JSON.

### Importar las texturas (`_pos.exr`, `_nrm.exr`)

| Ajuste | Valor |
|---|---|
| sRGB (Color Texture) | **Off** |
| Generate Mip Maps | **Off** |
| Filter Mode | **Point** |
| Compression | **None** |
| Wrap Mode | Clamp |
| Format | RGBA 32 bit float (o RGBA Half si exportaste Float16) |

### Built-in RP

1. Importa `nombre_vat.fbx`. El FBX trae dos juegos de UV: el segundo (UV2) es el lookup. Desactiva *Generate Lightmap UVs* si no lo necesitas.
2. Crea un material con el shader **VAT/Standard**.
3. Asigna `_PosTex` y `_NrmTex`, y copia del JSON: `_Frames` (= `frames`), `_TexW` (= `texture[0]`), `_Tiles` (= `tiles`) y `_Fps` (= `fps`).
4. Activa `_OffsetMode` si `position_mode` es `offset`.
5. Activa **Enable GPU Instancing** en el material.
6. Para desfasar cada instancia, pon un valor distinto de `_Phase` con `MaterialPropertyBlock`.

### URP / HDRP (Shader Graph)

1. Crea un Custom Function node → Type **File** → Source `VAT.hlsl` → Name **VAT**.
2. Inputs, en este orden: PosTex, NrmTex, UV2, Frame, Frames, TexW, Tiles, OffsetMode, VertexPos.
3. `UV2` es el nodo *UV* con canal **UV1**. `VertexPos` es el nodo *Position* en Object.
4. `Frame = Time × Fps × Speed + Phase`. Conéctalo con nodos normales.
5. Las salidas Position y Normal van a **Vertex Position** y **Vertex Normal** (Object space).

---

## 4. Unreal Engine

Exporta con destino **Unreal**, modo **Offset**: los EXR entran a 16 bits en Unreal, y las posiciones absolutas perderían precisión.

### Importar las texturas

| Ajuste | Valor |
|---|---|
| Compression Settings | **HDR (RGBA16F, sin compresión)** |
| Mip Gen Settings | **NoMipmaps** |
| sRGB | **Off** |
| Filter | **Nearest** |
| Never Stream | **On** |

### Importar el FBX

- Static Mesh, **sin Nanite**.
- Activa **Use Full Precision UVs** en Build Settings, para no perder precisión en el UV2.
- Desactiva *Generate Lightmap UVs*, para que no sobrescriba el UV2.
- Amplía los **Extended Bounds** del mesh: la animación mueve los vértices y, si no, el culling puede ocultarlo.

### Material

1. Añade una expresión **Custom** con el código de `VAT_Position.hlsl` (Output Type *Float 3*). Inputs con los mismos nombres del archivo: `PosTex` (Texture Object), `UV2` (TextureCoordinate, índice 1), `Frame`, `Frames`, `TexW`, `Tiles`.
2. `Frame = Time × Fps × Speed + PerInstanceRandom × Frames`. `Frames`, `TexW` y `Tiles` salen del JSON.
3. La salida pasa por **Transform Vector (Local → World)** y va a **World Position Offset**.
4. Repite con `VAT_Normal.hlsl` y `NrmTex`. Pasa el resultado por un **Vertex Interpolator**, transfórmalo a mundo y conéctalo a **Normal**, con *Tangent Space Normal* desactivado en el material.
5. Usa el material con Instanced Static Mesh o HISM para tener instancias.

---

## 5. Verificar los ejes (recomendado)

Los ejes de Unity `(-x, y, z)` y Unreal `(x, z, y)` están derivados de las convenciones de cada motor. Están comprobados matemáticamente, pero **no contra el motor real**. Compruébalo una vez:

1. En Maya, selecciona el destino y pulsa **Crear test de ejes**. Genera un objeto asimétrico animado, su VAT y `axis_test_ref.fbx`, una referencia estática del frame 15.
2. En el motor, pon el material con `_OffsetMode` **desactivado** y el tiempo fijo en el frame 15.
3. Coloca junto a él el FBX de referencia, con el mismo origen. Deben **superponerse**.
4. Si aparecen espejados, girados o desplazados, avísame con una captura para ajustar la matriz de ejes de ese motor.

---

## 6. Problemas frecuentes

| Síntoma | Causa probable |
|---|---|
| La malla no se mueve en la web | Archivos viejos en caché, o se exportó en Objeto un movimiento hecho con transform. Usa Mundo. |
| En Maya, "Movimiento máx = 0" | La animación no afecta esa malla, o falta evaluar. El script ya desactiva el Evaluation Manager. |
| Vértices que tiemblan | Float16 con posiciones absolutas. Usa Float32 u Offset. |
| Malla enorme o diminuta en Unity | Falta la escala 0.01, o el material no coincide con el FBX (unidades). |
| Malla espejada o girada | Ejes del motor. Haz el test de ejes. |
| Caras invertidas | Se invirtió el winding en Unity y Unreal. Si aún se ve mal, activa *Two Sided* en el material. |
| Error de textura demasiado ancha | Frames × tiles supera el máximo. Sube el paso, baja los frames o aumenta el tamaño máximo. |
| Artefactos entre el último y el primer frame | La interpolación cierra el bucle. Si tu animación no es cíclica, corta un frame. |

---

## 7. Estado y pendientes

**Probado:** el exportador soft body con tu esfera en Maya y el visor web. La lógica del resto del script (tiles, offset, bordes duros, tangentes, color, Rigid, EXR y ejes) se probó con una simulación de la API de Maya, no con Maya real.

**Sin probar en el motor real:** los shaders de Unity, los nodos de Unreal y el FBX con UV2. El código sigue las convenciones de cada motor, pero verifica con una malla sencilla antes de usarlo en producción.

**Ideas para más adelante:**

- Shader Graph y material de Unreal ya montados (archivos `.shadergraph` y `.uasset`) en lugar de código.
- Rigid body en Unity y Unreal, con su propio shader.
- Compresión de los `.bin` para web (float16 por defecto, o cuantización).
- Múltiples animaciones (clips) en una misma textura.
- Blending entre animaciones.
- GLB con la malla y UV2 para web.
- LOD de animación (menos frames en objetos lejanos).
