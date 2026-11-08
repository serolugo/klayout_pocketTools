# Pocket Tools 1.0 — comprobaciones antes del tag

Ejecutado el 7 de noviembre de 2026 en Windows con KLayout 0.30.12:

```powershell
python -m unittest discover -s tests -v
```

Resultado: **47 pruebas del runner, sin fallos, 2 omitidas**. Incluye un proceso
aislado del mismo KLayout nativo, con interfaz Qt offscreen y `KLAYOUT_HOME`
temporal. No modifica la configuración de la instancia principal.

## Comprobado automáticamente

- `boxify`: rectángulo normal, rectángulo con vértices colineales redundantes,
  L, hueco, rombo girado y geometría de área nula. Selección mixta con polígonos,
  Box y texto; instancias y polígonos de una subcelda se omiten. Conserva capa,
  propiedades y geometría. Un solo Undo restaura todos los polígonos convertidos.
- Booleanas: dos rectángulos parcialmente solapados, las cinco operaciones y
  ambos órdenes A/B mediante `ObjectInstPath.seq`. Las diferencias cambian y
  las simétricas conservan su resultado. Undo único restaura las dos figuras.
  Empates permiten union/intersect/xor y rechazan ambas diferencias; intersección
  vacía conserva originales; capas distintas e instancias/subceldas se rechazan.
- `merge`: acción nativa Merge Shapes sobre selección, selección vacía y Undo.
  `stretch`: modo Partial y foco en el widget `canvas`, desde la consola.
  Aviso visible durante Partial; repetir `stretch`, escribir `move` o pulsar
  **Volver a Move** desactiva Partial. El aviso también sigue los cambios de
  herramienta nativa. `move` está en la ayuda y el autocompletado con Tab.
- Dispatcher: comando registrado en historial, asignación Python con nombre
  `merge`, bloque multilínea y ayuda con todos los comandos nuevos.
- Consolas: orden Python → Ruby → PowerShell, Python inicial, ocultar todas,
  recuperar Python mediante la acción del menú, conservar sesión e historial,
  cerrar con X y recuperar. Themes y Consoles coexisten en ambos órdenes de
  carga; recargar la consola conserva Themes.
- Búsqueda de capas: suite existente de integración nativa.
- Versión de Pocket Console y `grain.xml` coinciden: 1.0.
- Regresión del gestor: pruebas existentes; hashes SHA-256 de
  `pocket_pdk.py`, `pocket_sky130.py` y `pocket_pdk_remove.py` sin cambios.

Omisiones: symlinks de directorio no disponibles para este usuario, y prueba
del arranque de SKY130 porque no está disponible la instalación esperada por
esa prueba. Los tests de junctions de Windows sí pasan. Esto no valida las
PCells de SKY130 ni sus dependencias; el gestor sigue siendo experimental.

## Pendiente en la interfaz visible de Windows antes de crear v1.0

- Arranque normal desde Salt: comprobar apariencia de los temas y barra de
  búsqueda con el escalado de pantalla habitual.
- Seleccionar A con clic y B con Shift + clic; repetir invirtiendo los clics.
  Confirmar diferencias y operaciones simétricas sobre esa selección real.
  Repetir con selección por marco para comprobar el mensaje de empate.
- Ejecutar `stretch` y arrastrar un borde y un extremo de pista con el ratón;
  verificar el resultado y Undo. El test automático comprueba modo y foco,
  no simula ese gesto interactivo.
- Mostrar/ocultar cada consola con el menú, cerrar el dock con X y recuperarlo,
  incluyendo un dock flotante y otro acoplado. Confirmar aspecto y foco al
  volver desde otras aplicaciones.

**No se creó ningún tag.** El historial paramétrico no forma parte de 1.0.
