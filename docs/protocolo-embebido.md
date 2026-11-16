# Protocolo edge de actuadores y embebido (ESP32)

Fuente de verdad del enlace entre el **edge de actuadores** (servidor, Python) y el **embebido de actuadores** (cliente,
ESP32 con 4 servos). Este archivo y la carpeta `fixtures/` son idénticos en los repos `edge-actuadores` y
`embebido-actuadores`: si cambian en uno, se actualizan en el otro.

- Transporte WiFi: `ws://<host>:<puerto>/ws` (puerto por defecto **8766**, el edge de voz usa el 8765).
- Transporte Bluetooth Classic (SPP): mismas tramas `0xA7 | tipo | largo | xor | payload | crc16` que el nodo de voz
  (solo se usan los tipos 1 texto, 3 ping y 4 pong).
- Todos los mensajes son JSON UTF-8 de texto. No hay audio.
- Un solo ESP32 maneja **4 servos**. Cada servo es un **canal** (0 a 3) y en el backend es un dispositivo distinto
  (puerta, ventana, cortina, luz) con su propia MAC. Como el ESP32 solo tiene una MAC real, cada canal usa una **MAC
  lógica** que el firmware calcula y escribe en el `hello`.

## MAC lógica de cada canal

Se parte de la MAC base del ESP32 (`esp_read_mac`), se activa el bit de "administrada localmente" del primer byte
(`| 0x02`, para no chocar con MAC reales) y se suma `canal + 1` al último byte (módulo 256). Ejemplo con base
`00:11:22:33:44:20`: canal 0 = `02:11:22:33:44:21`, canal 1 = `02:11:22:33:44:22`, y así. El firmware las imprime al
arrancar: **esas son las MAC que se registran en el backend**, una por dispositivo. Si prefieres otras, el sketch permite
fijarlas a mano (`CHANNEL_MAC_OVERRIDE`).

## 1. Saludo (hello)

ESP32 a edge, primer mensaje tras conectar (el edge espera 5 s como máximo). Ver `fixtures/hello.json`.

```json
{"type":"hello","deviceId":"alivia-act-01","mac":"AA:BB:CC:00:00:20","capabilities":["actuators"],"fw":"1.0.0",
 "token":"...","channels":[{"channel":0,"mac":"AA:BB:CC:00:00:10","kind":"DOOR"}, ...]}
```

- `token`: el mismo de `[server].token` del edge (o `ALIVIA_ACTUATOR_WS_TOKEN`).
- `mac`: MAC base del ESP32. Identifica la sesión; el acceso lo deciden las MAC de los canales.
- `channels[].kind`: `DOOR`, `WINDOW`, `CURTAIN` o `LIGHT`.

El edge valida el token y que **al menos un canal** esté en su tabla de dispositivos con el mismo tipo y estado `IN_USE`.
Responde `fixtures/ready.json` (`channels` lista los canales habilitados; los demás quedan denegados) o
`fixtures/error.json` y cierra. Si el mismo ESP32 se conecta otra vez, el edge cierra la sesión anterior (WiFi gana a
Bluetooth si están las dos).

## 2. Comando (edge a ESP32)

Ver `fixtures/command.json`.

```json
{"type":"command","correlationId":"9f1c...","channel":0,"mac":"AA:BB:CC:00:00:10","kind":"DOOR","action":"open","ttlMs":5000}
```

- Acciones: `open` y `close` (DOOR, WINDOW, CURTAIN), `on` y `off` (LIGHT).
- `ttlMs`: tiempo máximo que el comando puede esperar en el ESP32 antes de ejecutarse. Pasado ese tiempo no se mueve el servo.
- El ESP32 comprueba que `channel`, `mac` y `kind` coincidan con su configuración (defensa adicional).

## 3. Resultado (ESP32 a edge)

Ver `fixtures/result_ok.json` y `fixtures/result_fail.json`. **Siempre se responde**, con éxito o con fallo.

```json
{"type":"result","correlationId":"9f1c...","channel":0,"ok":true,"detail":"","state":"open","angle":90,"elapsedMs":700}
```

- `ok` es `true` cuando el servo recibió el ángulo destino y pasó su tiempo de asentamiento (`settleMs`). **Un servo común
  no devuelve su posición**, así que esto no detecta una puerta trabada o una luz que no cambió. Si más adelante se agrega
  un fin de carrera, solo cambia la condición de éxito del firmware; el protocolo no cambia.
- `state`: `open`, `closed`, `on` u `off` cuando `ok` es `true`; vacío si falló. `angle`: ángulo aplicado (-1 si falló).
- `detail` cuando falla: `comando invalido`, `canal inexistente`, `mac no coincide`, `tipo no coincide`,
  `accion invalida`, `caducado`, `reemplazado`.

### Reglas del ESP32

1. **Idempotente.** Si llega un `correlationId` ya resuelto (QoS 1 puede duplicar), repite el mismo resultado sin mover el servo.
2. **Mismo comando en curso.** Si llega la misma acción mientras el canal se mueve, el segundo comando espera el mismo movimiento y recibe su resultado al terminar.
3. **Acción contraria en curso.** El comando nuevo reemplaza al anterior: el anterior termina con `ok:false` y `detail:"reemplazado"`.
4. **Repetir una acción ya terminada** vuelve a mandar el ángulo (no se asume la posición física) y espera `repeatSettleMs` (150 ms).
5. Los 4 canales se mueven de forma independiente.
6. Al arrancar, cada servo se lleva al ángulo "cerrado/apagado". La posición física real se desconoce hasta el primer comando.

## 3b. Telemetria (ESP32 a edge, opcional)

Ver `fixtures/telemetry.json`. El ESP32 la manda al quedar listo el enlace y cada 30 s. El edge la expone en su API de
salud (`GET /embedded`); no afecta a las ordenes y puede faltar sin problema.

```json
{"type":"telemetry","uptimeMs":12345,"freeHeap":180000,"rssi":-55,"batteryMv":3850}
```

- `rssi` (dBm) solo por WiFi; `batteryMv` solo si el ESP32 tiene un divisor de tension hacia un pin ADC (ver `BATTERY_ADC_PIN`
  en el sketch). Los campos no disponibles se omiten. El edge solo guarda `uptimeMs`, `freeHeap`, `rssi`, `batteryMv`,
  `batteryPct` y `resetReason`.

## 4. Mantenimiento del enlace

- WiFi: ping/pong WebSocket cada 10 s (lo envía el edge y la librería del ESP32 responde sola). Sin respuesta, se cierra.
- Bluetooth: tramas PING/PONG cada 10 s en ambos sentidos.
- El ESP32 reconecta con espera creciente de 1 s hasta 10 s. Si el enlace cae, los servos **no** se mueven solos; los
  comandos en curso terminan normalmente y sus resultados se descartan si no hay enlace.

## 5. Casos de prueba compartidos

`fixtures/cases.json` describe la configuración de los 4 canales y 15 casos (comando, tiempo, resultado esperado con el
instante en que debe salir). El firmware los ejecuta en una prueba en PC (`test/host`) y el edge comprueba que sus
mensajes tienen la misma forma (`tests/test_contract.py`).
