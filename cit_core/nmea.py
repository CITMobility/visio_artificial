#!/usr/bin/env python3
# =============================================================================
# CIT Mobility / Edge Mobility
# cit_core/nmea.py :: interpretar las sentencias que emite el GNSS
#
#   El modulo SIM7600G-H escupe texto NMEA a 1 Hz por su puerto de GPS:
#
#       $GPGGA,011633.00,1901.725204,N,09814.890812,W,1,07,0.8,2117.1,M,...*72
#       $GPRMC,011633.00,A,1901.725204,N,09814.890812,W,0.0,,180826,,,A*XX
#       $GPGSV,...   $GLGSV,...   $BDGSV,...      <- satelites a la vista
#
#   De todo ese flujo solo interesan dos sentencias:
#
#       GGA  posicion, altitud, satelites, HDOP y calidad del fix
#       RMC  velocidad sobre el terreno y rumbo
#
#   Aqui solo hay funciones puras (texto -> numeros). No abren puertos ni
#   publican nada, para que se puedan probar en aislado:
#
#       python3 -m cit_core.nmea
#
#   Ojo con el prefijo: la constelacion cambia la primera letra ($GP de GPS,
#   $GL de GLONASS, $BD de BeiDou, $GN combinada), pero las tres letras
#   finales son las que dicen el tipo de sentencia. Por eso se compara SIEMPRE
#   por el sufijo, nunca por la sentencia entera.
# =============================================================================


def grados(valor, hemisferio):
    """Convierte el formato NMEA ddmm.mmmm a grados decimales.

    El NMEA no da grados decimales: da grados pegados a minutos. Los grados
    son los dos primeros digitos en latitud y los tres primeros en longitud;
    localizarlos contando desde el punto decimal funciona para ambos casos sin
    tener que saber cual se esta convirtiendo.

        1901.722343 N  ->  19 + (1.722343 / 60)  ->   19.0287057
        09814.892331 W ->  98 + (14.892331 / 60) ->  -98.2482055
    """
    valor = str(valor).strip()
    punto = valor.index(".")
    minutos = float(valor[punto - 2:])
    enteros = float(valor[:punto - 2])
    decimal = enteros + minutos / 60.0
    return -decimal if hemisferio.strip().upper() in ("S", "W") else decimal


def verificar(linea):
    """Comprueba el checksum '*XX' del final de la sentencia.

    Es el XOR de todo lo que hay entre '$' y '*'. Se valida siempre: una linea
    corrupta por ruido en el USB puede tener coordenadas con un digito
    cambiado, y eso da un punto en mitad del mar sin que nada mas lo delate.
    """
    if not linea.startswith("$") or "*" not in linea:
        return False
    cuerpo, _, esperado = linea[1:].partition("*")
    esperado = esperado.strip()[:2]
    if len(esperado) != 2:
        return False
    calculado = 0
    for caracter in cuerpo:
        calculado ^= ord(caracter)
    try:
        return calculado == int(esperado, 16)
    except ValueError:
        return False


def _numero(texto, defecto=None):
    """float() que no revienta: los campos NMEA vienen vacios muy a menudo."""
    try:
        return float(texto)
    except (TypeError, ValueError):
        return defecto


def _entero(texto, defecto=None):
    valor = _numero(texto)
    return defecto if valor is None else int(valor)


def _analizar_gga(campos):
    """GGA: la sentencia de posicion.

    $GPGGA,hora,lat,N,lon,W,calidad,satelites,hdop,altitud,M,...
       0     1    2  3  4  5    6        7      8     9
    """
    if len(campos) < 10:
        return None

    calidad = _entero(campos[6], 0)
    # calidad 0 = sin fix. Los campos de posicion pueden traer aun el ultimo
    # valor conocido, asi que hay que descartarlos explicitamente.
    if not calidad:
        return {"fix": False, "sats": _entero(campos[7], 0)}

    if not campos[2] or not campos[4]:
        return {"fix": False, "sats": _entero(campos[7], 0)}

    try:
        lat = grados(campos[2], campos[3])
        lon = grados(campos[4], campos[5])
    except (ValueError, IndexError):
        return None

    return {
        "fix": True,
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "alt": _numero(campos[9]),
        "sats": _entero(campos[7], 0),
        "hdop": _numero(campos[8]),
        "calidad": calidad,
    }


def _analizar_rmc(campos):
    """RMC: velocidad y rumbo (y el aviso de validez).

    $GPRMC,hora,estado,lat,N,lon,W,velocidad,rumbo,fecha,...
       0     1     2    3  4  5  6     7       8     9
    """
    if len(campos) < 9:
        return None

    if campos[2].strip().upper() != "A":     # 'V' = dato no valido
        return {"fix": False}

    nudos = _numero(campos[7])
    return {
        "fix": True,
        # El NMEA da nudos; el resto del sistema trabaja en km/h.
        "vel": None if nudos is None else round(nudos * 1.852, 1),
        "rumbo": _numero(campos[8]),
    }


def analizar(linea):
    """Traduce una sentencia NMEA a un diccionario, o None si no interesa.

    Devuelve None para las sentencias que no aportan posicion (GSV con los
    satelites a la vista, PQXFI propietaria de Qualcomm, GSA, VTG...) y para
    las que llegan corruptas.
    """
    if not linea or not linea.startswith("$"):
        return None
    if not verificar(linea):
        return None

    cuerpo = linea[1:].split("*")[0]
    campos = cuerpo.split(",")
    tipo = campos[0][-3:].upper()       # las 3 ultimas letras: GGA, RMC, GSV...

    if tipo == "GGA":
        return _analizar_gga(campos)
    if tipo == "RMC":
        return _analizar_rmc(campos)
    return None


# =============================================================================
# Prueba rapida con las sentencias reales capturadas del modulo
# =============================================================================
if __name__ == "__main__":
    # El dato validado en banco el 18-ago-2026 (guia GPS, seccion 9 y 17).
    print("grados('1901.722343','N')  =", grados("1901.722343", "N"))
    print("grados('09814.892331','W') =", grados("09814.892331", "W"))
    print("esperado                   = 19.0287057 / -98.2482055\n")

    muestras = [
        "$GPGGA,011633.00,1901.725204,N,09814.890812,W,1,07,0.8,2117.1,M,0.0,M,,*72",
        "$GPGSV,4,1,14,02,06,147,19,03,19,191,19,04,79,092,33,08,58,119,28,1*67",
        "$PQXFI,011633.0,1901.725204,N,09814.890812,W,2117.1,4.41,2.50,0.13*4A",
        "$GPGGA,011633.00,,,,,0,00,,,M,,M,,*4E",            # sin fix todavia
        "$GPGGA,011633.00,1901.725204,N,09814.890812,W,1,07,0.8,2117.1,M,0.0,M,,*00",  # checksum malo
    ]
    for m in muestras:
        print("{:<45} -> {}".format(m[:45], analizar(m)))
