# Edge de actuadores de Alivia

Recibe por el broker MQTT las órdenes del **edge de voz**, se las manda por **WebSocket o Bluetooth** al **ESP32 de
actuadores** (4 servos: puerta, ventana, cortina y luz) y devuelve por el mismo broker si la acción se cumplió o no.

```
edge de voz --MQTT--> broker --MQTT--> edge de actuadores --WebSocket/Bluetooth--> ESP32 --> servo
     ^                                         |                                      |
     +------------- result (ok / fallo) -------+<------------- result ---------------+
```

Todo corre local (tu PC o Mac); solo el backend está desplegado. El reporte al backend queda para una segunda etapa
(ver "Pendiente").

## Qué hace

1. Se suscribe a `alivia/actuators/+/command` (QoS 1) y valida cada orden: versión `v:2`, `deviceId` del topic igual al
   del mensaje, TTL vigente, dispositivo presente en la tabla con la misma MAC y tipo, estado `IN_USE` y acción válida
   para el tipo (`open/close` para puerta, ventana y cortina; `on/off` para la luz).
2. Busca el ESP32 que maneja ese dispositivo y le manda `{"type":"command", ...}` por su enlace (WiFi o Bluetooth).
3. Espera su `result` (máximo `[dispatch].exec_timeout_s`, 4 s por defecto, menor que el timeout del edge de voz).
4. **Siempre contesta** en `alivia/actuators/<deviceId>/result`: `ok:true` con el estado logrado, o `ok:false` con la
   causa (`embebido sin conexion`, `sin respuesta del embebido`, `caducado`, `dispositivo no autorizado`, o el detalle
   que informe el ESP32, por ejemplo `reemplazado`).
5. Si fue exitoso, publica además el último estado conocido (retenido) en `alivia/actuators/<deviceId>/state`.
6. Es idempotente: QoS 1 puede duplicar un mensaje; un `correlationId` ya resuelto repite el mismo resultado sin mover
   el servo otra vez.
7. Publica su propio estado en `alivia/actuators/status` (retenido, con LWT): `{"online":true,"embedded":[...]}`. El
   edge de voz puede exigirlo con `[mqtt].require_actuators_online = true`.

## Instalación y ejecución local

```bash
python3 -m venv .venv && source .venv/bin/activate        # en Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp config.example.toml config.toml                         # y edítalo (token, MQTT, tabla de dispositivos)
export ALIVIA_ACTUATOR_WS_TOKEN=un-secreto-largo            # o [server].token en config.toml (el mismo va en Secrets.h del ESP32)
python -m tools.dev_broker                                  # solo si no tienes Mosquitto; deja esta terminal abierta
python -m alivia_actuator_edge --config config.toml
```

### Probar sin el ESP32 ni el edge de voz

```bash
python -m tools.fake_embebido --url ws://127.0.0.1:8766/ws --token un-secreto-largo --speed 0.3
python -m tools.send_command --config config.toml --device-id 10 --action open
python -m tools.send_command --config config.toml --device-id 13 --action on
```

`fake_embebido` habla el protocolo real y aplica las mismas reglas que el firmware. Para ver los fallos:
`--fail 0=trabado` (la puerta responde `ok:false`), `--silent 3` (la luz nunca responde: timeout).

### Con el edge de voz real

En el `config.toml` del edge de voz: `[mqtt] enabled = true` y el mismo host de broker. Cada servo debe estar registrado
en el backend como dispositivo con su MAC lógica (la imprime el firmware al arrancar) y aparecer en `[[devices]]` de
este edge con el mismo `device_id`, `mac` y `type`.

## Tabla de dispositivos

Hoy es local (`[[devices]]` en `config.toml`) y se carga al arrancar. Un dispositivo solo recibe órdenes si su estado
es `IN_USE`; el ESP32 solo queda habilitado para los canales cuya MAC, tipo y estado coinciden con la tabla. La tabla
sigue el mismo modelo del backend (`deviceId`, `macAddress`, `deviceType`, `status`, `roomKey`), así que luego se
sustituye por la sincronización con `GET /api/v1/edge/device-table` sin tocar el resto (puerto `DeviceTable`).

## Estructura (DDD por contexto)

```
src/alivia_actuator_edge/
  devices/domain/model.py             MacAddress, DeviceKind, Status, Device, StaticDeviceTable (puerto DeviceTable)
  dispatch/domain/models.py           orden v2, Outcome, EmbeddedResult, parse_order
  dispatch/application/service.py     ActuatorService: valida, despacha, deduplica, contesta siempre
  dispatch/application/ports.py       EmbeddedGateway, ResultPublisher, ResultReporter
  dispatch/infrastructure/mqtt.py     adaptador paho (hilo propio) puenteado a asyncio
  dispatch/infrastructure/reporters.py LogReporter (hoy) / cliente del backend (después)
  embedded/application/hub.py         sesiones de ESP32: hello, autorización por tabla, comandos, resultados
  embedded/interfaces/transports/     WebSocket (puerto 8766) y Bluetooth SPP, tramas 0xA7 + CRC16
  http/app.py                         API Flask de salud y diagnóstico (solo lectura)
  shared/diagnostics.py               contadores y errores recientes (reportero)
  app.py, shared/config.py            composición y configuración
tools/   fake_embebido.py, send_command.py, dev_broker.py
docs/    protocolo-embebido.md, fixtures/*.json (idénticos en el repo del firmware)
```

## API de salud y diagnóstico (Flask)

El edge levanta una API HTTP **de solo lectura** (Flask, en un hilo aparte) para ver el estado sin mirar los logs. Por
defecto escucha solo en `127.0.0.1:8780`; para verla desde otra máquina pon `[http].host = "0.0.0.0"` y define
`[http].token` (se envía como `Authorization: Bearer <token>`; `/health` no lo pide).

| Endpoint | Qué muestra |
|---|---|
| `GET /health` | `ok` o `degraded` (200), o 503 si no hay broker. Sirve para monitoreo. Avisa si ningún ESP32 está conectado |
| `GET /devices` | Tabla de dispositivos: en línea o no, por qué ESP32 y canal, último resultado y contadores ok/fallo |
| `GET /embedded` | ESP32 conectados: enlace (ws/bt), canales, tiempo conectado, batería, memoria libre, señal WiFi |
| `GET /diagnostics` | Órdenes totales, ok, fallos agrupados por causa, estado del broker |
| `GET /errors?limit=50&deviceId=10` | Errores recientes (los últimos 200), con orden, dispositivo, causa y tiempo |

```bash
curl http://127.0.0.1:8780/health
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8780/devices
```

La **batería** solo aparece si el ESP32 la mide: hace falta un divisor de tensión hacia un pin ADC y definir
`BATTERY_ADC_PIN` en el sketch (el firmware la envía como telemetría cada 30 s). Sin eso `battery` es `null`. Los
errores y contadores viven en memoria: se reinician al reiniciar el edge.

## Seguridad

- El enlace con el ESP32 exige el token (comparación en tiempo constante) y que al menos un canal esté autorizado.
- La API de salud es de solo lectura y por defecto solo escucha en esta PC; si la abres a la red, usa `[http].token`.
- El edge nunca manda una orden a un canal que no pasó el hello, ni a un dispositivo que no está `IN_USE` en la tabla.
- Usa un broker con usuario y contraseña (`[mqtt].username/password`) y una red local que no sea pública.

## Pruebas

```bash
pytest            # 59 pruebas: modelo, servicio, hub (WebSocket y Bluetooth con puerto serie simulado), contrato, API de salud, punta a punta con broker real
```

Las pruebas de punta a punta levantan un broker `amqtt`, el edge completo y un ESP32 falso. Los fixtures de
`docs/fixtures` son los mismos que ejecuta el firmware en PC (`test/host` del repo `embebido-actuadores`).

## Avisos al familiar (fallas y batería baja)

Con `[backend] enabled = true` y la credencial del edge (`ALIVIA_EDGE_CREDENTIAL=<edgeId>.<secreto>`), el edge informa
al backend en `POST /api/v1/edge/events`, y el backend envía una notificación push a los familiares a cargo del hogar:

- **Falla de un dispositivo** (`DEVICE_FAILURE`): una orden que no se cumplió en el servo (el ESP32 respondió `ok:false`,
  no respondió a tiempo o estaba desconectado). Las órdenes rechazadas antes de llegar al servo (caducadas, no autorizadas
  o con una acción inválida) no se avisan. Se avisa a lo más una vez cada `failure_cooldown_s` por dispositivo.
- **Batería baja** (`LOW_BATTERY`): con la `batteryPct` que el ESP32 envía en su `telemetry`. Se avisa una vez al bajar de
  `low_battery_pct` y no otra vez hasta que se recargue por encima de `battery_rearm_pct`.

Los eventos se entregan en orden desde una cola en memoria, con reintentos crecientes mientras no haya red. Cada evento
lleva un `eventId`; si se reenvía, el backend no vuelve a notificar.

## Pendiente (etapa 2)

- Reportar al backend el resultado de cada acción exitosa (historial). Hoy `LogReporter` solo lo registra.
- Guardar en disco la cola de eventos, para no perderlos si el edge se reinicia sin red.
- Tabla de dispositivos desde el backend (long poll con ETag, como el edge de voz).
- Confirmación real con sensor (fin de carrera o contacto magnético): hoy el éxito es "el servo llegó al ángulo y pasó su
  tiempo de asentamiento". Solo cambiaría el firmware.
