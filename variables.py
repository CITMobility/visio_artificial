#!/usr/bin/env python3
# =============================================================================
# CIT Mobility / Edge Mobility
# variables.py :: arranca los programas que miden y muestra sus variables
#
#   Un solo comando pone en marcha todo el sistema:
#
#       python3 variables.py
#
#   Hoy recoge dos variables de la unidad:
#
#       AFORO   cuanta gente va dentro   (DeepStream-Yolo/contador_neto.py)
#       GPS     donde esta la unidad     (lector_gps.py)
#
#   Lo que hace, en orden:
#     1. Lanza los programas de la lista PROGRAMAS.
#     2. Se queda escuchando el bus e imprime aqui lo que le llega.
#     3. Al pulsar Ctrl+C, detiene los programas que lanzo antes de salir.
#
#   La salida de cada programa hijo NO se mezcla con esta pantalla: va a su
#   propio archivo en datos/. Si un hijo se cae, se muestran aqui sus ultimas
#   lineas para saber por que.
#
#   Para anadir una variable nueva en el futuro, basta con meter su programa
#   en la lista PROGRAMAS y anadir su tipo en al_recibir(). El bus escucha con
#   comodin, asi que no hay que tocar nada mas.
#
# Uso:
#   cd /home/nelsonkanzela/Desktop/cit/
#   python3 variables.py               # camara por defecto (/dev/video0)
#   python3 variables.py 1             # usar /dev/video1
#
# Variables de entorno opcionales:
#   LANZAR=0               solo escuchar, sin arrancar nada (para depurar)
#   SIN_GPS=1              no lanzar el lector de GPS
#   MODELO=yolo26          volver al detector retirado (default: peoplenet)
#   ORIGEN=camion01        escuchar solo ese origen (default: todos)
#   SIN_COLOR=1            desactiva los colores ANSI
#   CIT_TRANSPORTE=mqtt    usar broker en vez de archivo (default: archivo)
# =============================================================================

import os
import sys
import time
import signal
import subprocess
import threading
import _thread

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

from cit_core import bus  # noqa: E402

# --- Programas que se lanzan junto al monitor --------------------------------
# Para sumar otra variable al sistema, anade otra entrada aqui.
PROGRAMAS = [
    {
        "nombre": "contador",
        "ruta": os.path.join(AQUI, "DeepStream-Yolo", "contador_neto.py"),
        "args": sys.argv[1:],          # p.ej. el numero de camara
    },
]

# El GPS es opcional: si la unidad no lleva modulo, con SIN_GPS=1 no se lanza
# y no se llena el log de reintentos. Va sin args: sys.argv es la camara y eso
# solo le sirve al contador.
if os.environ.get("SIN_GPS") != "1":
    PROGRAMAS.append({
        "nombre": "gps",
        "ruta": os.path.join(AQUI, "lector_gps.py"),
        "args": [],
        "critico": False,      # si se cae, el aforo debe seguir contando
    })

LANZAR = os.environ.get("LANZAR", "1") == "1"
ESPERA_CIERRE = 8.0                    # segundos de cortesia al pedir que paren

# --- Parametros del monitor --------------------------------------------------
ORIGEN = os.environ.get("ORIGEN", "+")      # "+" es el comodin: cualquiera
PATRON = "{}/{}/#".format(bus.RAIZ, ORIGEN)

# --- Colores -----------------------------------------------------------------
_color = sys.stdout.isatty() and os.environ.get("SIN_COLOR") != "1"


def c(codigo, texto):
    return "\033[{}m{}\033[0m".format(codigo, texto) if _color else str(texto)


VERDE = "92"
ROJO = "91"
AMARILLO = "93"
GRIS = "90"
NEGRITA = "1"

# --- Estado ------------------------------------------------------------------
_ultimo = {}        # origen -> ultimo aforo mostrado
_ultimo_gps = {}    # origen -> ultima posicion mostrada (redondeada)
_conectados = {}    # origen -> True/False
_hijos = []         # procesos lanzados por este programa
_cerrando = False
_fallo = False      # algun hijo se murio por su cuenta


def hora_local():
    return time.strftime("%H:%M:%S")


# =============================================================================
# Lanzar y detener los programas hijos
# =============================================================================
def _ultimas_lineas(ruta, cuantas=15):
    try:
        with open(ruta, errors="replace") as f:
            return [l.rstrip() for l in f.readlines()[-cuantas:]]
    except OSError:
        return []


def lanzar(prog):
    """Arranca un programa hijo. Su salida se desvia a datos/<nombre>.log"""
    if not os.path.isfile(prog["ruta"]):
        sys.stderr.write("ERROR: no existe {}\n".format(prog["ruta"]))
        return None

    os.makedirs(bus.DIR_DATOS, exist_ok=True)
    ruta_log = os.path.join(bus.DIR_DATOS, "{}.log".format(prog["nombre"]))

    entorno = os.environ.copy()
    entorno.setdefault("DISPLAY", ":0")     # la vision abre una ventana
    entorno["PYTHONUNBUFFERED"] = "1"       # que el log se escriba al momento

    registro = open(ruta_log, "w")
    proceso = subprocess.Popen(
        [sys.executable, prog["ruta"]] + list(prog["args"]),
        cwd=os.path.dirname(prog["ruta"]),
        stdout=registro,
        stderr=subprocess.STDOUT,
        env=entorno,
        start_new_session=True,   # grupo propio: se puede parar el arbol entero
    )
    return {"prog": prog, "proceso": proceso, "log": ruta_log, "registro": registro}


def detener_todos():
    """Pide a los hijos que paren, con educacion primero y a la fuerza despues."""
    global _cerrando
    if _cerrando:
        return
    _cerrando = True

    for h in _hijos:
        proceso = h["proceso"]
        if proceso.poll() is not None:
            continue

        print(c(GRIS, "Deteniendo {}...".format(h["prog"]["nombre"])), flush=True)

        # SIGINT es el equivalente a pulsar Ctrl+C: deja que el hijo cierre
        # su pipeline de GStreamer en condiciones.
        for señal in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proceso.pid), señal)
            except (OSError, ProcessLookupError):
                break
            try:
                proceso.wait(timeout=ESPERA_CIERRE if señal == signal.SIGINT else 3.0)
                break
            except subprocess.TimeoutExpired:
                continue        # no hizo caso, se sube el tono

        try:
            h["registro"].close()
        except OSError:
            pass


def vigilar():
    """Si un hijo se muere solo, avisar aqui.

    Solo la muerte de un programa CRITICO tumba el sistema. El GPS no lo es:
    perder la posicion es una degradacion molesta, pero llevarse por delante el
    conteo de aforo por un cable de GPS suelto seria mucho peor. Se avisa en
    pantalla y el resto sigue funcionando.
    """
    global _fallo
    avisados = set()
    while not _cerrando:
        for h in _hijos:
            codigo = h["proceso"].poll()
            if codigo is None:
                continue
            if _cerrando:
                return
            nombre = h["prog"]["nombre"]
            if nombre in avisados:
                continue
            avisados.add(nombre)

            critico = h["prog"].get("critico", True)
            etiqueta = c(ROJO, "ERROR:") if critico else c(AMARILLO, "AVISO:")
            print("\n{} {} termino solo (codigo {}).".format(
                etiqueta, nombre, codigo), flush=True)
            lineas = _ultimas_lineas(h["log"])
            if lineas:
                print(c(GRIS, "--- ultimas lineas de {} ---".format(h["log"])))
                for l in lineas:
                    print(c(GRIS, "  " + l))

            if not critico:
                print(c(AMARILLO, "El sistema sigue sin {}.".format(nombre)),
                      flush=True)
                continue

            _fallo = True
            _thread.interrupt_main()    # despierta al hilo principal
            return
        time.sleep(0.5)


# =============================================================================
# Lo que llega del bus
# =============================================================================
def al_recibir(tema, datos):
    """Se llama una vez por cada mensaje que llega del bus."""
    partes = tema.split("/")
    if len(partes) < 3:
        return
    origen, tipo = partes[1], partes[2]

    if tipo == "estado":
        estado = datos.get("estado", "?")
        en_linea = estado == "en linea"
        # No repetir el mismo estado dos veces seguidas.
        if _conectados.get(origen) == en_linea:
            return
        _conectados[origen] = en_linea
        marca = c(VERDE, "CONECTADO") if en_linea else c(ROJO, "DESCONECTADO")
        print("{}  {}  {}".format(
            c(GRIS, hora_local()), c(NEGRITA, origen), marca), flush=True)
        return

    if tipo == "gps":
        mostrar_gps(origen, datos)
        return

    if tipo != "aforo":
        return

    aforo = datos.get("aforo")
    if aforo is None:
        return

    entradas = datos.get("entradas", 0)
    salidas = datos.get("salidas", 0)

    anterior = _ultimo.get(origen)
    if anterior == aforo:
        return

    if anterior is None:
        flecha = " "
    elif aforo > anterior:
        flecha = c(VERDE, "▲")      # triangulo arriba
    else:
        flecha = c(ROJO, "▼")       # triangulo abajo
    _ultimo[origen] = aforo

    print("{}  {}  {} AFORO {}   {}".format(
        c(GRIS, hora_local()),
        c(NEGRITA, "{:<10}".format(origen)),
        flecha,
        c(NEGRITA + ";" + AMARILLO, "{:>4}".format(aforo)),
        c(GRIS, "entradas {}  salidas {}".format(entradas, salidas)),
    ), flush=True)


def mostrar_gps(origen, datos):
    """Pinta la posicion. Solo cuando cambia algo que se ve en pantalla."""
    if not datos.get("fix"):
        motivo = datos.get("motivo") or "buscando satelites"
        # Con los satelites a la vista se ve si la busqueda avanza. Ademas
        # hace que la linea cambie cuando cambia la cuenta, asi que la
        # pantalla deja de parecer congelada mientras el receptor trabaja.
        sats = datos.get("sats")
        if sats is not None:
            motivo = "{}: {} a la vista".format(motivo, sats)
        if _ultimo_gps.get(origen) == motivo:
            return
        _ultimo_gps[origen] = motivo
        print("{}  {}  {}   {}".format(
            c(GRIS, hora_local()),
            c(NEGRITA, "{:<10}".format(origen)),
            c(AMARILLO, "GPS"),
            c(GRIS, "sin posicion ({})".format(motivo)),
        ), flush=True)
        return

    lat, lon = datos.get("lat"), datos.get("lon")
    if lat is None or lon is None:
        return

    # 6 decimales son ~0.1 m, muy por debajo del error real del receptor
    # (2.5 m CEP50). Mas cifras solo serian ruido en pantalla.
    posicion = "{:.6f}, {:.6f}".format(lat, lon)
    if _ultimo_gps.get(origen) == posicion:
        return
    _ultimo_gps[origen] = posicion

    detalle = []
    if datos.get("alt") is not None:
        detalle.append("alt {:.0f} m".format(datos["alt"]))
    if datos.get("sats"):
        detalle.append("sat {}".format(datos["sats"]))
    if datos.get("hdop") is not None:
        detalle.append("hdop {}".format(datos["hdop"]))
    if datos.get("vel"):
        detalle.append("{:.0f} km/h".format(datos["vel"]))

    print("{}  {}  {}   {}   {}".format(
        c(GRIS, hora_local()),
        c(NEGRITA, "{:<10}".format(origen)),
        c(AMARILLO, "GPS"),
        c(NEGRITA, posicion),
        c(GRIS, "  ".join(detalle)),
    ), flush=True)


def al_conectar():
    print(c(VERDE, "Escuchando el bus.") + c(GRIS, "  (la vision tarda un poco "
          "en arrancar la primera vez)\n"), flush=True)


# =============================================================================
def main():
    print("=" * 64)
    print(" CIT Mobility :: variables de la unidad  (aforo, GPS)")
    print(" Modelo    : {}".format(os.environ.get("MODELO", "peoplenet")))
    print(" Bus       : {}".format(bus.descripcion()))
    print(" Escuchando: {}".format(PATRON))

    if LANZAR:
        for prog in PROGRAMAS:
            hijo = lanzar(prog)
            if hijo is None:
                detener_todos()
                return 1
            _hijos.append(hijo)
            print(" Lanzado   : {}  (pid {}, log en {})".format(
                prog["nombre"], hijo["proceso"].pid, hijo["log"]))
        threading.Thread(target=vigilar, daemon=True).start()
    else:
        print(" Lanzado   : nada (LANZAR=0), solo escucho")

    print(" Ctrl+C para detener todo")
    print("=" * 64)

    try:
        bus.suscribir(PATRON, al_recibir, id_cliente="cit-monitor",
                      al_conectar=al_conectar)
    except KeyboardInterrupt:
        print("")
    except RuntimeError as e:
        sys.stderr.write("\n{}\n".format(e))
        detener_todos()
        return 1
    except OSError as e:
        sys.stderr.write(
            "\nERROR: no se pudo abrir el bus ({}): {}\n"
            "Con CIT_TRANSPORTE=mqtt comprueba que mosquitto este corriendo:\n"
            "  systemctl status mosquitto\n\n".format(bus.descripcion(), e))
        detener_todos()
        return 1
    finally:
        detener_todos()

    if _fallo:
        # Que se note el fallo hacia fuera: quien lance esto (una terminal,
        # systemd, otro script) debe poder distinguir un final sano de uno malo.
        print(c(ROJO, "Termino por un fallo en un programa critico."))
        return 1

    print("Listo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
