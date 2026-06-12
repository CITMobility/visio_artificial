import cv2
import sys

# 1. Pipeline de lectura (Recibe del laptop en el puerto 5000)
def gstreamer_read_pipeline():
    return (
        "udpsrc port=5001 caps=\"video/mpegts, systemstream=(boolean)true\" ! "
        "tsdemux ! h264parse ! nvv4l2decoder ! "
        "nvvidconv ! video/x-raw, width=640, height=480, format=BGRx ! "
        "videoconvert ! video/x-raw, format=BGR ! "
        "appsink drop=True max-buffers=1 sync=false"
    )

# 2. Pipeline de escritura (Envía a DeepStream en el puerto 5400)
# Toma los frames BGR, los pasa por hardware a H.264, los empaqueta en MPEG-TS y los transmite
# 2. Pipeline de escritura (Envía a DeepStream en el puerto 5400)
# Usa la CPU (x264enc) en modo ultrarrápido porque la Orin Nano no tiene NVENC
def gstreamer_write_pipeline():
    return (
        "appsrc ! video/x-raw, format=BGR ! "
        "queue ! videoconvert ! video/x-raw, format=I420 ! "
        "x264enc speed-preset=ultrafast tune=zerolatency bitrate=2000 ! "
        "h264parse ! mpegtsmux ! "
        "udpsink host=127.0.0.1 port=5400 sync=false"
    )

# Configurar entrada
cap = cv2.VideoCapture(gstreamer_read_pipeline(), cv2.CAP_GSTREAMER)

if not cap.isOpened():
    print("Error: No se pudo abrir el stream de entrada en el puerto 5000.")
    sys.exit()

# Configurar salida
# Usamos 30 fps por defecto, ajusta si el video de tu camión tiene otra tasa de cuadros
fps = 30.0 
out = cv2.VideoWriter(gstreamer_write_pipeline(), cv2.CAP_GSTREAMER, 0, fps, (640, 480), True)

if not out.isOpened():
    print("Error: No se pudo abrir el stream de salida hacia el puerto 5400.")
    sys.exit()

ZONA_MUERTA_1 = (2, 5, 200, 475)
ZONA_MUERTA_2 = (392, 186, 242, 289)

print("Procesando stream en modo headless y enviando a DeepStream (Puerto 5400)...")
print("Presiona Ctrl+C en la terminal para detener.")

try:
    while True:
        ret, frame = cap.read()
        
        if not ret:
            continue

        # Agregar los cuadros negros (Zonas Muertas)
        x1, y1, w1, h1 = ZONA_MUERTA_1
        cv2.rectangle(frame, (x1, y1), (x1 + w1, y1 + h1), (0, 0, 0), -1)
        
        x2, y2, w2, h2 = ZONA_MUERTA_2
        cv2.rectangle(frame, (x2, y2), (x2 + w2, y2 + h2), (0, 0, 0), -1)

        # Escribir el frame procesado al nuevo stream UDP
        out.write(frame)

except KeyboardInterrupt:
    print("\nDeteniendo el procesamiento...")

finally:
    cap.release()
    out.release()