#!/usr/bin/env python3
# =============================================================================
# CIT Mobility / Edge Mobility
# cit_core/bus.py :: bus de mensajes compartido
#
#   Los programas que PRODUCEN datos (contadores, sensores) publican aqui.
#   Los programas que CONSUMEN datos (monitor, colector) se suscriben aqui.
#   Ninguno de los dos se conoce entre si.
#
#   Hay dos transportes. La API es identica en ambos, asi que cambiar de uno
#   a otro NO obliga a tocar los programas:
#
#     archivo  (por defecto)  No instala nada. Los eventos se escriben en
#                             datos/eventos.jsonl y quien escucha lee el
#                             archivo. Solo sirve dentro de la misma maquina,
#                             pero deja historial en disco de regalo.
#
#     mqtt                    Necesita mosquitto corriendo. Sirve entre varias
#                             maquinas y es instantaneo.
#
#   Se elige con la variable de entorno:
#       CIT_TRANSPORTE=archivo    (default)
#       CIT_TRANSPORTE=mqtt
#
#   Temas (topics), iguales en los dos transportes:
#       cit/<origen>/aforo     estado del aforo
#       cit/<origen>/estado    "en linea" / "desconectado"
#
# Regla de oro: si el bus falla, publicar NO debe romper ni frenar al programa
# que produce. Se pierde el dato, no el sistema de vision.
# =============================================================================

import os
import sys
import json
import time
import glob
import threading

# --- Parametros --------------------------------------------------------------
TRANSPORTE = os.environ.get("CIT_TRANSPORTE", "archivo").lower()

RAIZ = "cit"                                    # prefijo de los temas
_AQUI = os.path.dirname(os.path.abspath(__file__))
DIR_DATOS = os.environ.get("CIT_DATOS", os.path.join(os.path.dirname(_AQUI), "datos"))
ARCHIVO_EVENTOS = os.path.join(DIR_DATOS, "eventos.jsonl")
DIR_ESTADO = os.path.join(DIR_DATOS, "estado")

LATIDO = 5.0        # cada cuanto el productor dice "sigo vivo" (segundos)
PACIENCIA = 15.0    # sin latido durante este tiempo, se le da por caido
INTERVALO = 0.2     # cada cuanto el que escucha revisa el archivo

# Solo para el transporte mqtt
BROKER = os.environ.get("CIT_BROKER", "127.0.0.1")
PUERTO = int(os.environ.get("CIT_PUERTO", "1883"))

try:
    import paho.mqtt.client as mqtt
    HAY_MQTT = True
except ImportError:
    HAY_MQTT = False

AVISO_FALTA_PAHO = (
    "AVISO: CIT_TRANSPORTE=mqtt pero falta el modulo 'paho-mqtt'.\n"
    "       Instalalo con:  python3 -m pip install --user paho-mqtt\n"
    "       O usa el transporte por archivo:  CIT_TRANSPORTE=archivo\n"
)


# =============================================================================
# Utilidades comunes
# =============================================================================
def tema(origen, sufijo):
    """Arma el nombre del tema: cit/camion01/aforo"""
    return "{}/{}/{}".format(RAIZ, origen, sufijo)


def ahora():
    """Marca de tiempo UTC en formato ISO, sin microsegundos."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _origen_del_patron(patron):
    """De 'cit/+/#' saca None (cualquiera); de 'cit/camion01/#' saca el nombre."""
    partes = patron.split("/")
    if len(partes) < 2 or partes[1] in ("+", "#"):
        return None
    return partes[1]


def _asegurar_carpetas():
    os.makedirs(DIR_ESTADO, exist_ok=True)


# =============================================================================
# Transporte ARCHIVO -- lado que publica
# =============================================================================
class _PublicadorArchivo:
    """Escribe los eventos en datos/eventos.jsonl y su estado en datos/estado/.

    El registro de eventos solo crece con datos de verdad (cambios de aforo).
    El latido, que se repite cada pocos segundos, va a un archivo aparte que se
    sobrescribe, para no inflar el historial con ruido.
    """

    def __init__(self, origen, silencioso=False):
        self.origen = origen
        self.activo = False
        self.conectado = False
        self._parar = threading.Event()
        self._hilo = None
        self._ultimo = {}          # ultimo dato publicado, para el latido
        self._ultimo_tipo = None   # de que era ese dato: "aforo", "gps"...
        self._cerrojo = threading.Lock()
        self._archivo_estado = os.path.join(DIR_ESTADO, "{}.json".format(origen))

        try:
            _asegurar_carpetas()
            # Comprobar de verdad que se puede escribir, no suponerlo.
            with open(ARCHIVO_EVENTOS, "a"):
                pass
        except OSError as e:
            if not silencioso:
                print("AVISO: no se pudo abrir el bus en {} ({}). "
                      "El programa sigue sin publicar.".format(DIR_DATOS, e),
                      flush=True)
            return

        self.activo = True
        self.conectado = True      # en disco no hay enlace que negociar
        self._escribir_estado("en linea")

        self._hilo = threading.Thread(target=self._latir, daemon=True)
        self._hilo.start()

    def _escribir_estado(self, estado):
        """Sobrescribe el archivo de estado de forma atomica."""
        cuerpo = {"ts": ahora(), "origen": self.origen, "estado": estado}
        with self._cerrojo:
            cuerpo.update(self._ultimo)
            # El tipo viaja con el dato para que quien lea este archivo al
            # arrancar sepa de que variable es. Sin el habria que adivinarlo
            # mirando los campos, y eso se rompe al anadir la tercera.
            if self._ultimo_tipo:
                cuerpo["tipo"] = self._ultimo_tipo
            cuerpo["estado"] = estado
        temporal = self._archivo_estado + ".tmp"
        try:
            with open(temporal, "w") as f:
                json.dump(cuerpo, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporal, self._archivo_estado)   # atomico en Linux
        except OSError:
            pass

    def _latir(self):
        """Dice 'sigo vivo' cada pocos segundos hasta que el programa termina."""
        while not self._parar.wait(LATIDO):
            self._escribir_estado("en linea")

    def esperar_conexion(self, segundos=2.0):
        return self.conectado

    def publicar(self, sufijo, datos, retener=False):
        """Anade una linea JSON al registro de eventos. retener se ignora aqui:
        en disco todo queda escrito por naturaleza."""
        if not self.activo:
            return False

        evento = {"ts": ahora(), "origen": self.origen, "tipo": sufijo}
        evento.update(datos)

        try:
            # Una linea corta escrita en modo 'a' es atomica en Linux, asi que
            # varios productores a la vez no se pisan entre si.
            with open(ARCHIVO_EVENTOS, "a") as f:
                f.write(json.dumps(evento) + "\n")
                f.flush()
        except OSError:
            return False        # el bus jamas debe tumbar al productor

        if sufijo != "estado":
            with self._cerrojo:
                self._ultimo = dict(datos)
                self._ultimo_tipo = sufijo
            self._escribir_estado("en linea")
        return True

    def cerrar(self):
        if not self.activo:
            return
        self._parar.set()
        self._escribir_estado("desconectado")
        self.activo = False
        self.conectado = False


# =============================================================================
# Transporte ARCHIVO -- lado que escucha
# =============================================================================
def _suscribir_archivo(patron, al_recibir, al_conectar=None):
    """Sigue el registro de eventos y avisa de cada linea nueva.

    Al arrancar muestra primero el estado actual de cada origen, para que un
    monitor abierto tarde no se quede con la pantalla en blanco esperando el
    siguiente cruce.
    """
    filtro = _origen_del_patron(patron)
    _asegurar_carpetas()

    if al_conectar:
        al_conectar()

    vistos = {}         # origen -> ("en linea"/"desconectado", ts del archivo)

    def revisar_estados(inicial=False):
        """Lee datos/estado/*.json y deduce quien esta vivo y quien se cayo."""
        for ruta in glob.glob(os.path.join(DIR_ESTADO, "*.json")):
            origen = os.path.basename(ruta)[:-5]
            if filtro and origen != filtro:
                continue
            try:
                with open(ruta) as f:
                    datos = json.load(f)
                edad = time.time() - os.path.getmtime(ruta)
            except (OSError, ValueError):
                continue

            declarado = datos.get("estado", "?")
            # Si dice estar vivo pero lleva mucho sin latir, murio de golpe.
            if declarado == "en linea" and edad > PACIENCIA:
                declarado = "desconectado"

            if vistos.get(origen) != declarado:
                vistos[origen] = declarado
                al_recibir(tema(origen, "estado"),
                           {"ts": datos.get("ts", ahora()), "origen": origen,
                            "estado": declarado})
                # Al arrancar, ensenar tambien el ultimo valor conocido, sea de
                # la variable que sea: un monitor que abre tarde debe ver ya el
                # aforo Y la posicion, sin esperar al siguiente cambio.
                if inicial and declarado == "en linea":
                    ultimo_tipo = datos.get("tipo")
                    if ultimo_tipo and ultimo_tipo != "estado":
                        al_recibir(tema(origen, ultimo_tipo), datos)
                    elif "aforo" in datos:
                        # Archivos de estado escritos por una version anterior,
                        # que aun no guardaban el tipo.
                        al_recibir(tema(origen, "aforo"), datos)

    revisar_estados(inicial=True)

    # Seguir el archivo desde el final: lo viejo ya se mostro con los estados.
    posicion = os.path.getsize(ARCHIVO_EVENTOS) if os.path.exists(ARCHIVO_EVENTOS) else 0
    ultimo_repaso = 0.0

    while True:
        try:
            tam = os.path.getsize(ARCHIVO_EVENTOS)
        except OSError:
            tam = 0                     # el archivo aun no existe

        if tam < posicion:              # alguien lo vacio o lo rotó
            posicion = 0

        if tam > posicion:
            try:
                with open(ARCHIVO_EVENTOS) as f:
                    f.seek(posicion)
                    nuevas = f.readlines()
                    posicion = f.tell()
            except OSError:
                nuevas = []

            for linea in nuevas:
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    datos = json.loads(linea)
                except ValueError:
                    continue            # linea a medio escribir, se ignora
                origen = datos.get("origen", "?")
                if filtro and origen != filtro:
                    continue
                # Un origen que no habiamos visto: anunciarlo ya, sin esperar
                # al repaso periodico, o su CONECTADO saldria despues del dato.
                if origen not in vistos:
                    revisar_estados()
                al_recibir(tema(origen, datos.get("tipo", "aforo")), datos)

        # Los estados se repasan mas despacio: no cambian tan seguido.
        if time.time() - ultimo_repaso > 1.0:
            revisar_estados()
            ultimo_repaso = time.time()

        time.sleep(INTERVALO)


# =============================================================================
# Transporte MQTT
# =============================================================================
class _PublicadorMQTT:
    def __init__(self, origen, silencioso=False):
        self.origen = origen
        self.activo = False
        self.conectado = False
        self.cliente = None

        if not HAY_MQTT:
            if not silencioso:
                print(AVISO_FALTA_PAHO, end="", flush=True)
            return

        try:
            self.cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                       client_id="cit-{}-{}".format(origen, os.getpid()))
        except AttributeError:
            self.cliente = mqtt.Client(client_id="cit-{}-{}".format(origen, os.getpid()))

        def _conectado(cliente, userdata, flags, rc, propiedades=None):
            self.conectado = True
            # Al reconectar hay que reanunciarse: el testamento ya marco
            # este origen como desconectado en el broker.
            self.publicar("estado", {"estado": "en linea"}, retener=True)

        def _desconectado(cliente, userdata, *resto):
            self.conectado = False

        self.cliente.on_connect = _conectado
        self.cliente.on_disconnect = _desconectado

        # Testamento: si el proceso muere sin avisar, el broker publica
        # "desconectado" por el.
        self.cliente.will_set(
            tema(origen, "estado"),
            json.dumps({"ts": ahora(), "origen": origen, "estado": "desconectado"}),
            qos=1, retain=True,
        )
        self.cliente.reconnect_delay_set(min_delay=1, max_delay=30)

        try:
            self.cliente.connect_async(BROKER, PUERTO, keepalive=30)
        except OSError as e:
            if not silencioso:
                print("AVISO: no se pudo iniciar el bus MQTT ({}). "
                      "El programa sigue sin publicar.".format(e), flush=True)
            return

        # activo antes de arrancar el hilo: on_connect puede dispararse
        # de inmediato y necesita poder publicar el "en linea".
        self.activo = True
        self.cliente.loop_start()

    def esperar_conexion(self, segundos=2.0):
        """Da un margen al enlace para establecerse, solo para poder informar
        la verdad al arrancar. Aunque devuelva False, paho sigue reintentando."""
        if not self.activo:
            return False
        limite = time.time() + segundos
        while time.time() < limite and not self.conectado:
            time.sleep(0.05)
        return self.conectado

    def publicar(self, sufijo, datos, retener=False):
        if not self.activo:
            return False
        cuerpo = {"ts": ahora(), "origen": self.origen, "tipo": sufijo}
        cuerpo.update(datos)
        try:
            self.cliente.publish(tema(self.origen, sufijo), json.dumps(cuerpo),
                                 qos=0, retain=retener)
            return True
        except Exception:
            return False        # el bus jamas debe tumbar al productor

    def cerrar(self):
        if not self.activo:
            return
        self.publicar("estado", {"estado": "desconectado"}, retener=True)
        try:
            self.cliente.loop_stop()
            self.cliente.disconnect()
        except Exception:
            pass
        self.activo = False


def _suscribir_mqtt(patron, al_recibir, id_cliente, al_conectar=None):
    if not HAY_MQTT:
        raise RuntimeError(AVISO_FALTA_PAHO)

    nombre = "{}-{}".format(id_cliente, os.getpid())
    try:
        cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=nombre)
    except AttributeError:
        cliente = mqtt.Client(client_id=nombre)

    def _conectado(cliente, userdata, flags, rc, propiedades=None):
        cliente.subscribe(patron, qos=1)
        if al_conectar:
            al_conectar()

    def _mensaje(cliente, userdata, msg):
        try:
            datos = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        al_recibir(msg.topic, datos)

    cliente.on_connect = _conectado
    cliente.on_message = _mensaje
    cliente.reconnect_delay_set(min_delay=1, max_delay=30)
    cliente.connect(BROKER, PUERTO, keepalive=30)
    cliente.loop_forever()


# =============================================================================
# API publica -- lo unico que usan los programas
# =============================================================================
def Publicador(origen, silencioso=False):
    """Devuelve el publicador del transporte configurado.

    Uso:
        bus = Publicador("camion01")
        bus.publicar("aforo", {"aforo": 7, "entradas": 9, "salidas": 2})
        bus.cerrar()
    """
    if TRANSPORTE == "mqtt":
        return _PublicadorMQTT(origen, silencioso)
    return _PublicadorArchivo(origen, silencioso)


def suscribir(patron, al_recibir, id_cliente="cit-oyente", al_conectar=None):
    """Escucha el bus y llama a al_recibir(tema, datos) por cada mensaje.

    BLOQUEA: es para programas cuyo unico trabajo es escuchar.

    patron acepta comodines al estilo MQTT:
        cit/+/#            todo, venga de donde venga
        cit/camion01/#     solo ese origen
    """
    if TRANSPORTE == "mqtt":
        return _suscribir_mqtt(patron, al_recibir, id_cliente, al_conectar)
    return _suscribir_archivo(patron, al_recibir, al_conectar)


def descripcion():
    """Una linea legible sobre por donde viajan los datos. Para los banners."""
    if TRANSPORTE == "mqtt":
        return "mqtt {}:{}".format(BROKER, PUERTO)
    return "archivo {}".format(ARCHIVO_EVENTOS)
