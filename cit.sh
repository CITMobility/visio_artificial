#!/usr/bin/env bash
# =============================================================================
# CIT Mobility, S.A.P.I. de C.V.
# Edge Mobility. cit.sh :: arranca y detiene la unidad completa.  v5
#
#   ./cit.sh arrancar           levanta la unidad en una sesion de tmux
#   ./cit.sh parar              la detiene en el orden correcto
#   ./cit.sh reiniciar          parar + arrancar (lo usa el comando app.restart)
#   ./cit.sh estado             dice que esta vivo, a donde envia y como va el video
#   ./cit.sh liga               liga del tunel (solo con TUNEL=1)
#   ./cit.sh instalar-arranque  arrancar sola al encender (crontab @reboot)
#   ./cit.sh quitar-arranque    deshace lo anterior
#
# CAMBIOS DE ESTA VERSION (v5, 24-sep-2026)
#   ARQUITECTURA DEL ADR-001 (tres planos: telemetria, medios y control).
#   1. La ventana "vm" ya no corre reenviador.py sino agente.py, el agente
#      publicador unico: arma la telemetria de la tabla 3 del ADR, la guarda
#      en disco y la sube; recibe los comandos de la lista cerrada.
#   2. El contador por omision es contador_p2p_v17.py: sesion de video sin
#      renovacion y cuota en sesiones al mes, guardada en disco.
#   3. "reiniciar" nuevo: es lo que ejecuta el comando app.restart.
#   4. "instalar-arranque" nuevo: sin el, un module.restart deja la unidad
#      apagada hasta que alguien entre a arrancarla.
#   5. TLS hacia la VM activo por omision (VM_MQTT_TLS=1), como exige el ADR.
#   6. AWS sin cambios: sigue apagado y de reserva (ENVIAR_AWS=1).
#
# CAMBIOS DE LA VERSION ANTERIOR (v4, 22-sep-2026)
#   LOS DATOS Y EL VIDEO VAN A LA VM.
#   1. La configuracion sale de unidad.env (copiar de unidad.env.ejemplo), ya
#      no de valores escritos aqui. Las contrasenas no aparecen en los
#      comandos de tmux: cada ventana carga el archivo por su cuenta.
#   2. Nueva ventana "vm": reenviador.py hacia el broker MQTT de la VM, con
#      su propia posicion de lectura (datos/reenvio_vm.pos). Tambien recibe
#      las ordenes de video de la VM y se las pasa al contador.
#   3. El contador por omision es contador_p2p_v16.py: H.264 por SRT a la VM,
#      solo cuando la VM lo ordena y 60 s como maximo por orden.
#   4. AWS se conserva pero va apagado: ENVIAR_AWS=1 lo vuelve a encender,
#      con su posicion de siempre (datos/reenvio.pos).
#   5. El tunel de cloudflared va apagado: con v16 no hay pagina que publicar.
#      TUNEL=1 con CONTADOR=contador_p2p_v15.py vuelve al modo anterior.
#   6. La clave del control local se genera en cada arranque. Ya no es fija.
#
# Ventanas que crea:
#   contador   camara, conteo, video bajo orden
#   gps        lector del modulo GNSS
#   vm         agente.py: telemetria + comandos de la VM    (si hay VM_HOST)
#   aws        reenviador hacia AWS IoT Core               (si ENVIAR_AWS=1)
#   tunel      cloudflared                                 (si TUNEL=1)
# =============================================================================
set -u

CIT="$HOME/Desktop/cit"
DS="$CIT/DeepStream-Yolo"
SESION="cit"
CONF="$CIT/unidad.env"
SESION_ENV="$CIT/datos/sesion.env"
LOG_TUNEL="$HOME/tunel.log"
PATRON='https://[a-z0-9]+(-[a-z0-9]+)+\.trycloudflare\.com'

# Valores por omision; unidad.env los sobrescribe.
UNIDAD_ID=cit-001
UNIDAD="Ruta A-01"
VEHICULO_ID=""
VM_HOST=""
ENVIAR_AWS=0
TUNEL=0
CONTADOR=contador_p2p_v17.py
CIT_BROKER=a3g1b4rvksirbn-ats.iot.us-east-1.amazonaws.com
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"

# Lo que cada ventana ejecuta antes de su programa. Se escribe con comillas
# simples a proposito: las variables se expanden DENTRO de la ventana, asi las
# contrasenas nunca se ven en el texto del comando.
CARGA='set -a; [ -f "$HOME/Desktop/cit/unidad.env" ] && . "$HOME/Desktop/cit/unidad.env"; . "$HOME/Desktop/cit/datos/sesion.env"; set +a'

# -----------------------------------------------------------------------------
detener() {
    echo "Deteniendo la unidad..."
    # Primero por las buenas: los reenviadores publican su estado de apagado
    # y el contador cierra el video y libera la camara.
    #
    # Los patrones van ANCLADOS al comando real ("^python3 ..."). Sin ancla,
    # pkill -f tambien mata cualquier proceso que solo MENCIONE el nombre:
    # un tail -f del log, un editor abierto sobre el archivo, un grep.
    pkill -TERM -f '^python3 (.*/)?agente\.py'      2>/dev/null
    pkill -TERM -f '^python3 (.*/)?reenviador\.py'  2>/dev/null
    pkill -INT  -f '^python3 (.*/)?variables\.py'   2>/dev/null
    pkill -INT  -f '^python3 (.*/)?contador_p2p'    2>/dev/null
    sleep 6
    # Despues se barre lo que haya quedado, incluido el envio SRT, que vive en
    # su propio grupo de procesos y no muere con el contador.
    pkill -f '^python3 (.*/)?contador_neto\.py'  2>/dev/null
    pkill -f '^python3 (.*/)?contador_p2p'       2>/dev/null
    pkill -f '^python3 (.*/)?lector_gps\.py'     2>/dev/null
    pkill -f '^python3 (.*/)?agente\.py'         2>/dev/null
    pkill -f '^gst-launch-1\.0 udpsrc .*srtsink' 2>/dev/null
    pkill -f '^(\./|.*/)?go2rtc( |$)'            2>/dev/null
    pkill -f '^cloudflared '                     2>/dev/null
    sleep 2
    tmux kill-session -t "$SESION" 2>/dev/null
    echo "Listo."
}

# -----------------------------------------------------------------------------
liga() {
    [ -f "$LOG_TUNEL" ] || { echo "no hay tunel"; return; }
    l=$(grep -oE "$PATRON" "$LOG_TUNEL" | tail -1)
    clave=$(grep -oP '^CLAVE=\K.*' "$SESION_ENV" 2>/dev/null)
    if [ -n "$l" ]; then echo "${l}/?k=${clave}"; else echo "todavia no hay"; fi
}

# -----------------------------------------------------------------------------
estado() {
    echo "--- sesion de tmux ---"
    tmux list-windows -t "$SESION" -F '#{window_name}' 2>/dev/null | tr '\n' ' ' || echo "no hay sesion"
    echo; echo
    echo "--- procesos ---"
    ps -eo pid,etime,args | grep -E "contador_p2p|contador_neto|lector_gps|agente\.py|reenviador|srtsink|cloudflared" \
           | grep -v grep || echo "ninguno"
    echo
    echo "--- destinos ---"
    echo " VM : ${VM_HOST:-NO CONFIGURADA}   AWS: $([ "$ENVIAR_AWS" = 1 ] && echo encendido || echo apagado)"
    echo
    echo "--- contador (control local) ---"
    clave=$(grep -oP '^CLAVE=\K.*' "$SESION_ENV" 2>/dev/null)
    curl -s -m 3 "http://127.0.0.1:8080/estado?k=${clave}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(" sin respuesta"); sys.exit()
v = d.get("video", {})
print(" aforo {}  (entradas {}, salidas {})".format(d.get("aforo"), d.get("entradas"), d.get("salidas")))
c = d.get("camara") or {}
print(" camara {}  (ultimo cuadro hace {} s)".format("viva" if c.get("activa") else "SIN CUADROS", c.get("edad_s")))
print(" video {}  restan {} s  sesiones restantes este mes {}  srt: {}".format(
    "ABIERTO" if v.get("abierta") else "cerrado", v.get("restan_s"),
    v.get("sesiones_restantes", v.get("cuota_restante_s")), v.get("srt")))' 2>/dev/null || echo " sin respuesta"
    echo
    echo "--- ultimas lineas del agente (VM) ---"
    tmux capture-pane -p -t "${SESION}:vm" 2>/dev/null | grep -v '^$' | tail -4 || echo " sin ventana vm"
    if [ "$TUNEL" = 1 ]; then echo; echo "--- liga ---"; liga; fi
}

# -----------------------------------------------------------------------------
arrancar() {
    if [ -n "${TMUX:-}" ]; then
        echo "ERROR: esto se ejecuta FUERA de tmux."
        echo "Sal con Ctrl+b d y vuelve a intentarlo."
        exit 1
    fi
    if [ ! -f "$DS/$CONTADOR" ]; then
        echo "ERROR: no encuentro $DS/$CONTADOR"
        exit 1
    fi
    if [ ! -f "$CONF" ]; then
        echo "AVISO: no existe $CONF. Se arranca sin VM (solo conteo y GPS)."
        echo "       cp $CIT/unidad.env.ejemplo $CONF  y llenalo."
    fi
    if [ -z "$VM_HOST" ]; then
        echo "AVISO: VM_HOST vacio. No se enviara nada a la VM ni habra video."
    fi

    detener
    mkdir -p "$CIT/datos"

    # Clave del control local del contador, nueva en cada arranque. La
    # comparten el contador y el reenviador de la VM, nadie mas.
    ( umask 077
      printf 'CLAVE=%s\n' "$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)" > "$SESION_ENV" )

    echo "Levantando la unidad..."
    tmux new-session -d -s "$SESION" -n contador -c "$DS"
    tmux send-keys -t "${SESION}:contador" "$CARGA"'; SIN_GPS=1 SRT_DESTINO=${VM_HOST:+srt://$VM_HOST:${VM_SRT_PUERTO:-8890}} SRT_STREAMID=${VM_SRT_STREAMID:-publish:$UNIDAD_ID} SRT_PASSPHRASE=${VM_SRT_PASSPHRASE:-} python3 '"$CONTADOR" C-m

    tmux new-window -t "$SESION" -n gps -c "$CIT"
    tmux send-keys -t "${SESION}:gps" "$CARGA"'; python3 lector_gps.py' C-m

    if [ -n "$VM_HOST" ]; then
        tmux new-window -t "$SESION" -n vm -c "$CIT"
        # agente.py sustituye aqui al reenviador de la v4. reenviador.py no se
        # toca: sigue siendo el de AWS, mas abajo.
        tmux send-keys -t "${SESION}:vm" "$CARGA"'; CIT_UNIDAD=$UNIDAD_ID CIT_VEHICULO=${VEHICULO_ID:-} CIT_BROKER=$VM_HOST CIT_PUERTO=${VM_MQTT_PUERTO:-8883} CIT_TLS=${VM_MQTT_TLS:-1} CIT_CA=${VM_MQTT_CA:-} CIT_CERT=${VM_MQTT_CERT:-} CIT_LLAVE=${VM_MQTT_LLAVE:-} CIT_USUARIO=${VM_MQTT_USUARIO:-} CIT_CONTRASENA=${VM_MQTT_CONTRASENA:-} CIT_CLAVE_LOCAL=$CLAVE python3 agente.py' C-m
    fi

    if [ "$ENVIAR_AWS" = 1 ]; then
        tmux new-window -t "$SESION" -n aws -c "$CIT"
        tmux send-keys -t "${SESION}:aws" "$CARGA"'; CIT_UNIDAD=$UNIDAD_ID CIT_CA=certs/AmazonRootCA1.pem CIT_CERT=certs/$UNIDAD_ID.cert.pem CIT_LLAVE=certs/$UNIDAD_ID.private.key CIT_USUARIO= CIT_ORDENES=0 python3 reenviador.py' C-m
    fi

    l=""
    if [ "$TUNEL" = 1 ]; then
        : > "$LOG_TUNEL"
        tmux new-window -t "$SESION" -n tunel -c "$HOME"
        tmux send-keys -t "${SESION}:tunel" \
            "cloudflared tunnel --protocol http2 --edge-ip-version 4 --url http://localhost:8080 2>&1 | tee $LOG_TUNEL" C-m
        echo "Esperando la liga del tunel (hasta 60 s)..."
        for _ in $(seq 1 30); do
            sleep 2
            l=$(grep -oE "$PATRON" "$LOG_TUNEL" 2>/dev/null | tail -1)
            [ -n "$l" ] && break
        done
    fi

    echo
    echo "======================================================================"
    echo " CIT Mobility :: unidad $UNIDAD_ID en marcha"
    echo "----------------------------------------------------------------------"
    echo " Contador : $CONTADOR"
    echo " VM       : ${VM_HOST:-NO CONFIGURADA}  (telemetria + comandos MQTT; video SRT)"
    echo " AWS      : $([ "$ENVIAR_AWS" = 1 ] && echo encendido || echo apagado)"
    [ "$TUNEL" = 1 ] && echo " Liga     : ${l:-todavia no hay, ./cit.sh liga}$([ -n "$l" ] && echo "/?k=$(grep -oP '^CLAVE=\K.*' "$SESION_ENV")")"
    echo
    echo " Ver ventanas:     tmux a -t $SESION"
    echo " Cambiar:          Ctrl+b y el numero, o Ctrl+b w para la lista"
    echo " Salir sin apagar: Ctrl+b d"
    echo " Como va:          ./cit.sh estado"
    echo " Apagar todo:      ./cit.sh parar"
    echo
    echo " El modelo tarda hasta 90 s en cargar la primera vez."
    echo "======================================================================"
}

# -----------------------------------------------------------------------------
# Arranque al encender. Con crontab @reboot y no con systemd porque no pide
# permisos de administrador. La espera deja que aparezcan la camara, el modulo
# GNSS y la red antes de lanzar nada.
LINEA_CRON="@reboot sleep 30 && $CIT/cit.sh arrancar >> $CIT/datos/arranque.log 2>&1"

instalar_arranque() {
    ( crontab -l 2>/dev/null | grep -vF "$CIT/cit.sh arrancar"; echo "$LINEA_CRON" ) | crontab -
    echo "Instalado. La unidad arrancara sola 30 s despues de encender:"
    crontab -l | grep -F "$CIT/cit.sh"
}

quitar_arranque() {
    crontab -l 2>/dev/null | grep -vF "$CIT/cit.sh arrancar" | crontab -
    echo "Quitado. La unidad ya no arranca sola."
}

# -----------------------------------------------------------------------------
case "${1:-}" in
    arrancar|start)    arrancar ;;
    parar|stop)        detener  ;;
    reiniciar|restart) arrancar ;;          # arrancar ya detiene primero
    estado|status)     estado   ;;
    liga|url)          liga     ;;
    instalar-arranque) instalar_arranque ;;
    quitar-arranque)   quitar_arranque ;;
    *)
        echo "Uso: ./cit.sh {arrancar|parar|reiniciar|estado|liga|instalar-arranque|quitar-arranque}"
        exit 1
        ;;
esac
