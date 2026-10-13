#!/usr/bin/env python3
# =============================================================================
# CIT Mobility / Edge Mobility
# cit_core/serie.py :: puerto serie sin dependencias externas
#
#   Lo minimo para hablar con el modulo SIM7600G-H: abrir un /dev/ttyUSB*,
#   leer lineas y escribir comandos AT.
#
#   Por que no pyserial: el sistema no tiene ninguna dependencia de pip
#   obligatoria (ver AGENTS.md) y queremos que siga asi. Todo lo que hace
#   pyserial para este caso son cuatro llamadas a termios de la libreria
#   estandar, asi que se hacen aqui.
#
#   Flujo:
#       with Puerto("/dev/ttyUSB1") as p:
#           p.escribir("AT")            # anade el \r que pide el modulo
#           linea = p.leer_linea(2.0)   # str, o None si vencio el plazo
#
#   Regla: si el USB se desconecta, las operaciones lanzan OSError. NO se
#   traga aqui a proposito: quien llama necesita enterarse para volver a
#   buscar el modulo, que al reconectar puede ser otro /dev/ttyUSB*.
# =============================================================================

import os
import time
import select
import termios

BAUDIOS = 115200

# Los baudios no son un numero cualquiera en termios, son constantes propias.
_VELOCIDADES = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
}


class Puerto:
    """Un /dev/ttyUSB* en modo raw, 8N1, sin control de flujo."""

    def __init__(self, ruta, baudios=BAUDIOS):
        self.ruta = ruta
        self.baudios = baudios
        self.fd = None
        self._buzon = b""          # bytes leidos que aun no forman linea entera
        self._abrir()

    # --- Apertura y configuracion --------------------------------------------
    def _abrir(self):
        # O_NONBLOCK en la apertura evita quedarse colgado esperando la senal
        # DCD del modem, que estos puertos no siempre levantan.
        self.fd = os.open(self.ruta, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self._configurar()
        except Exception:
            self.cerrar()
            raise

    def _configurar(self):
        """Modo raw a mano: es lo que hace cfmakeraw() de C."""
        try:
            ajustes = termios.tcgetattr(self.fd)
        except termios.error as e:
            raise OSError("no se pudo leer la configuracion de {}: {}".format(
                self.ruta, e))

        ientrada, isalida, control, local, ivel, ovel, especiales = ajustes

        # Entrada: sin traducir CR/LF ni control de flujo por software. Sin
        # esto, los \r del modulo se convierten en \n y las lineas se parten mal.
        ientrada &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK |
                      termios.ISTRIP | termios.INLCR | termios.IGNCR |
                      termios.ICRNL | termios.IXON | termios.IXOFF |
                      termios.IXANY)

        # Salida: cruda, sin post-procesado.
        isalida &= ~termios.OPOST

        # Local: sin eco ni edicion de linea. Si quedara ICANON, el terminal
        # intentaria interpretar los datos NMEA como si fueran tecleados.
        local &= ~(termios.ECHO | termios.ECHOE | termios.ECHONL |
                   termios.ICANON | termios.ISIG | termios.IEXTEN)

        # Control: 8 bits, sin paridad, un bit de parada.
        control &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
        control |= termios.CS8
        # CLOCAL: ignorar las lineas de modem. CREAD: habilitar la recepcion.
        control |= termios.CLOCAL | termios.CREAD
        if hasattr(termios, "CRTSCTS"):
            control &= ~termios.CRTSCTS      # sin control de flujo por hardware

        velocidad = _VELOCIDADES.get(self.baudios, termios.B115200)

        # VMIN=0 VTIME=0: read() vuelve enseguida con lo que haya. El plazo lo
        # gobierna select(), no termios.
        especiales[termios.VMIN] = 0
        especiales[termios.VTIME] = 0

        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, [
                ientrada, isalida, control, local, velocidad, velocidad,
                especiales])
        except termios.error as e:
            # Los puertos de modem no aceptan todos los ajustes de termios y
            # devuelven error aunque hayan aplicado los que importan (es la
            # misma queja que suelta 'stty' en estos puertos). Se comprueba si
            # de verdad quedo configurado antes de darlo por fallido.
            try:
                actual = termios.tcgetattr(self.fd)
            except termios.error:
                raise OSError("no se pudo configurar {}: {}".format(self.ruta, e))
            if actual[4] != velocidad:
                raise OSError("no se pudo configurar {}: {}".format(self.ruta, e))

    # --- Lectura --------------------------------------------------------------
    def _leer_bytes(self, espera):
        """Espera hasta 'espera' segundos a que lleguen datos. b'' si no llegan."""
        try:
            listos, _, _ = select.select([self.fd], [], [], espera)
        except (OSError, ValueError) as e:
            raise OSError("fallo al esperar datos de {}: {}".format(self.ruta, e))
        if not listos:
            return b""
        try:
            trozo = os.read(self.fd, 4096)
        except BlockingIOError:
            return b""
        except OSError as e:
            # EIO tipico cuando alguien desenchufa el USB en caliente.
            raise OSError("se perdio {}: {}".format(self.ruta, e))
        if trozo == b"":
            raise OSError("se cerro {} (fin de flujo)".format(self.ruta))
        return trozo

    def leer_linea(self, espera=2.0):
        """Devuelve la siguiente linea completa, o None si vencio el plazo.

        El NMEA llega troceado: un read() puede traer media sentencia y dos
        sentencias del tiron. Por eso se acumula en un buzon y solo se entrega
        lo que ya tiene su fin de linea.
        """
        limite = time.monotonic() + espera
        while True:
            corte = self._buzon.find(b"\n")
            if corte >= 0:
                cruda, self._buzon = self._buzon[:corte], self._buzon[corte + 1:]
                # errors=ignore: el modulo escupe basura binaria al arrancar y
                # una linea ilegible no debe tumbar la lectura.
                return cruda.decode("ascii", errors="ignore").strip()

            restante = limite - time.monotonic()
            if restante <= 0:
                return None
            trozo = self._leer_bytes(min(restante, 0.5))
            if trozo:
                self._buzon += trozo
                # Un buzon que crece sin ver un \n significa que del otro lado
                # no hay lineas (puerto equivocado). Se recorta para no comer
                # memoria sin limite.
                if len(self._buzon) > 65536:
                    self._buzon = self._buzon[-4096:]

    def vaciar(self):
        """Tira lo que haya pendiente. Util antes de mandar un comando AT, para
        no leer como respuesta algo que ya estaba en la cola."""
        self._buzon = b""
        try:
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except (termios.error, OSError):
            pass

    # --- Escritura ------------------------------------------------------------
    def escribir(self, texto):
        """Manda un comando. El \r final lo pone esta funcion: el modulo lo
        exige y olvidarlo es el error clasico al hablar por AT."""
        datos = (texto + "\r").encode("ascii", errors="ignore")
        while datos:
            try:
                escritos = os.write(self.fd, datos)
            except BlockingIOError:
                select.select([], [self.fd], [], 1.0)
                continue
            except OSError as e:
                raise OSError("no se pudo escribir en {}: {}".format(self.ruta, e))
            datos = datos[escritos:]

    # --- Cierre ---------------------------------------------------------------
    def cerrar(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *resto):
        self.cerrar()
        return False

    def __del__(self):
        self.cerrar()
