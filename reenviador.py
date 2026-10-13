#!/usr/bin/env python3
"""
CIT Mobility, S.A.P.I. de C.V.
Edge Mobility. Reenviador de telemetria hacia el broker MQTT.

Sigue datos/eventos.jsonl y publica cada evento al broker. Guarda su posicion
de lectura en disco y solo la avanza cuando el broker confirma la entrega, asi
que un tunel, un corte de luz o un reinicio no pierden datos.

Funciona igual contra Mosquitto local, EMQX o AWS IoT Core. Cambiar de uno a
otro es cambiar variables de entorno, no codigo.

    Local sin TLS (pruebas):
        CIT_UNIDAD=cit-001 CIT_BROKER=127.0.0.1 CIT_PUERTO=1883 CIT_TLS=0 \
        python3 reenviador.py

    AWS IoT Core:
        CIT_UNIDAD=cit-001 \
        CIT_BROKER=xxxxx-ats.iot.us-east-1.amazonaws.com \
        CIT_CA=certs/AmazonRootCA1.pem \
        CIT_CERT=certs/cit-001.cert.pem \
        CIT_LLAVE=certs/cit-001.private.key \
        python3 reenviador.py

Integracion en el sistema: se agrega como una entrada mas a PROGRAMAS en
variables.py, con "critico": False. Si el reenviador muere, el conteo y el GPS
siguen operando y los eventos se acumulan en el archivo para enviarse despues.

    VM propia (Mosquitto con usuario), recibiendo ordenes de video:
        CIT_UNIDAD=cit-001 CIT_BROKER=<ip-de-la-vm> CIT_PUERTO=1883 CIT_TLS=0 \
        CIT_USUARIO=cit-001 CIT_CONTRASENA=... \
        CIT_ORDENES=1 CIT_CLAVE_LOCAL=<CLAVE del contador> \
        CIT_DESPLAZAMIENTO=datos/reenvio_vm.pos \
        python3 reenviador.py

Garantia de entrega: al menos una vez. Si el proceso muere entre la
confirmacion del broker y la escritura de la posicion, ese mensaje se reenvia
al arrancar. Es el comportamiento correcto para QoS 1 y quien consume debe
tolerar duplicados. Nunca se pierde un evento.

Varios destinos a la vez: se corre una instancia por destino, cada una con su
propio CIT_DESPLAZAMIENTO. Cada una lleva su propia cuenta de lo confirmado,
asi que una nube caida no frena a la otra.

ORDENES (CIT_ORDENES=1, v1.1 del 22-sep-2026)
    Ademas de publicar, se suscribe a cit/v1/<unidad>/cmd y pasa las ordenes
    al contador por su HTTP local. Contesta en cit/v1/<unidad>/respuesta.

        {"id":"a1","orden":"video_abrir","ts":"2026-09-22T20:00:00Z"}
        {"id":"a2","orden":"video_cerrar"}
        {"id":"a3","orden":"video_estado"}

    La respuesta lleva el mismo id, "ok" y, si fallo, "motivo". Una orden con
    ts mas viejo que CIT_VIGENCIA_ORDEN segundos se rechaza: tras un corte de
    red no debe abrirse video que alguien pidio hace diez minutos.

    El reenviador NO decide nada del video. La ventana de 60 s y la cuota
    diaria las impone el contador, que es quien gasta los datos.

Requisito: pip3 install paho-mqtt
"""

import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("Falta paho-mqtt. Instalalo con: pip3 install paho-mqtt")


# --- Configuracion -----------------------------------------------------------
AQUI = os.path.dirname(os.path.abspath(__file__))

UNIDAD = os.environ.get("CIT_UNIDAD", "cit-001")
BROKER = os.environ.get("CIT_BROKER", "127.0.0.1")
PUERTO = int(os.environ.get("CIT_PUERTO", "8883"))
USAR_TLS = os.environ.get("CIT_TLS", "1") == "1"
CA = os.environ.get("CIT_CA", "")
CERT = os.environ.get("CIT_CERT", "")
LLAVE = os.environ.get("CIT_LLAVE", "")
# Usuario y contrasena del broker. AWS IoT no los usa (se autentica con el
# certificado); Mosquitto en la VM si.
USUARIO = os.environ.get("CIT_USUARIO", "")
CONTRASENA = os.environ.get("CIT_CONTRASENA", "")

# --- Ordenes desde la nube ---
ORDENES = os.environ.get("CIT_ORDENES", "0") == "1"
CONTADOR_URL = os.environ.get("CIT_CONTADOR", "http://127.0.0.1:8080")
CLAVE_LOCAL = os.environ.get("CIT_CLAVE_LOCAL", "")
VIGENCIA_ORDEN = float(os.environ.get("CIT_VIGENCIA_ORDEN", "30"))
# Orden de la nube -> (metodo, ruta del contador)
RUTAS_ORDEN = {
    "video_abrir": ("POST", "/video/abrir"),
    "video_cerrar": ("POST", "/video/cerrar"),
    "video_estado": ("GET", "/estado"),
}

EVENTOS = os.environ.get("CIT_EVENTOS", os.path.join(AQUI, "datos", "eventos.jsonl"))
POSICION = os.environ.get("CIT_DESPLAZAMIENTO", os.path.join(AQUI, "datos", "reenvio.pos"))

PREFIJO = "cit/v1"
INTERVALO = 0.2          # cada cuanto se revisa el archivo
ESPERA_PUBACK = 15.0     # segundos maximos esperando confirmacion del broker
RESUMEN = 60.0           # cada cuanto se imprime el resumen
ESPERAS = [2, 5, 10, 20, 30, 60]   # espera creciente al reconectar

# Tipos que se aceptan como tema. Un tipo nuevo funciona sin tocar este archivo
# mientras respete esta forma, que es la regla de portabilidad del tema.
import re
TIPO_VALIDO = re.compile(r"^[a-z][a-z0-9_]{0,19}$")

_parar = False


def ahora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def aviso(txt):
    print("[{}] {}".format(time.strftime("%H:%M:%S"), txt), flush=True)


# --- Posicion de lectura -----------------------------------------------------
class Posicion:
    """Guarda donde se quedo la lectura. Sobrevive a reinicios."""

    def __init__(self, ruta):
        self.ruta = ruta
        self.inodo = 0
        self.pos = 0
        self._cargar()

    def _cargar(self):
        try:
            with open(self.ruta, encoding="utf-8") as f:
                d = json.load(f)
            self.inodo = int(d.get("inodo", 0))
            self.pos = int(d.get("pos", 0))
        except Exception:
            self.inodo, self.pos = 0, 0

    def guardar(self):
        os.makedirs(os.path.dirname(self.ruta) or ".", exist_ok=True)
        tmp = self.ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"inodo": self.inodo, "pos": self.pos}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.ruta)


# --- Lectura del archivo de eventos -----------------------------------------
class Cola:
    """Lee eventos.jsonl desde la posicion guardada, linea completa por linea."""

    def __init__(self, ruta, posicion):
        self.ruta = ruta
        self.p = posicion

    def _revisar_archivo(self):
        """Detecta rotacion o truncado y reinicia la posicion si hace falta."""
        try:
            st = os.stat(self.ruta)
        except FileNotFoundError:
            return False
        if self.p.inodo and st.st_ino != self.p.inodo:
            aviso("El archivo de eventos cambio de inodo. Se relee desde el inicio.")
            self.p.inodo, self.p.pos = st.st_ino, 0
            self.p.guardar()
        elif st.st_size < self.p.pos:
            aviso("El archivo de eventos encogio. Se relee desde el inicio.")
            self.p.pos = 0
            self.p.guardar()
        if not self.p.inodo:
            self.p.inodo = st.st_ino
        return True

    def pendientes(self, maximo=500):
        """Devuelve [(nueva_posicion, evento_o_None)] de lineas COMPLETAS."""
        if not self._revisar_archivo():
            return []
        salida = []
        try:
            with open(self.ruta, "rb") as f:
                f.seek(self.p.pos)
                for cruda in f:
                    if not cruda.endswith(b"\n"):
                        break               # linea a medio escribir, se espera
                    nueva = f.tell()
                    texto = cruda.decode("utf-8", errors="ignore").strip()
                    ev = None
                    if texto:
                        try:
                            ev = json.loads(texto)
                            if not isinstance(ev, dict):
                                ev = None
                        except Exception:
                            ev = None       # linea corrupta, se descarta
                    salida.append((nueva, ev))
                    if len(salida) >= maximo:
                        break
        except FileNotFoundError:
            return []
        return salida


# --- Reenviador --------------------------------------------------------------
class Reenviador:
    def __init__(self, crear_cliente=None):
        self.pos = Posicion(POSICION)
        self.cola = Cola(EVENTOS, self.pos)
        self.conectado = False
        self.enviados = 0
        self.descartados = 0
        self.reintentos = 0
        self.crear_cliente = crear_cliente or self._cliente_mqtt
        self.cli = None
        # Las ordenes llegan en el hilo de red de paho. Se atienden en otro
        # hilo para que una llamada lenta al contador no congele la conexion.
        self.ordenes = queue.Queue(maxsize=20)
        if ORDENES:
            threading.Thread(target=self._atender_ordenes, daemon=True).start()

    # ---------------------------------------------------------------- cliente
    def _cliente_mqtt(self):
        # clean_session (el valor por omision en MQTT 3.1.1) es a proposito:
        # al reconectar, el broker NO entrega ordenes que se acumularon
        # mientras la unidad estaba fuera. Una orden de video vieja no sirve.
        cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=UNIDAD)
        if USAR_TLS:
            if CA and CERT and LLAVE:
                cli.tls_set(ca_certs=CA, certfile=CERT, keyfile=LLAVE)
            elif USUARIO and not (CERT or LLAVE):
                # TLS solo de servidor (p.ej. Let's Encrypt en la VM) con
                # usuario y contrasena. CA vacia = certificados del sistema.
                cli.tls_set(ca_certs=CA or None)
            else:
                sys.exit("Con TLS activo hacen falta CIT_CA, CIT_CERT y CIT_LLAVE, "
                         "o bien CIT_USUARIO para TLS solo de servidor.")
        if USUARIO:
            cli.username_pw_set(USUARIO, CONTRASENA or None)
        cli.will_set(
            "{}/{}/estado".format(PREFIJO, UNIDAD),
            json.dumps({"ts": ahora(), "unidad": UNIDAD,
                        "tipo": "estado", "estado": "desconectada"}),
            qos=1, retain=True)
        cli.on_connect = self._al_conectar
        cli.on_disconnect = self._al_desconectar
        cli.on_message = self._al_recibir
        return cli

    def _al_conectar(self, cli, userdata, flags, rc, props=None):
        if getattr(rc, "is_failure", False) or (isinstance(rc, int) and rc != 0):
            aviso("Rechazo del broker al conectar: {}".format(rc))
            return
        self.conectado = True
        aviso("Conectado a {}:{} como {}".format(BROKER, PUERTO, UNIDAD))
        cli.publish(
            "{}/{}/estado".format(PREFIJO, UNIDAD),
            json.dumps({"ts": ahora(), "unidad": UNIDAD,
                        "tipo": "estado", "estado": "en linea"}),
            qos=1, retain=True)
        if ORDENES:
            # Se vuelve a suscribir en cada conexion: con clean_session el
            # broker olvida la suscripcion al desconectarse.
            cli.subscribe("{}/{}/cmd".format(PREFIJO, UNIDAD), qos=1)
            aviso("Escuchando ordenes en {}/{}/cmd".format(PREFIJO, UNIDAD))

    def _al_desconectar(self, cli, userdata, *a):
        self.conectado = False
        aviso("Desconectado del broker")

    # ----------------------------------------------------------------- ordenes
    def _al_recibir(self, cli, userdata, msg):
        """Hilo de red de paho: solo se encola, nunca se atiende aqui."""
        # Solo el tema exacto de ordenes, y nunca un mensaje retenido: una
        # orden de video retenida se repetiria en cada reconexion. Algunos
        # brokers (amqtt, visto en pruebas) entregan retenidos de otros temas.
        if msg.retain or msg.topic != "{}/{}/cmd".format(PREFIJO, UNIDAD):
            return
        try:
            self.ordenes.put_nowait(msg.payload)
        except queue.Full:
            aviso("Cola de ordenes llena; se descarta una orden")

    def _atender_ordenes(self):
        while True:
            crudo = self.ordenes.get()
            try:
                respuesta = self.ejecutar_orden(crudo)
            except Exception as e:
                respuesta = {"ok": False, "motivo": "error interno: {}".format(e)}
            respuesta.update({"ts": ahora(), "unidad": UNIDAD, "tipo": "respuesta"})
            aviso("ORDEN {} -> {}".format(respuesta.get("orden"), "ok" if respuesta.get("ok")
                                          else respuesta.get("motivo")))
            if not respuesta.get("ok"):
                aviso("      recibido: {!r}".format(crudo[:160]))
            try:
                if self.cli is not None and self.conectado:
                    self.cli.publish("{}/{}/respuesta".format(PREFIJO, UNIDAD),
                                     json.dumps(respuesta, ensure_ascii=False), qos=1)
            except Exception as e:
                aviso("No se pudo publicar la respuesta: {}".format(e))

    def ejecutar_orden(self, crudo):
        """Valida la orden y la pasa al contador. Devuelve la respuesta."""
        try:
            orden = json.loads(crudo.decode("utf-8") if isinstance(crudo, bytes) else crudo)
            if not isinstance(orden, dict):
                raise ValueError
        except Exception:
            return {"ok": False, "motivo": "orden ilegible, se espera JSON"}

        nombre = str(orden.get("orden", ""))
        resp = {"id": orden.get("id"), "orden": nombre}
        if nombre not in RUTAS_ORDEN:
            resp.update(ok=False, motivo="orden desconocida; validas: {}".format(
                ", ".join(sorted(RUTAS_ORDEN))))
            return resp

        ts = orden.get("ts")
        if ts:
            try:
                emitida = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc).timestamp()
            except ValueError:
                resp.update(ok=False, motivo="ts invalido, formato 2026-09-22T20:00:00Z")
                return resp
            if time.time() - emitida > VIGENCIA_ORDEN:
                resp.update(ok=False, motivo="orden vencida")
                return resp

        metodo, ruta = RUTAS_ORDEN[nombre]
        url = "{}{}?k={}".format(CONTADOR_URL.rstrip("/"), ruta,
                                 urllib.parse.quote(CLAVE_LOCAL))
        pedido = urllib.request.Request(url, method=metodo,
                                        data=b"" if metodo == "POST" else None)
        try:
            with urllib.request.urlopen(pedido, timeout=8) as r:
                cuerpo = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            resp.update(ok=False, motivo="el contador respondio {}".format(e.code))
            return resp
        except Exception as e:
            resp.update(ok=False, motivo="el contador no responde: {}".format(e))
            return resp

        if nombre == "video_estado":
            resp.update(ok=True, video=cuerpo.get("video"), aforo=cuerpo.get("aforo"))
        else:
            resp.update(ok=bool(cuerpo.get("ok")), video=cuerpo.get("video"))
            if cuerpo.get("motivo"):
                resp["motivo"] = cuerpo["motivo"]
            if cuerpo.get("segundos") is not None:
                resp["segundos"] = cuerpo["segundos"]
        return resp

    # ----------------------------------------------------------------- mensaje
    def preparar(self, ev):
        """Traduce un evento del bus al contrato de la nube. None si se descarta."""
        tipo = str(ev.get("tipo", "")).strip().lower()
        if not TIPO_VALIDO.match(tipo):
            return None, None
        cuerpo = dict(ev)
        cuerpo["unidad"] = UNIDAD
        cuerpo.setdefault("ts", ahora())
        datos = json.dumps(cuerpo, separators=(",", ":"), ensure_ascii=False)
        if len(datos.encode("utf-8")) > 120 * 1024:      # limite de AWS: 128 KB
            return None, None
        return "{}/{}/{}".format(PREFIJO, UNIDAD, tipo), datos

    def publicar(self, tema, datos):
        """Publica QoS 1 y espera confirmacion. True solo si el broker confirmo."""
        try:
            info = self.cli.publish(tema, datos, qos=1)
        except Exception as e:
            aviso("Error al publicar: {}".format(e))
            return False
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False
        try:
            info.wait_for_publish(timeout=ESPERA_PUBACK)
        except Exception:
            return False
        return info.is_published()

    # -------------------------------------------------------------------- ciclo
    def vuelta(self):
        """Envia lo pendiente. Devuelve cuantos envio."""
        n = 0
        for nueva_pos, ev in self.cola.pendientes():
            if _parar or not self.conectado:
                break
            if ev is None:
                self.descartados += 1
                self.pos.pos = nueva_pos
                self.pos.guardar()
                continue
            tema, datos = self.preparar(ev)
            if tema is None:
                self.descartados += 1
                self.pos.pos = nueva_pos
                self.pos.guardar()
                continue
            if not self.publicar(tema, datos):
                # No se confirmo. NO se avanza la posicion: se reintenta.
                self.reintentos += 1
                return n
            # La posicion solo avanza despues de la confirmacion del broker.
            self.pos.pos = nueva_pos
            self.pos.guardar()
            self.enviados += 1
            n += 1
        return n

    def correr(self):
        intento = 0
        ultimo_resumen = time.time()
        while not _parar:
            if self.cli is None:
                self.cli = self.crear_cliente()
            if not self.conectado:
                try:
                    self.cli.connect(BROKER, PUERTO, keepalive=60)
                    self.cli.loop_start()
                    espera = 0
                    while espera < 10 and not self.conectado and not _parar:
                        time.sleep(0.3)
                        espera += 0.3
                except Exception as e:
                    aviso("No se pudo conectar: {}".format(e))
                if not self.conectado:
                    d = ESPERAS[min(intento, len(ESPERAS) - 1)]
                    intento += 1
                    aviso("Reintento de conexion en {} s. "
                          "Los eventos se siguen acumulando en disco.".format(d))
                    try:
                        self.cli.loop_stop()
                    except Exception:
                        pass
                    self.cli = None
                    dormir(d)
                    continue
                intento = 0

            try:
                self.vuelta()
            except Exception as e:
                aviso("Error en la vuelta: {}".format(e))

            if time.time() - ultimo_resumen >= RESUMEN:
                ultimo_resumen = time.time()
                aviso("enviados={}  descartados={}  reintentos={}  posicion={}"
                      .format(self.enviados, self.descartados,
                              self.reintentos, self.pos.pos))
            time.sleep(INTERVALO)

        aviso("Deteniendo")
        try:
            if self.cli is not None:
                self.cli.publish(
                    "{}/{}/estado".format(PREFIJO, UNIDAD),
                    json.dumps({"ts": ahora(), "unidad": UNIDAD, "tipo": "estado",
                                "estado": "desconectada"}),
                    qos=1, retain=True).wait_for_publish(timeout=3)
                self.cli.loop_stop()
                self.cli.disconnect()
        except Exception:
            pass
        aviso("Total enviados: {}  descartados: {}".format(
            self.enviados, self.descartados))


def dormir(segundos):
    fin = time.time() + segundos
    while time.time() < fin and not _parar:
        time.sleep(0.2)


def main():
    global _parar, UNIDAD, BROKER, PUERTO, USAR_TLS, EVENTOS, POSICION

    ap = argparse.ArgumentParser(
        description="Reenviador de telemetria de Edge Mobility hacia MQTT")
    ap.add_argument("--unidad", default=None)
    ap.add_argument("--broker", default=None)
    ap.add_argument("--puerto", type=int, default=None)
    ap.add_argument("--sin-tls", action="store_true")
    ap.add_argument("--eventos", default=None)
    ap.add_argument("--posicion", default=None)
    ap.add_argument("--desde-cero", action="store_true",
                    help="ignora la posicion guardada y reenvia todo el archivo")
    a = ap.parse_args()

    if a.unidad:   UNIDAD = a.unidad
    if a.broker:   BROKER = a.broker
    if a.puerto:   PUERTO = a.puerto
    if a.sin_tls:  USAR_TLS = False
    if a.eventos:  EVENTOS = a.eventos
    if a.posicion: POSICION = a.posicion

    if a.desde_cero:
        try:
            os.remove(POSICION)
        except OSError:
            pass

    def apagar(*_):
        global _parar
        _parar = True
    signal.signal(signal.SIGINT, apagar)
    signal.signal(signal.SIGTERM, apagar)

    print("=" * 68)
    print(" CIT Mobility :: reenviador de telemetria")
    print(" Unidad   : {}".format(UNIDAD))
    print(" Broker   : {}:{}  TLS: {}".format(BROKER, PUERTO,
                                              "si" if USAR_TLS else "NO"))
    print(" Eventos  : {}".format(EVENTOS))
    print(" Posicion : {}".format(POSICION))
    print(" Temas    : {}/{}/<tipo>".format(PREFIJO, UNIDAD))
    print(" Usuario  : {}".format(USUARIO or "(ninguno)"))
    print(" Ordenes  : {}".format(
        "SI, {}/{}/cmd -> {}".format(PREFIJO, UNIDAD, CONTADOR_URL) if ORDENES
        else "no"))
    print("=" * 68)
    if ORDENES and not CLAVE_LOCAL:
        aviso("AVISO: CIT_ORDENES=1 sin CIT_CLAVE_LOCAL. El contador rechazara las ordenes.")

    Reenviador().correr()
    return 0


if __name__ == "__main__":
    sys.exit(main())
