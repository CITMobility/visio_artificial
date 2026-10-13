#!/usr/bin/env python3
# =============================================================================
# CIT Mobility / Edge Mobility
# lector_gps.py :: lee la posicion del modulo SIM7600G-H y la publica al bus
#
#   Es un PRODUCTOR, hermano de DeepStream-Yolo/contador_neto.py: no muestra
#   nada ni sabe quien le escucha, solo deja el dato en el bus.
#
#       [modulo SIM7600G-H]
#               |  NMEA a 1 Hz por /dev/ttyUSB*
#               v
#       cit_core/modulo_gps.py   encuentra el puerto (aunque cambie de numero)
#               |
#       cit_core/nmea.py         traduce las sentencias a numeros
#               |
#       cit_core/bus.py          publica  cit/<origen>/gps
#               |
#               v
#       variables.py             lo muestra en la terminal
#
#   Uso:
#       python3 lector_gps.py              # lo lanza variables.py por su cuenta
#       python3 lector_gps.py --verbose    # ademas imprime cada posicion
#       python3 lector_gps.py --sin-configurar
#
#   Variables de entorno:
#       ORIGEN_GPS=gps01       nombre con el que se anuncia en el bus
#       GPS_INTERVALO=5        segundos entre publicaciones estando quieto
#       GPS_METROS=10          si se mueve mas que esto, publica sin esperar
#       CIT_GPS_NMEA=/dev/...  forzar el puerto NMEA (salta la deteccion)
#       CIT_GPS_AT=/dev/...    forzar el puerto de comandos AT
#       CIT_GPS_ACTUAL=/dev/shm/cit/gps_actual.json
#                              ultima lectura, reescrita a cada sentencia
#
#   POSICION AL SEGUNDO (24-sep-2026). Ademas del bus, cada lectura se deja
#   en CIT_GPS_ACTUAL, un archivo en memoria (tmpfs) que se sobrescribe. Lo
#   lee agente.py para armar la telemetria a 1 Hz con la unidad en marcha
#   (ADR-001) sin inflar eventos.jsonl ni cambiar lo que va al bus o a AWS:
#   el bus sigue recibiendo lo mismo de siempre (cada 5 s o cada 10 m).
#
#   REGLA QUE MANDA SOBRE TODO LO DEMAS: este programa no puede morirse.
#   Lo lanza variables.py, que apaga el sistema entero si un hijo termina
#   solo. Si se cayera al desenchufar el GPS, se llevaria por delante el
#   conteo de aforo, que es lo importante. Por eso ningun fallo de hardware
#   es mortal aqui: se avisa, se espera y se vuelve a intentar.
# =============================================================================

import os
import sys
import json
import math
import time
import fcntl
import signal

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

from cit_core import bus, nmea, serie          # noqa: E402
from cit_core import modulo_gps                # noqa: E402

# --- Parametros ---------------------------------------------------------------
ORIGEN = os.environ.get("ORIGEN_GPS", "gps01")

# Cada cuanto se publica estando quieto. El modulo da 1 Hz, pero publicar cada
# segundo llenaria el historial de eventos.jsonl con la misma coordenada.
INTERVALO = float(os.environ.get("GPS_INTERVALO", "5.0"))

# ...salvo que la unidad se mueva: entonces se publica al momento, para no
# perder maniobras entre punto y punto.
METROS = float(os.environ.get("GPS_METROS", "10.0"))

# Reintento creciente al perder el modulo: empieza rapido por si fue un
# tropiezo y se va calmando para no castigar el sistema si no vuelve.
ESPERA_MINIMA = 2.0
ESPERA_MAXIMA = 30.0

# Sin una sola sentencia valida en este tiempo, se da el puerto por perdido y
# se vuelve a buscar el modulo desde cero (puede haber cambiado de ttyUSB).
SILENCIO_MAXIMO = 20.0

# Ultima lectura para agente.py. En /dev/shm para no gastar el disco con una
# escritura por segundo todo el dia.
GPS_ACTUAL = os.environ.get("CIT_GPS_ACTUAL", "/dev/shm/cit/gps_actual.json")

VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv
CONFIGURAR = "--sin-configurar" not in sys.argv

_seguir = True


# =============================================================================
# Utilidades
# =============================================================================
def aviso(texto):
    """Todo lo que se imprime va al log del proceso (datos/gps.log)."""
    print("{}  {}".format(time.strftime("%H:%M:%S"), texto), flush=True)


def distancia(lat1, lon1, lat2, lon2):
    """Metros entre dos coordenadas (haversine).

    Solo se usa para decidir si publicar ya o esperar, asi que sobra de largo
    con la aproximacion de tierra esferica.
    """
    radio = 6371000.0
    fi1, fi2 = math.radians(lat1), math.radians(lat2)
    dfi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dfi / 2) ** 2 +
         math.cos(fi1) * math.cos(fi2) * math.sin(dlambda / 2) ** 2)
    return 2 * radio * math.asin(min(1.0, math.sqrt(a)))


def dejar_actual(foto):
    """Sobrescribe la ultima lectura en GPS_ACTUAL, de forma atomica.

    Nunca falla hacia afuera: si no se puede escribir, el bus sigue igual.
    """
    try:
        os.makedirs(os.path.dirname(GPS_ACTUAL), exist_ok=True)
        cuerpo = dict(foto)
        cuerpo["ts"] = bus.ahora()
        temporal = GPS_ACTUAL + ".tmp"
        with open(temporal, "w") as f:
            json.dump(cuerpo, f)
        os.replace(temporal, GPS_ACTUAL)
    except (OSError, ValueError):
        pass


def _parar(*resto):
    """Ctrl+C o SIGTERM: salir del bucle por las buenas."""
    global _seguir
    _seguir = False


def tomar_candado():
    """Impide que corran dos lectores a la vez. Devuelve el archivo o None.

    Dos procesos leyendo el mismo puerto serie NO dan error: se reparten los
    bytes al azar y los dos ven sentencias NMEA partidas por la mitad. El
    sintoma es desconcertante (posiciones que aparecen y desaparecen solas) y
    no se parece en nada a la causa, asi que mejor hacerlo imposible.

    Pasa de verdad: variables.py lanza este programa, y basta con que alguien
    abra otro a mano para depurar.
    """
    try:
        os.makedirs(bus.DIR_DATOS, exist_ok=True)
        candado = open(os.path.join(bus.DIR_DATOS, "gps.lock"), "w")
    except OSError:
        return True          # sin poder crear el candado, mejor seguir que parar

    try:
        fcntl.flock(candado.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        candado.close()
        return None
    # El candado se suelta solo cuando el proceso muere, incluso a SIGKILL.
    return candado


# =============================================================================
# Lectura del modulo
# =============================================================================
class Posicion:
    """Junta lo que dicen las distintas sentencias en un solo dato.

    Hace falta porque la posicion viene en GGA y la velocidad en RMC: son dos
    lineas distintas del mismo instante. Se acumulan y se publica cuando la
    foto esta completa.
    """

    def __init__(self):
        self.datos = {}
        self.hay_fix = False

    def actualizar(self, leido):
        if leido.get("fix"):
            self.hay_fix = True
            self.datos.update({k: v for k, v in leido.items() if k != "fix"})
            return "lat" in self.datos
        # RMC/GGA diciendo que no hay fix: se pierde la posicion, no se
        # conserva la ultima. Publicar una coordenada vieja como si fuera
        # actual seria mentir sobre donde esta la unidad. Antes el diccionario
        # se quedaba con la coordenada anterior y viajaba al bus junto a
        # fix=false; ahora se vacia de verdad.
        if "lat" in leido or leido.get("sats") is not None:
            self.hay_fix = False
            # Lo unico que se conserva sin fix son los satelites a la vista.
            # Es la diferencia entre "esta a punto de enganchar" y "la antena
            # no recibe nada", y sin ese numero no hay forma de saber si
            # merece la pena seguir esperando o hay que ir a revisar el cable.
            sats = leido.get("sats")
            self.datos = {} if sats is None else {"sats": sats}
        return False

    def instantanea(self):
        foto = dict(self.datos)
        foto["fix"] = self.hay_fix
        return foto


def escuchar(ruta_nmea, publicar):
    """Lee el flujo NMEA hasta que se acabe o falle. Puede lanzar OSError."""
    posicion = Posicion()
    ultimo_dato = time.monotonic()

    with serie.Puerto(ruta_nmea) as puerto:
        while _seguir:
            linea = puerto.leer_linea(1.0)

            if linea is None:
                # Puerto abierto pero mudo: el modulo pudo reiniciarse o el
                # NMEA estar apagado. Mejor volver a buscarlo que quedarse
                # esperando eternamente un dato que ya no va a llegar.
                if time.monotonic() - ultimo_dato > SILENCIO_MAXIMO:
                    raise OSError(
                        "{} lleva {:.0f} s sin decir nada".format(
                            ruta_nmea, SILENCIO_MAXIMO))
                continue

            leido = nmea.analizar(linea)
            if leido is None:
                continue                # sentencia que no interesa o corrupta

            ultimo_dato = time.monotonic()
            posicion.actualizar(leido)
            publicar(posicion.instantanea())


# =============================================================================
# Publicacion
# =============================================================================
class Emisor:
    """Decide cuando merece la pena publicar y lo hace."""

    def __init__(self, canal):
        self.canal = canal
        self.ultimo_envio = 0.0
        self.ultima_lat = None
        self.ultima_lon = None
        self.ultimo_fix = None

    def __call__(self, foto):
        dejar_actual(foto)
        ahora = time.monotonic()
        fix = foto.get("fix", False)

        # Ganar o perder el fix se publica al instante: es un cambio de estado
        # del sistema, no una lectura mas.
        cambio_de_fix = fix != self.ultimo_fix

        movido = False
        if fix and self.ultima_lat is not None and "lat" in foto:
            movido = distancia(self.ultima_lat, self.ultima_lon,
                               foto["lat"], foto["lon"]) > METROS

        vencido = (ahora - self.ultimo_envio) >= INTERVALO

        if not (cambio_de_fix or movido or vencido):
            return
        if fix and "lat" not in foto:
            return                      # aun no ha llegado el GGA con posicion

        self.publicar(foto)
        self.ultimo_envio = ahora
        self.ultimo_fix = fix
        if fix and "lat" in foto:
            self.ultima_lat = foto["lat"]
            self.ultima_lon = foto["lon"]

    def publicar(self, foto):
        self.canal.publicar("gps", foto)
        if VERBOSE:
            if foto.get("fix") and "lat" in foto:
                aviso("{:.7f}, {:.7f}   alt {}  sat {}  hdop {}".format(
                    foto["lat"], foto["lon"], foto.get("alt"),
                    foto.get("sats"), foto.get("hdop")))
            else:
                aviso("sin fix (satelites a la vista: {})".format(
                    foto.get("sats", 0)))


    def sin_modulo(self, motivo):
        """Publica que no hay GPS. Que el silencio no se confunda con estar
        parado en el sitio: quien escuche tiene que poder distinguirlo."""
        dejar_actual({"fix": False, "motivo": motivo})
        if self.ultimo_fix is False and (time.monotonic() - self.ultimo_envio) < INTERVALO:
            return
        self.canal.publicar("gps", {"fix": False, "motivo": motivo})
        self.ultimo_envio = time.monotonic()
        self.ultimo_fix = False
        self.ultima_lat = self.ultima_lon = None


# =============================================================================
def main():
    signal.signal(signal.SIGINT, _parar)
    signal.signal(signal.SIGTERM, _parar)

    candado = tomar_candado()
    if candado is None:
        aviso("ya hay otro lector GPS en marcha; este no arranca.")
        aviso("(dos procesos leyendo el mismo puerto se roban los bytes)")
        return 0        # no es un fallo: el dato ya lo esta publicando el otro

    canal = bus.Publicador(ORIGEN)
    emisor = Emisor(canal)

    aviso("lector GPS :: origen '{}', bus {}".format(ORIGEN, bus.descripcion()))
    aviso("publica cada {:.0f} s, o antes si se mueve mas de {:.0f} m".format(
        INTERVALO, METROS))

    espera = ESPERA_MINIMA
    configurado = False

    try:
        while _seguir:
            try:
                nmea_ruta, at_ruta, como = modulo_gps.localizar()
            except modulo_gps.ModuloNoEncontrado as e:
                emisor.sin_modulo("sin modulo")
                aviso("{}".format(e))
                aviso("reintento en {:.0f} s".format(espera))
                _dormir(espera)
                espera = min(espera * 2, ESPERA_MAXIMA)
                continue

            aviso("modulo en NMEA={} AT={} (localizado por {})".format(
                nmea_ruta, at_ruta, como))

            # Encender el GNSS una sola vez por sesion. Si el puerto AT no esta
            # disponible (lo tipico: ModemManager lo tiene tomado) NO se aborta:
            # con AT+CGPSAUTO=1 ya grabado en el modulo, el GNSS arranca solo y
            # el NMEA sale igual. El puerto AT es un lujo, no un requisito.
            if CONFIGURAR and not configurado and at_ruta:
                aviso("configurando el GNSS por {}:".format(at_ruta))
                configurado = modulo_gps.configurar(at_ruta, registrar=aviso)
            elif CONFIGURAR and not at_ruta and not configurado:
                aviso("sin puerto AT disponible; se lee el NMEA tal como venga")
                aviso("(" + modulo_gps.AVISO_MODEMMANAGER.splitlines()[0] + ")")

            if not nmea_ruta:
                emisor.sin_modulo("sin flujo NMEA")
                aviso("hay modulo pero no sale NMEA; reintento en {:.0f} s".format(espera))
                _dormir(espera)
                espera = min(espera * 2, ESPERA_MAXIMA)
                continue

            try:
                espera = ESPERA_MINIMA          # llego a leer: cuenta a cero
                escuchar(nmea_ruta, emisor)
            except OSError as e:
                # Desenchufaron el USB, o el modulo se reinicio. Se vuelve
                # arriba y se busca otra vez: al reconectar puede aparecer con
                # OTRO numero de ttyUSB, asi que hay que rehacer la deteccion
                # entera, no reabrir la misma ruta.
                if not _seguir:
                    break
                emisor.sin_modulo("se perdio el puerto")
                aviso("se perdio la lectura: {}".format(e))
                aviso("vuelvo a buscar el modulo en {:.0f} s".format(espera))
                _dormir(espera)
                espera = min(espera * 2, ESPERA_MAXIMA)
    finally:
        canal.cerrar()
        aviso("lector GPS detenido.")

    return 0


def _dormir(segundos):
    """Espera troceada, para que Ctrl+C no tarde en hacerse notar."""
    limite = time.monotonic() + segundos
    while _seguir and time.monotonic() < limite:
        time.sleep(0.2)


if __name__ == "__main__":
    sys.exit(main())
