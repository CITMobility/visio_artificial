#!/usr/bin/env python3
# =============================================================================
# CIT Mobility / Edge Mobility
# cit_core/modulo_gps.py :: encontrar y encender el GNSS del SIM7600G-H
#
#   El problema que resuelve este archivo: el modulo entrega cinco puertos
#   /dev/ttyUSB0..4 y los que importan son dos, pero SU NUMERO NO ES FIJO.
#   Depende del orden de arranque, de si hay otro USB serie conectado y del
#   puerto fisico donde se enchufe. Dar por hecho que el GPS esta en
#   /dev/ttyUSB1 funciona en el banco y falla en la unidad.
#
#   Lo que NO cambia es el numero de interfaz USB dentro del modulo:
#
#       interfaz 03  ->  flujo NMEA continuo  (de aqui salen las coordenadas)
#       interfaz 04  ->  comandos AT          (por aqui se enciende el GNSS)
#
#   Asi que se busca por interfaz, no por nombre de dispositivo.
#
#   Tres niveles, cada uno cubriendo el fallo del anterior:
#
#       1. sysfs      lee /sys y empareja idVendor + bInterfaceNumber.
#                     Instantaneo y sin tocar los puertos. El caso normal.
#       2. sondeo     abre cada /dev/ttyUSB* y /dev/ttyACM* y pregunta:
#                     ¿sale NMEA por aqui? ¿responde OK a un AT?
#                     Cubre un modulo con otro VID o con otro mapeo.
#       3. entorno    CIT_GPS_NMEA / CIT_GPS_AT saltan toda la deteccion.
#                     Escotilla manual para banco de pruebas.
#
#   Comprobar que detecta bien, sin arrancar el sistema entero:
#
#       python3 -m cit_core.modulo_gps
#
# =============================================================================

import os
import glob

from . import serie

# --- Que se busca -------------------------------------------------------------
# SimTech, el fabricante del SIM7600G-H. El PID cambia segun la composicion USB
# activa (9011 con RNDIS, 9001 estandar) y por eso NO se filtra por el: en ambas
# composiciones las funciones serie caen en las mismas interfaces. Filtrar por
# PID seria volver a atarse a algo que el modulo puede cambiar solo.
VENDEDOR = "1e0e"

INTERFAZ_NMEA = 3
INTERFAZ_AT = 4

# Los puertos que se miran cuando toca sondear, en orden.
PATRONES = ("/dev/ttyUSB*", "/dev/ttyACM*")

# Secuencia de encendido del GNSS. Ver la guia de extraccion de datos GPS.
COMANDOS_ENCENDIDO = [
    ("AT", "responde el puerto"),
    ("AT+AUTOCSQ=0", "callar los reportes de senal"),
    ("AT+CGPS=1,1", "GNSS en modo standalone"),
    ("AT+CGPSNMEAPORTCFG=3", "sacar NMEA por el USB"),
    ("AT+CGPSNMEA=197119", "habilitar todas las sentencias"),
    ("AT+CGPSINFOCFG=10,31", "reportar solo cada 1 s"),
    ("AT+CGPSAUTO=1", "arrancar el GNSS al encender"),
]


class ModuloNoEncontrado(Exception):
    """No hay modulo GPS al alcance. Es una situacion esperable (cable suelto,
    modulo sin alimentar), no un error de programa: quien llama debe reintentar,
    no abortar."""


AVISO_MODEMMANAGER = (
    "el puerto AT esta ocupado, casi seguro por ModemManager (el servicio de "
    "Ubuntu que gestiona modems: toma el SIM7600 al detectarlo). No estorba "
    "mientras el GNSS ya este encendido, porque el flujo NMEA sale por otro "
    "puerto que si queda libre. Solo si hace falta mandar comandos AT:\n"
    "    sudo systemctl disable --now ModemManager\n"
    "  o excluir el modulo con una regla de udev (ENV{ID_MM_DEVICE_IGNORE}=\"1\")."
)


# =============================================================================
# Nivel 1 -- por sysfs
# =============================================================================
def _interfaz_de(nombre):
    """Del nombre 'ttyUSB1' saca (vendedor, producto, numero de interfaz).

    La ruta de sysfs es:
        /sys/bus/usb-serial/devices/ttyUSB1 -> .../1-2.2.1:1.3/ttyUSB1
                                                        ^^^^^^^ la interfaz
    El padre de la interfaz es el dispositivo USB, que es quien lleva el
    idVendor y el idProduct.
    """
    enlace = os.path.join("/sys/bus/usb-serial/devices", nombre)
    try:
        interfaz = os.path.dirname(os.path.realpath(enlace))
    except OSError:
        return None

    def leer(ruta):
        try:
            with open(ruta) as f:
                return f.read().strip()
        except OSError:
            return None

    numero = leer(os.path.join(interfaz, "bInterfaceNumber"))
    dispositivo = os.path.dirname(interfaz)
    vendedor = leer(os.path.join(dispositivo, "idVendor"))
    producto = leer(os.path.join(dispositivo, "idProduct"))

    if numero is None or vendedor is None:
        return None
    try:
        return vendedor.lower(), (producto or "").lower(), int(numero, 16)
    except ValueError:
        return None


def _buscar_por_sysfs():
    """Devuelve (nmea, at); cualquiera de los dos puede venir a None."""
    nmea = at = None
    try:
        nombres = sorted(os.listdir("/sys/bus/usb-serial/devices"))
    except OSError:
        return None, None

    for nombre in nombres:
        datos = _interfaz_de(nombre)
        if datos is None:
            continue
        vendedor, _producto, interfaz = datos
        if vendedor != VENDEDOR:
            continue
        ruta = "/dev/" + nombre
        if interfaz == INTERFAZ_NMEA:
            nmea = ruta
        elif interfaz == INTERFAZ_AT:
            at = ruta

    return nmea, at


# =============================================================================
# Nivel 2 -- sondeando los puertos
# =============================================================================
def _huele_a_nmea(ruta, espera=2.0):
    """¿Salen sentencias NMEA por aqui? Solo escucha, no escribe nada."""
    try:
        with serie.Puerto(ruta) as p:
            for _ in range(40):
                linea = p.leer_linea(espera / 4.0)
                if linea is None:
                    continue
                # Basta el prefijo: $GP, $GL, $BD, $GN segun la constelacion.
                if linea.startswith("$G") or linea.startswith("$BD"):
                    return True
    except OSError:
        return False
    return False


def _responde_at(ruta, espera=1.5):
    """¿Contesta OK a un AT pelado? Entonces es el puerto de comandos."""
    try:
        with serie.Puerto(ruta) as p:
            p.vaciar()
            p.escribir("AT")
            for _ in range(10):
                linea = p.leer_linea(espera / 5.0)
                if linea is None:
                    continue
                if linea.strip() == "OK":
                    return True
                # Los puertos de datos (PPP) devuelven ERROR o nada; el de
                # diagnostico suele no contestar. Cualquier respuesta AT
                # valida sirve para identificarlo.
                if linea.startswith("+CME ERROR") or linea.strip() == "ERROR":
                    return True
    except OSError:
        return False
    return False


def _buscar_por_sondeo(saltar=()):
    """Prueba puerto por puerto. Lento (segundos), pero no depende de sysfs."""
    nmea = at = None
    candidatos = []
    for patron in PATRONES:
        candidatos.extend(sorted(glob.glob(patron)))

    # El NMEA primero: se detecta solo escuchando, sin escribir en un puerto
    # que quiza no sea del modulo.
    for ruta in candidatos:
        if ruta in saltar:
            continue
        if _huele_a_nmea(ruta):
            nmea = ruta
            break

    for ruta in candidatos:
        if ruta == nmea or ruta in saltar:
            continue
        if _responde_at(ruta):
            at = ruta
            break

    return nmea, at


# =============================================================================
# API publica
# =============================================================================
def localizar(sondear=True):
    """Encuentra los dos puertos del modulo. Lanza ModuloNoEncontrado si no hay.

    Devuelve (ruta_nmea, ruta_at, como) donde 'como' explica por que nivel se
    resolvio, para que quede en el log cuando algo vaya raro en la unidad.
    """
    # Nivel 3: lo que diga el entorno manda sobre todo lo demas.
    forzado_nmea = os.environ.get("CIT_GPS_NMEA")
    forzado_at = os.environ.get("CIT_GPS_AT")
    if forzado_nmea and os.path.exists(forzado_nmea):
        return forzado_nmea, (forzado_at or None), "entorno"

    # Nivel 1.
    nmea, at = _buscar_por_sysfs()
    if nmea and os.path.exists(nmea):
        return nmea, at, "sysfs"

    # Nivel 2. Cuesta unos segundos, asi que solo si el anterior no dio nada.
    if sondear:
        nmea, at = _buscar_por_sondeo()
        if nmea:
            return nmea, at, "sondeo"
        if at:
            # Hay modulo (contesta AT) pero aun no saca NMEA: probablemente el
            # GNSS esta apagado. Se devuelve para poder encenderlo.
            return None, at, "sondeo"

    raise ModuloNoEncontrado(
        "no se encontro el modulo GPS. Comprueba el LED PWR de la tarjeta "
        "(se alimenta por el header de 40 pines, NO por el micro USB) y que "
        "'lsusb' muestre el dispositivo {}.".format(VENDEDOR))


def configurar(ruta_at, registrar=None):
    """Enciende el GNSS y deja el flujo NMEA saliendo por el USB.

    Devuelve True si el modulo respondio. No lanza excepcion si algun comando
    falla: varios devuelven error cuando el GNSS YA estaba encendido, que es lo
    normal en un equipo que lleva rato en marcha, y eso no es un fallo.
    """
    if not ruta_at:
        return False

    def anotar(texto):
        if registrar:
            registrar(texto)

    try:
        with serie.Puerto(ruta_at) as p:
            respondio = False
            for comando, motivo in COMANDOS_ENCENDIDO:
                p.vaciar()
                p.escribir(comando)
                resultado = _esperar_respuesta(p)
                if resultado is not None:
                    respondio = True
                anotar("  {:<24} {:<32} {}".format(
                    comando, motivo, resultado or "sin respuesta"))
            if respondio:
                # Que algun comando conteste ERROR es normal y NO es fallo: los
                # de la familia CGPS lo devuelven cuando el GNSS ya estaba
                # encendido o cuando ese ajuste ya estaba puesto. Lo que
                # importa es que el modulo conteste.
                anotar("  (los ERROR de arriba son normales si el GNSS ya "
                       "estaba configurado)")
            return respondio
    except OSError as e:
        anotar("  no se pudo configurar por {}: {}".format(ruta_at, e))
        # EBUSY aqui es el caso mas comun de todos y despista mucho si no se
        # explica: el modulo esta perfectamente, solo que otro proceso llego
        # antes al puerto.
        if getattr(e, "errno", None) == 16 or "busy" in str(e).lower():
            anotar("  AVISO: " + AVISO_MODEMMANAGER)
        return False


def _esperar_respuesta(puerto, espera=3.0):
    """Lee hasta el OK o el ERROR de un comando AT.

    'operation not supported' se cuenta como exito a proposito: es lo que
    contesta AT+CGPS=1,1 cuando el GNSS ya estaba encendido.
    """
    import time
    limite = time.monotonic() + espera
    while time.monotonic() < limite:
        linea = puerto.leer_linea(0.5)
        if not linea:
            continue
        texto = linea.strip()
        if not texto or texto.startswith("+CSQ:"):
            continue                # eco y reportes de senal: ruido
        if texto == "OK":
            return "OK"
        if "operation not supported" in texto.lower():
            return "ya estaba encendido"
        if texto.startswith("+CME ERROR") or texto == "ERROR":
            return texto
    return None


# =============================================================================
# Comprobacion manual
# =============================================================================
if __name__ == "__main__":
    print("Buscando el modulo GPS...\n")

    print("Nivel 1 (sysfs):")
    n, a = _buscar_por_sysfs()
    print("  NMEA = {}\n  AT   = {}\n".format(n, a))

    try:
        nmea, at, como = localizar()
    except ModuloNoEncontrado as e:
        print("NO ENCONTRADO: {}".format(e))
        raise SystemExit(1)

    print("Resultado: NMEA={}  AT={}  (por {})\n".format(nmea, at, como))

    if nmea:
        print("Escuchando 5 s en {}:".format(nmea))
        try:
            with serie.Puerto(nmea) as p:
                for _ in range(6):
                    linea = p.leer_linea(1.0)
                    if linea:
                        print("  " + linea[:80])
        except OSError as e:
            print("  no se pudo leer: {}".format(e))
